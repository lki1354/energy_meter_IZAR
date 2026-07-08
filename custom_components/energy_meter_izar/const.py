"""Constants for the Energy Meter IZAR integration."""

from __future__ import annotations

DOMAIN = "energy_meter_izar"

CONF_PROTOCOL = "protocol"
CONF_DIRECTORY = "directory"
CONF_FILE_PATTERN = "file_pattern"
CONF_POLL_INTERVAL = "poll_interval"
CONF_REQUIRE_RDY = "require_rdy"
CONF_DELETE_AFTER = "delete_after"

PROTOCOL_FTP = "ftp"
PROTOCOL_FTPS = "ftps"
PROTOCOL_SFTP = "sftp"
PROTOCOLS = [PROTOCOL_FTP, PROTOCOL_FTPS, PROTOCOL_SFTP]

DEFAULT_PORT_FTP = 21
DEFAULT_PORT_SFTP = 22
DEFAULT_DIRECTORY = "/"
DEFAULT_FILE_PATTERN = "0080A3DB81A5_*.xml"
DEFAULT_POLL_INTERVAL_MINUTES = 15
DEFAULT_REQUIRE_RDY = True
DEFAULT_DELETE_AFTER = False

STORAGE_VERSION = 1
STORAGE_KEY_TEMPLATE = f"{DOMAIN}.{{entry_id}}"

#: SQLite archive of every decoded reading, under /config/energy_meter_izar/.
#: Billing queries arbitrary past periods from this file, so it is
#: deliberately kept out of HA's recorder database and its purge cycle.
READINGS_DB_FILENAME = "readings.db"

#: Billing configuration and bill output, under /config/energy_meter_izar/.
BILLING_CONFIG_FILENAME = "billing.yaml"
BILLS_SUBDIR = "bills"

SERVICE_GENERATE_BILL = "generate_bill"
ATTR_START = "start"
ATTR_END = "end"
ATTR_PROFILE = "profile"
ATTR_FORMATS = "formats"

EVENT_BILL_GENERATED = f"{DOMAIN}_bill_generated"
