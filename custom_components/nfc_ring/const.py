"""Constants for the NFC Ring integration."""

DOMAIN = "nfc_ring"

# UDP port the rings send to and listen on (CoAP default).
RING_PORT = 5683

# Firmware images (MCUboot-signed .bin files) offered to the rings, relative
# to the Home Assistant config directory. The highest version wins. Release
# images are downloaded here before they are installed.
FIRMWARE_DIR = "nfc_ring_firmware"

# GitHub repository whose latest release carries the newest ring firmware.
FIRMWARE_REPOSITORY = "Echo5414/nfc-ring-releases"

# Dispatcher signals
SIGNAL_RING_ADDED = f"{DOMAIN}_ring_added"
SIGNAL_RING_CHANGED = f"{DOMAIN}_ring_changed"  # argument: device id
SIGNAL_FIRMWARE_CHANGED = f"{DOMAIN}_firmware_changed"
