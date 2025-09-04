import asyncio, logging, requests, sys, os, re
from typing import Optional, Tuple
import string
import binascii


from config import HA_TOKEN, HA_URL, CONFIG_PORT, LOG_LEVEL

# ───────────────────────────────────────────────
# Logging
# ───────────────────────────────────────────────
logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s.%(msecs)03d %(message)s",
    datefmt="%H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("pdeg_ha_bridge")

# ───────────────────────────────────────────────
# Helpers
# ───────────────────────────────────────────────
def parse_command(line: str) -> Tuple[str, str, Optional[int]]:
    """
    Parse: <entity_id> <on|off> [brightness]
      - brightness: 0-255 OR '0-100%'
    Returns: (entity_id, action, brightness_or_None)
    Raises ValueError for bad input.
    """
    if not line:
        raise ValueError("Empty command")

    # Collapse whitespace; PDEG may send extra spaces
    parts = re.split(r"\s+", line.strip())
    if len(parts) < 2:
        raise ValueError("Expected: <entity_id> <on|off> [brightness]")

    entity_id = parts[0].lower()
    action = parts[1].lower()

    if action not in ("on", "off"):
        raise ValueError("Action must be ON or OFF")

    brightness = None
    if len(parts) >= 3 and action == "on":
        raw_bri = parts[2].strip().lower()
        if raw_bri.endswith("%"):
            # percentage 0..100 → scale to 0..255
            try:
                pct = int(raw_bri[:-1])
            except ValueError:
                raise ValueError("Brightness percentage must be an integer 0..100%")
            if not (0 <= pct <= 100):
                raise ValueError("Brightness percentage out of range 0..100%")
            brightness = round(pct * 255 / 100)
        else:
            # raw 0..255
            try:
                bri = int(raw_bri)
            except ValueError:
                raise ValueError("Brightness must be an integer 0..255 or '0..100%'")
            if not (0 <= bri <= 255):
                raise ValueError("Brightness out of range 0..255")
            brightness = bri

    return entity_id, action, brightness

def bytes_hex(raw: bytes) -> str:
    # "18 27 01 03 03 6C 69 67 68 74 ..."
    return " ".join(f"{b:02X}" for b in raw)

def clean_command(raw: bytes) -> str:
    """
    Strip any binary header PDEG prepends.
    Keep from the first ASCII letter (A-Z or a-z).
    """
    # find first A-Z or a-z
    start = None
    for i, b in enumerate(raw):
        if (65 <= b <= 90) or (97 <= b <= 122):
            start = i
            break
    if start is None:
        return ""  # no textual payload
    return raw[start:].decode("utf-8", errors="ignore").strip()

def domain_from_entity(entity_id: str) -> str:
    if "." not in entity_id:
        raise ValueError("entity_id must include domain, e.g. 'light.shelly_bulb_1'")
    return entity_id.split(".", 1)[0]


def build_service_call(entity_id: str, action: str, brightness: Optional[int]) -> Tuple[str, str, dict]:
    """
    Map entity domain to HA service + payload.
    - lights: light.turn_on/off; brightness only on 'on'
    - switches: switch.turn_on/off (brightness ignored)
    - fallback: generic 'homeassistant.turn_on/off'
    """
    dom = domain_from_entity(entity_id)
    data = {"entity_id": entity_id}

    if dom == "light":
        svc_domain = "light"
        service = "turn_on" if action == "on" else "turn_off"
        if action == "on" and brightness is not None:
            data["brightness"] = brightness
    elif dom == "switch":
        svc_domain = "switch"
        service = "turn_on" if action == "on" else "turn_off"
    else:
        # Generic fallback so you can target fans, etc., if they support HA turn_on/off
        svc_domain = "homeassistant"
        service = "turn_on" if action == "on" else "turn_off"

    log.info(f"json: domain:{svc_domain}, servic:{service}, data:{data}")
    return svc_domain, service, data


def call_ha_service(domain: str, service: str, data: dict) -> None:
    url = f"{HA_URL}/api/services/{domain}/{service}"
    log.info(f"url call: {url}")
    headers = {"Authorization": f"Bearer {HA_TOKEN}", "Content-Type": "application/json"}
    # NOTE: requests is blocking; keep it simple per your base code.
    resp = requests.post(url, headers=headers, json=data, timeout=5)
    if resp.status_code >= 400:
        raise RuntimeError(f"HA service error {resp.status_code}: {resp.text[:200]}")


# ───────────────────────────────────────────────
# TCP handler
# ───────────────────────────────────────────────
async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
    peer = None
    try:
        peer = writer.get_extra_info("peername")
        raw = await reader.readline()
        line = clean_command(raw)
        log.info(f"RX from {peer}: {line!r}")

        try:
            entity_id, action, brightness = parse_command(line)
        except ValueError as ve:
            log.warning(f"Input error: {ve}; line={line!r}")
            # input error: just drop the connection
            return

        try:
            domain, service, payload = build_service_call(entity_id, action, brightness)
            log.info(f"HA call: {domain}.{service} {payload}")
        except ValueError as ve:
            log.warning(f"build service error: {ve}; line={line!r}")
            # input error: just drop the connection
            return
        
        # Run blocking HTTP call without freezing the loop
        await asyncio.to_thread(call_ha_service, domain, service, payload)
        log.info("OK")

    except Exception:
        # Any unexpected error -> log and exit to allow restart
        log.exception("Fatal error in handler; exiting for supervisor restart")
        os._exit(1)
    finally:
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass


async def main():
    try:
        server = await asyncio.start_server(handle, "0.0.0.0", CONFIG_PORT)
        addr = ", ".join(str(s.getsockname()) for s in server.sockets)
        log.info(f"Listening on {addr}")
        async with server:
            await server.serve_forever()
    except Exception:
        log.exception("Fatal error in main; exiting for supervisor restart")
        os._exit(1)


if __name__ == "__main__":
    asyncio.run(main())
