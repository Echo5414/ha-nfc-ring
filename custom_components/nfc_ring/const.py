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

# Option: show the overview page in the sidebar (default: yes).
CONF_SHOW_PANEL = "show_panel"

# Options form only, never stored: pairs a ring when the page is switched off.
CONF_PAIRING_CODE = "pairing_code"

# Dispatcher signals
SIGNAL_RING_ADDED = f"{DOMAIN}_ring_added"
SIGNAL_RING_CHANGED = f"{DOMAIN}_ring_changed"  # argument: device id
SIGNAL_FIRMWARE_CHANGED = f"{DOMAIN}_firmware_changed"

# How a ring reaches Home Assistant, and the radios it can be switched to.
# Thread and Bluetooth are in one firmware build; Zigbee is a build of its own.
LINK_THREAD = "thread"
LINK_BLUETOOTH = "bluetooth"
RADIO_THREAD = "thread"
RADIO_BLUETOOTH = "bluetooth"
RADIO_ZIGBEE = "zigbee"
