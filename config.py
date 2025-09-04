import os

HA_URL = os.getenv("HA_URL", "")
HA_TOKEN = os.getenv("HA_TOKEN", "")
CONFIG_PORT = os.getenv("CONFIG_PORT", 2323)
LOG_LEVEL = os.getenv("LOG_LEVEL", "ERROR")
BRI_MAP = [int(x) for x in os.getenv("BRI_MAP", "100,60,20,0").split(",")]
