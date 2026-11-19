"""Constants for GivEnergy Gateway Modbus Coms."""
DOMAIN = "givenergy_modbus"
DEFAULT_PORT = 8899
DEFAULT_SCAN_INTERVAL = 15
MIN_SCAN_INTERVAL = 5
DEFAULT_SLOW_EVERY = 4  # holding/config registers every Nth poll

CONF_SLOW_EVERY = "slow_every"

PLATFORMS = ["sensor", "switch", "number", "select", "time", "button"]

SERVICE_PROBE = "probe_registers"
SERVICE_READ = "read_registers"
SERVICE_WRITE = "write_register"
