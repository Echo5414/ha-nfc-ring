"""NFC Ring: reports scans from Thread NFC rings as Home Assistant tag scans.

Each ring sends a confirmable CoAP POST /nfc over Thread when it reads a tag,
and a POST /hello with its firmware version after joining the network. This
integration listens on UDP port 5683 of the Home Assistant host, filters
retransmissions, and calls the tag integration exactly like the companion
app does. The ring only gets its 2.04 once the scan is recorded.

Firmware updates: an update entity per ring offers the newest image from the
project's GitHub releases or <config>/nfc_ring_firmware (see firmware.py).
Installing it sends the ring a POST /dfu; the ring then downloads the image
block-wise (GET /fw/<device id>) and restarts.
MCUboot on the ring only boots images signed with our key, and a new image
that never reaches Home Assistant is rolled back by the ring itself.

Pairing: a ring only counts once its pairing code (printed on its packaging)
was entered here. From then on each of its messages must carry a valid
signature and a fresh counter (see auth.py), and it only accepts update
notices signed by us. Rings that were already known before pairing existed
and still run firmware older than 0.4.0 keep working unsigned until they are
updated and paired; the overview page marks them.

Radios: from firmware 0.5.0 on a ring can use Bluetooth instead of Thread.
It then sends scans as signed advertisements, and for everything longer
(hello, settings, firmware) Home Assistant connects to it (ble.py for the
protocol, ble_link.py for the connection). Which radio a ring uses is a
setting sent to it encrypted (async_set_radio); a hello says which board
the ring is, so each ring is only offered firmware built for it.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import timedelta
from enum import Enum, auto
import json
import logging
import os
from pathlib import Path
import random
import socket
import time

from homeassistant.components.tag import async_scan_tag
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryNotReady, HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.storage import Store

from . import auth, ble, coap, firmware as fw, panel
from .const import (
    DOMAIN,
    LINK_BLUETOOTH,
    LINK_THREAD,
    RADIO_BLUETOOTH,
    RADIO_THREAD,
    RADIO_ZIGBEE,
    FIRMWARE_DIR,
    FIRMWARE_REPOSITORY,
    RING_PORT,
    SIGNAL_FIRMWARE_CHANGED,
    SIGNAL_RING_ADDED,
    SIGNAL_RING_CHANGED,
)

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.SELECT, Platform.UPDATE]

MAX_RINGS = 16

# Unknown rings that said hello and wait for their code. Kept in memory only;
# anyone on the network can fill this list, so it is small and never stored.
MAX_PENDING_RINGS = 16

# A code that matched no ring yet is tried on the next hellos for this long.
_CODE_WAIT_SECONDS = 600
_MAX_WAITING_CODES = 4

# struct in6_pktinfo: 16-byte address + 4-byte interface index
_PKTINFO_SIZE = 20

# IPv6 minimum MTU; anything larger is not a ring message.
_MAX_DATAGRAM = 1280

# Datagrams handled per wakeup before yielding back to the event loop.
_MAX_DATAGRAMS_PER_WAKEUP = 32

_STORAGE_VERSION = 1
_STORAGE_SAVE_DELAY = 10

# The local folder is cheap to scan; GitHub is asked far less often (its API
# allows 60 anonymous requests per hour) and on "check for updates".
_FIRMWARE_SCAN_INTERVAL = timedelta(minutes=10)
_RELEASE_CHECK_SECONDS = 6 * 3600
_RELEASE_RETRY_SECONDS = 15 * 60

# Downloaded release images, below FIRMWARE_DIR and apart from hand-placed ones.
_RELEASE_CACHE = "releases"

# Retransmission timeouts for our own confirmable requests. Rings are sleepy
# and only hear us when they poll their parent (every few seconds).
_REQUEST_TIMEOUTS = (3, 6, 12)

# An update fails if the download makes no progress this long, if the ring
# has not confirmed the new version this long after the last block (it
# rolls back on its own after 5 minutes), or after the overall deadline.
_DOWNLOAD_STALL_SECONDS = 60
_INSTALL_SECONDS = 600
_UPDATE_DEADLINE_SECONDS = 20 * 60

# How long a ring that uses Bluetooth may take to be reached: it has to be
# in range, and Home Assistant connects to one ring at a time.
_BLUETOOTH_REACH_SECONDS = 60

type NfcRingConfigEntry = ConfigEntry[RingServer]


@dataclass
class Candidate:
    """A signed hello of an unpaired ring, kept to check a pairing code against."""

    text: str
    mac: str
    boot: int
    n: int


@dataclass
class Ring:
    address: str | None = None  # the ring's last source IPv6 address
    local: str | None = None  # our address the ring talks to (its "server")
    fw: str | None = None  # firmware version from the last hello
    sid: int | None = None  # boot session from the last hello
    # None: known from before pairing existed and not paired yet.
    key: bytes | None = None
    replay: auth.ReplayWindow = field(default_factory=auth.ReplayWindow)
    candidate: Candidate | None = None  # only while unpaired
    # From a hello of firmware 0.5.0 on. board None: older firmware, which
    # only existed for one board. radio None: older firmware, always Thread.
    board: str | None = None
    variant: str | None = None  # the build: which radios the firmware contains
    radio: str | None = None  # the radio the ring uses
    link: str = LINK_THREAD  # how its last hello arrived
    ble: str | None = None  # its Bluetooth address, once it was seen there
    # The radio chosen in Home Assistant, until the ring reports it.
    wanted: str | None = None
    cfg_ctr: int = 0  # counter of the last settings message sent


@dataclass
class PendingRing:
    """An unknown ring that said hello; it is refused until its code is entered."""

    address: str | None
    local: str | None
    fw: str
    seen: float  # time.time()
    candidate: Candidate | None  # None: firmware too old to pair
    link: str = LINK_THREAD
    ble: str | None = None


class _Verdict(Enum):
    FRESH = auto()  # act on it
    REPEAT = auto()  # a copy of a message already acted on
    REJECT = auto()  # unsigned, wrongly signed or too old


class _HelloResult(Enum):
    ACCEPTED = auto()
    REPEAT = auto()  # heard before; answer it the same way again
    REFUSED = auto()  # not paired with us


def _hello_text(hello: coap.Hello) -> str | None:
    """The text a hello's signature covers; None for an unsigned hello."""
    if hello.mac is None:
        return None
    if hello.board is not None:
        return auth.hello2_text(
            hello.dev, hello.fw, hello.board, hello.variant, hello.radio,
            hello.sid, hello.confirmed, hello.boot, hello.n,
        )
    return auth.hello_text(hello.dev, hello.fw, hello.sid, hello.confirmed, hello.boot, hello.n)


def _compact(obj: dict) -> bytes:
    """JSON the way the ring reads it: no spaces."""
    return json.dumps(obj, separators=(",", ":")).encode()


@dataclass
class UpdateSession:
    version: str
    start_sid: int | None  # ring boot that received the notice
    done: asyncio.Future[str | None]  # None on success, else the reason it failed
    image: bytes = b""
    progress: int = 0  # percent of the image served
    next_offset: int = 0  # end of the furthest block served
    downloaded_at: float | None = None
    started: float = field(default_factory=time.monotonic)
    last_progress: float = field(default_factory=time.monotonic)
    # Over Bluetooth: the notice to deliver once connected, and the ring's
    # answer to it (a ble.DFU_* code, None if it did not answer).
    notice: bytes | None = None
    notice_answer: asyncio.Future[int | None] | None = None


async def async_setup_entry(hass: HomeAssistant, entry: NfcRingConfigEntry) -> bool:
    """Start listening for rings."""
    try:
        server = RingServer(hass, entry)
    except OSError as err:
        raise ConfigEntryNotReady(f"Cannot listen on UDP port {RING_PORT}: {err}") from err

    try:
        await server.async_load()
    except Exception:
        server.close()
        raise

    entry.runtime_data = server
    _start_bluetooth(hass, server)
    await panel.async_setup(hass)
    panel.async_apply(hass, entry)
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    entry.async_on_unload(
        async_track_time_interval(hass, server.async_scan_firmware, _FIRMWARE_SCAN_INTERVAL)
    )
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_create_background_task(
        hass, server.async_scan_firmware(), f"{DOMAIN} firmware release check"
    )
    return True


def _start_bluetooth(hass: HomeAssistant, server: RingServer) -> None:
    """Listens for rings over Bluetooth if Home Assistant has Bluetooth set up."""
    if "bluetooth" not in hass.config.components:
        return
    try:
        from . import ble_link  # noqa: PLC0415
    except ImportError as err:
        _LOGGER.warning("Rings cannot use Bluetooth here: %s", err)
        return
    server.ble = ble_link.BleLink(hass, server)
    server.ble.start()


async def _async_options_updated(hass: HomeAssistant, entry: NfcRingConfigEntry) -> None:
    """The only option is the sidebar page; no reload needed."""
    panel.async_apply(hass, entry)


async def async_remove_config_entry_device(
    hass: HomeAssistant, entry: NfcRingConfigEntry, device: dr.DeviceEntry
) -> bool:
    """Delete on the device page: forgets the ring and its key.

    The ring is then refused like any unknown one until its code is entered
    again.
    """
    server = entry.runtime_data
    devs = [dev for domain, dev in device.identifiers if domain == DOMAIN]
    if any(dev in server.updates for dev in devs):
        return False
    for dev in devs:
        server.forget_ring(dev)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: NfcRingConfigEntry) -> bool:
    """Stop listening."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        panel.async_remove(hass)
        entry.runtime_data.close()
    return unloaded


class RingServer:
    """Talks CoAP with the rings: scans, hellos and firmware updates.

    The Home Assistant host is the Thread border router and has addresses on
    both the LAN and the Thread interface. OpenThread drops a response that
    does not come from the address it sent the request to, and the ring only
    accepts update notices from that address, so we always send from the
    address the ring talks to (IPV6_PKTINFO), which a plain asyncio datagram
    endpoint cannot do.
    """

    def __init__(self, hass: HomeAssistant, entry: NfcRingConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self.duplicates = coap.DuplicateFilter()
        self.rings: dict[str, Ring] = {}
        self.pending: dict[str, PendingRing] = {}  # oldest first
        self._codes: dict[str, float] = {}  # entered code -> time.monotonic() it expires
        # Newest of local and release, per board and build.
        self.firmwares: dict[fw.Target, fw.Firmware] = {}
        self._local_firmware: dict[fw.Target, fw.Firmware] = {}
        self._release_firmware: dict[fw.Target, fw.Firmware] = {}
        self._release_checked: float | None = None
        # Serializes scans, GitHub checks and release downloads.
        self._firmware_lock = asyncio.Lock()
        self.updates: dict[str, UpdateSession] = {}
        self.ble = None  # ble_link.BleLink while Bluetooth is in use
        self._applying: set[str] = set()  # rings a settings message is on its way to
        self._provisioned: set[str] = set()  # rings given the Thread network unasked
        self._store: Store[dict[str, dict]] = Store(
            hass, _STORAGE_VERSION, f"{DOMAIN}.rings", private=True  # holds the ring keys
        )
        # mid -> (peer address, token, response code future)
        self._requests: dict[int, tuple[str, bytes, asyncio.Future[int]]] = {}
        self._next_mid = random.randrange(0x10000)

        self.sock = socket.socket(socket.AF_INET6, socket.SOCK_DGRAM)
        try:
            self.sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
            self.sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_RECVPKTINFO, 1)
            self.sock.setblocking(False)
            self.sock.bind(("::", RING_PORT))
            hass.loop.add_reader(self.sock.fileno(), self._on_readable)
        except OSError:
            self.sock.close()
            raise

    async def async_load(self) -> None:
        stored = await self._store.async_load() or {}
        for dev, data in list(stored.items())[:MAX_RINGS]:
            # A ring heard while loading is newer than what was stored.
            ring = Ring(
                address=data.get("address"),
                local=data.get("local"),
                fw=data.get("fw"),
                sid=data.get("sid"),
                board=data.get("board"),
                variant=data.get("variant"),
                radio=data.get("radio"),
                link=LINK_BLUETOOTH if data.get("link") == LINK_BLUETOOTH else LINK_THREAD,
                ble=data.get("ble"),
                wanted=data.get("wanted"),
            )
            try:
                if data.get("key"):
                    ring.key = bytes.fromhex(data["key"])
                    ring.replay = auth.ReplayWindow(
                        int(data["boot"]), int(data["top"]), int(data["mask"], 16)
                    )
            except (KeyError, TypeError, ValueError):
                # Without its counters a key would accept replays: pair again.
                _LOGGER.error("Stored pairing of ring %s is damaged; pair it again", dev)
                ring.key = None
            self.rings.setdefault(dev, ring)
        # GitHub is checked in the background once setup is done.
        await self.async_scan_firmware(skip_release_check=True)

    def close(self) -> None:
        if self.ble is not None:
            self.ble.stop()
            self.ble = None
        self.hass.loop.remove_reader(self.sock.fileno())
        self.sock.close()
        for _, _, future in self._requests.values():
            future.cancel()
        for session in self.updates.values():
            if not session.done.done():
                session.done.set_result("NFC Ring integration was unloaded")

    # Rings and firmware

    @callback
    def _ring_seen(
        self,
        dev: str,
        address: str | None,
        local: str | None,
        hello: coap.Hello | None = None,
        *,
        link: str = LINK_THREAD,
        ble_address: str | None = None,
    ) -> None:
        """Records where a known ring is and, from a hello, what it runs."""
        ring = self.rings[dev]
        before = (ring.address, ring.local, ring.fw, ring.radio, ring.link, ring.wanted, ring.board)
        if address:
            ring.address = address
        if local:
            ring.local = local
        if ble_address:
            ring.ble = ble_address
        if hello is not None:
            ring.fw = hello.fw
            ring.sid = hello.sid
            ring.link = link
            if hello.board is not None:
                # Older firmware does not say; the board stays what it was.
                ring.board = hello.board
            ring.variant = hello.variant
            ring.radio = hello.radio
            if ring.wanted == (ring.radio or RADIO_THREAD) and not (
                link == LINK_BLUETOOTH and ring.wanted == RADIO_THREAD
            ):
                # It has switched. (A ring set to Thread that comes in over
                # Bluetooth has not found the network: it still needs it.)
                ring.wanted = None

        self._ensure_device(dev)
        self._store.async_delay_save(self._stored_rings, _STORAGE_SAVE_DELAY)
        after = (ring.address, ring.local, ring.fw, ring.radio, ring.link, ring.wanted, ring.board)
        if after != before:
            async_dispatcher_send(self.hass, SIGNAL_RING_CHANGED, dev)

    @callback
    def _stored_rings(self) -> dict[str, dict]:
        stored = {}
        for dev, ring in self.rings.items():
            data: dict = {
                "address": ring.address,
                "local": ring.local,
                "fw": ring.fw,
                "sid": ring.sid,
                "board": ring.board,
                "variant": ring.variant,
                "radio": ring.radio,
                "link": ring.link,
                "ble": ring.ble,
                "wanted": ring.wanted,
            }
            if ring.key is not None:
                data |= {
                    "key": ring.key.hex(),
                    "boot": ring.replay.boot,
                    "top": ring.replay.top,
                    "mask": f"{ring.replay.mask:x}",
                }
            stored[dev] = data
        return stored

    # Pairing

    @callback
    def _verdict(
        self, ring: Ring, text: str | None, message: coap.ScanEvent | coap.Hello
    ) -> _Verdict:
        """Checks a known ring's message; the caller accepts what is FRESH."""
        if ring.key is None:
            return _Verdict.FRESH
        if text is None or not auth.verify(ring.key, text, message.mac):
            return _Verdict.REJECT
        freshness = ring.replay.check(message.boot, message.n)
        if freshness is auth.Freshness.OLD:
            return _Verdict.REJECT
        return _Verdict.REPEAT if freshness is auth.Freshness.SEEN else _Verdict.FRESH

    @callback
    def _pair(self, dev: str, key: bytes, known: PendingRing | None = None) -> Ring:
        ring = self.rings.get(dev)
        added = ring is None
        if added:
            if len(self.rings) >= MAX_RINGS:
                raise HomeAssistantError(f"Already {MAX_RINGS} rings; delete one first")
            ring = self.rings[dev] = Ring()
            if known is not None:
                ring.address, ring.local, ring.fw = known.address, known.local, known.fw
                ring.link, ring.ble = known.link, known.ble
        ring.key = key
        ring.replay = auth.ReplayWindow()
        ring.candidate = None
        self.pending.pop(dev, None)

        self._ensure_device(dev)
        self._store.async_delay_save(self._stored_rings, 0)
        async_dispatcher_send(
            self.hass, *((SIGNAL_RING_ADDED,) if added else (SIGNAL_RING_CHANGED, dev))
        )
        _LOGGER.info("Ring %s is paired", dev)
        if self.ble is not None:
            # A ring on Bluetooth waits for us to connect and answer its hello.
            self.ble.kick(dev)
        return ring

    @callback
    def async_pair(self, text: str) -> str | None:
        """Pairs the ring a code belongs to.

        Returns its device id, or None if no ring that said hello fits: the
        code then stays valid for a few minutes and is tried on every hello.
        Raises ValueError if text cannot be a code.
        """
        code = auth.normalize_code(text)
        if code is None:
            raise ValueError("not a pairing code")

        waiting: list[tuple[str, Candidate, PendingRing | None]] = [
            (dev, p.candidate, p) for dev, p in self.pending.items() if p.candidate
        ]
        waiting += [
            (dev, r.candidate, None)
            for dev, r in self.rings.items()
            if r.key is None and r.candidate
        ]
        for dev, candidate, known in waiting:
            key = auth.derive_key(code, dev)
            if auth.verify(key, candidate.text, candidate.mac):
                ring = self._pair(dev, key, known)
                # The hello that proved the code is used up.
                ring.replay.accept(candidate.boot, candidate.n)
                return dev

        self._codes.pop(code, None)
        while len(self._codes) >= _MAX_WAITING_CODES:
            del self._codes[next(iter(self._codes))]
        self._codes[code] = time.monotonic() + _CODE_WAIT_SECONDS
        return None

    @property
    def waiting_for_ring(self) -> bool:
        """True while an entered code waits for its ring to say hello."""
        now = time.monotonic()
        return any(expires > now for expires in self._codes.values())

    @callback
    def _key_from_waiting_code(self, dev: str, text: str, mac: str) -> bytes | None:
        now = time.monotonic()
        for code, expires in list(self._codes.items()):
            if expires <= now:
                del self._codes[code]
                continue
            key = auth.derive_key(code, dev)
            if auth.verify(key, text, mac):
                del self._codes[code]
                return key
        return None

    @callback
    def forget_ring(self, dev: str) -> None:
        if self.rings.pop(dev, None) is not None:
            self._store.async_delay_save(self._stored_rings, 0)

    @callback
    def _ensure_device(self, dev: str) -> dr.DeviceEntry:
        return dr.async_get(self.hass).async_get_or_create(
            config_entry_id=self.entry.entry_id,
            identifiers={(DOMAIN, dev)},
            name=f"NFC Ring {dev[-4:].upper()}",
            manufacturer="DIY",
            model="NFC Ring prototype",
            sw_version=self.rings[dev].fw,
            hw_version=self.rings[dev].board,
        )

    @property
    def _firmware_dir(self) -> Path:
        return Path(self.hass.config.path(FIRMWARE_DIR))

    @property
    def _release_cache_dir(self) -> Path:
        return self._firmware_dir / _RELEASE_CACHE

    async def async_scan_firmware(
        self, _now=None, *, force_release_check: bool = False, skip_release_check: bool = False
    ) -> None:
        """Finds the newest images: the local folder always, GitHub when due."""
        async with self._firmware_lock:
            self._local_firmware = await self.hass.async_add_executor_job(
                fw.find_local, self._firmware_dir
            )

            now = time.monotonic()
            if not skip_release_check and (
                force_release_check
                or self._release_checked is None
                or now - self._release_checked >= _RELEASE_CHECK_SECONDS
            ):
                try:
                    release = await fw.async_fetch_latest_release(self.hass, FIRMWARE_REPOSITORY)
                except fw.FirmwareError as err:
                    _LOGGER.warning("Firmware release check failed: %s", err)
                    # Try again soon rather than in six hours.
                    self._release_checked = now - _RELEASE_CHECK_SECONDS + _RELEASE_RETRY_SECONDS
                else:
                    self._release_checked = now
                    self._release_firmware = release

            if self._release_firmware:
                self._release_firmware = await self.hass.async_add_executor_job(
                    self._cached_releases, dict(self._release_firmware)
                )

            self._select_firmware()

    def _cached_releases(self, releases: dict[fw.Target, fw.Firmware]) -> dict[fw.Target, fw.Firmware]:
        """The releases with their paths set where already downloaded (blocking)."""
        return {
            target: fw.cached_release(self._release_cache_dir, firmware)
            for target, firmware in releases.items()
        }

    @callback
    def _select_firmware(self) -> None:
        firmwares = {}
        for target in self._local_firmware.keys() | self._release_firmware.keys():
            firmwares[target] = fw.newest(
                self._local_firmware.get(target), self._release_firmware.get(target)
            )
        if firmwares != self.firmwares:
            self.firmwares = firmwares
            async_dispatcher_send(self.hass, SIGNAL_FIRMWARE_CHANGED)

    @callback
    def _target(self, ring: Ring) -> fw.Target:
        """Board and build of the firmware this ring should run."""
        board = ring.board or coap.LEGACY_BOARD
        if ring.wanted == RADIO_ZIGBEE:
            return board, RADIO_ZIGBEE
        return board, ring.variant or coap.LEGACY_VARIANT

    @callback
    def firmware_for(self, dev: str) -> fw.Firmware | None:
        """The newest image built for this ring's board, whatever it runs now."""
        ring = self.rings.get(dev)
        return self.firmwares.get(self._target(ring)) if ring is not None else None

    @callback
    def offered_firmware(self, dev: str) -> fw.Firmware | None:
        """The image to install on this ring, or None if it is up to date."""
        ring = self.rings.get(dev)
        firmware = self.firmware_for(dev)
        if ring is None or firmware is None:
            return None
        if firmware.variant != (ring.variant or coap.LEGACY_VARIANT):
            return firmware  # another build was chosen: any version of it
        # Never offer an older image than the one running.
        if ring.fw is not None and coap.version_key(firmware.version) <= coap.version_key(ring.fw):
            return None
        return firmware

    async def _async_firmware_file(self, firmware: fw.Firmware) -> fw.Firmware:
        """The firmware with a local file, downloading a release if needed."""
        if firmware.path is not None:
            return firmware

        async with self._firmware_lock:
            # Another install may have downloaded it meanwhile.
            firmware = await self.hass.async_add_executor_job(
                fw.cached_release, self._release_cache_dir, firmware
            )
            if firmware.path is None:
                try:
                    firmware = await fw.async_download(
                        self.hass, firmware, self._release_cache_dir
                    )
                except fw.FirmwareError as err:
                    raise HomeAssistantError(
                        f"Could not get firmware {firmware.version}: {err}"
                    ) from err
            current = self._release_firmware.get(firmware.target)
            if current is not None and current.version == firmware.version:
                self._release_firmware[firmware.target] = firmware
                self._select_firmware()
        return firmware

    # Receiving

    def _on_readable(self) -> None:
        for _ in range(_MAX_DATAGRAMS_PER_WAKEUP):
            try:
                data, ancdata, flags, addr = self.sock.recvmsg(
                    _MAX_DATAGRAM, socket.CMSG_SPACE(_PKTINFO_SIZE)
                )
            except (BlockingIOError, InterruptedError):
                return
            except OSError as err:
                _LOGGER.warning("Receiving from a ring failed: %s", err)
                return

            if flags & (socket.MSG_TRUNC | socket.MSG_CTRUNC):
                continue

            local_addr = next(
                (
                    cmsg_data[:16]
                    for level, cmsg_type, cmsg_data in ancdata
                    if level == socket.IPPROTO_IPV6
                    and cmsg_type == socket.IPV6_PKTINFO
                    and len(cmsg_data) >= _PKTINFO_SIZE
                ),
                None,
            )
            self._on_datagram(data, addr, local_addr)

    def _on_datagram(self, data: bytes, addr: tuple, local_addr: bytes | None) -> None:
        try:
            msg = coap.parse(data)
        except ValueError:
            return

        if msg.type in (coap.TYPE_ACK, coap.TYPE_RST):
            self._on_response(msg, addr)
            return

        # Rings send confirmable requests only; NON is ignored.
        if msg.type != coap.TYPE_CON:
            return

        if msg.unsupported_critical_option:
            self._reply(coap.build_ack(msg, coap.CODE_BAD_OPTION), addr, local_addr)
            return

        if msg.path == coap.SCAN_PATH and msg.code == coap.CODE_POST:
            self._on_scan(msg, addr, local_addr)
        elif msg.path == coap.HELLO_PATH and msg.code == coap.CODE_POST:
            self._on_hello(msg, addr, local_addr)
        elif msg.path.startswith(coap.FIRMWARE_PATH + "/") and msg.code == coap.CODE_GET:
            self._on_firmware_block(msg, addr, local_addr)
        else:
            self._reply(coap.build_ack(msg, coap.CODE_NOT_FOUND), addr, local_addr)

    def _on_response(self, msg: coap.CoapMessage, addr: tuple) -> None:
        """Answer to one of our own requests (an update notice)."""
        pending = self._requests.get(msg.mid)
        if pending is None:
            return
        peer, token, future = pending
        if addr[0] != peer or future.done():
            return
        if msg.type == coap.TYPE_RST:
            future.set_result(coap.CODE_EMPTY)
        elif msg.token == token:
            # Piggybacked response; rings never send a separate one.
            future.set_result(msg.code)

    def _on_scan(self, msg: coap.CoapMessage, addr: tuple, local_addr: bytes | None) -> None:
        try:
            event = coap.parse_event(msg.payload)
        except ValueError as err:
            _LOGGER.debug("Invalid scan from %s: %s", addr[0], err)
            self._reply(coap.build_ack(msg, coap.CODE_BAD_REQUEST), addr, local_addr)
            return

        ring = self.rings.get(event.dev)
        text = None
        if event.mac is not None:
            text = auth.scan_text(event.dev, event.uid, event.sid, event.seq, event.boot, event.n)
        if ring is not None and ring.key is None and text is not None:
            # Known from before pairing, and its firmware signs: a scan can
            # prove an entered code just like a hello does.
            key = self._key_from_waiting_code(event.dev, text, event.mac)
            if key is not None:
                ring = self._pair(event.dev, key)
            else:
                ring.candidate = Candidate(text, event.mac, event.boot, event.n)
        if ring is None or (verdict := self._verdict(ring, text, event)) is _Verdict.REJECT:
            _LOGGER.debug("Refusing scan from %s as ring %s: not paired", addr[0], event.dev)
            self._reply(coap.build_ack(msg, coap.CODE_UNAUTHORIZED), addr, local_addr)
            return

        if verdict is _Verdict.REPEAT:
            # The same signed message again. Only a retransmission of a scan
            # we still track is answered; anything else is a replay.
            state = self.duplicates.peek(event.key)
            if state is coap.ScanState.DONE:
                self._reply(coap.build_ack(msg, coap.CODE_CHANGED), addr, local_addr)
            elif state is None:
                self._reply(coap.build_ack(msg, coap.CODE_UNAUTHORIZED), addr, local_addr)
            return

        # Only a fresh message may move the ring to a new address.
        self._ring_seen(
            event.dev,
            addr[0],
            socket.inet_ntop(socket.AF_INET6, local_addr) if local_addr else None,
        )
        if self._ensure_device(event.dev).disabled:
            # Deactivated in Home Assistant (device page): its scans do nothing.
            _LOGGER.debug("Ignoring scan from deactivated ring %s", event.dev)
            self._reply(coap.build_ack(msg, coap.CODE_FORBIDDEN), addr, local_addr)
            return

        state = self.duplicates.admit(event.key)
        if state is coap.ScanState.FULL:
            _LOGGER.debug("Too many scans in flight, refusing scan from %s", addr[0])
            self._reply(coap.build_ack(msg, coap.CODE_SERVICE_UNAVAILABLE), addr, local_addr)
            return
        if state is coap.ScanState.PENDING:
            # Still recording the first copy; the ring retries and gets the
            # real result then.
            return
        if ring.key is not None:
            ring.replay.accept(event.boot, event.n)
        if state is coap.ScanState.DONE:
            # Retransmission: the ring missed our first reply.
            self._reply(coap.build_ack(msg, coap.CODE_CHANGED), addr, local_addr)
        else:
            self.entry.async_create_background_task(
                self.hass, self._handle_scan(msg, event, addr, local_addr), f"{DOMAIN} scan"
            )

    @callback
    def _retry_allowed(self, event: coap.ScanEvent) -> None:
        """After a failed scan: lets the ring's retransmission try again."""
        self.duplicates.forget(event.key)
        ring = self.rings.get(event.dev)
        if ring is not None and ring.key is not None and event.n is not None:
            ring.replay.release(event.boot, event.n)

    async def _handle_scan(
        self, msg: coap.CoapMessage, event: coap.ScanEvent, addr: tuple, local_addr: bytes | None
    ) -> None:
        try:
            device = self._ensure_device(event.dev)
            await async_scan_tag(self.hass, event.uid, device.id)
        except HomeAssistantError as err:
            _LOGGER.error("Could not record scan of tag %s: %s", event.uid, err)
            self._retry_allowed(event)
            self._reply(coap.build_ack(msg, coap.CODE_INTERNAL_ERROR), addr, local_addr)
            return
        except BaseException:
            # Includes cancellation on unload: let a retransmission try again.
            self._retry_allowed(event)
            raise

        self.duplicates.finish(event.key)
        _LOGGER.debug("Ring %s scanned tag %s (seq %d)", event.dev, event.uid, event.seq)
        self._reply(coap.build_ack(msg, coap.CODE_CHANGED), addr, local_addr)

    def _on_hello(self, msg: coap.CoapMessage, addr: tuple, local_addr: bytes | None) -> None:
        try:
            hello = coap.parse_hello(msg.payload)
        except ValueError as err:
            _LOGGER.debug("Invalid hello from %s: %s", addr[0], err)
            self._reply(coap.build_ack(msg, coap.CODE_BAD_REQUEST), addr, local_addr)
            return

        local = socket.inet_ntop(socket.AF_INET6, local_addr) if local_addr else None
        result = self._process_hello(hello, link=LINK_THREAD, address=addr[0], local=local)
        if result is _HelloResult.REFUSED:
            _LOGGER.debug("Refusing hello from %s as ring %s: not paired", addr[0], hello.dev)
            self._reply(coap.build_ack(msg, coap.CODE_UNAUTHORIZED), addr, local_addr)
            return
        # Acknowledging a trial hello is what lets the ring confirm the image.
        self._reply(coap.build_ack(msg, coap.CODE_CHANGED), addr, local_addr)
        if result is _HelloResult.ACCEPTED and self._settings_due(hello.dev):
            # The radio was changed while the ring could not be told.
            self.entry.async_create_background_task(
                self.hass, self._async_apply_quietly(hello.dev), f"{DOMAIN} settings"
            )

    @callback
    def _process_hello(
        self,
        hello: coap.Hello,
        *,
        link: str,
        address: str | None = None,
        local: str | None = None,
        ble_address: str | None = None,
    ) -> _HelloResult:
        """Checks a hello from either radio and records what it says."""
        ring = self.rings.get(hello.dev)
        text = _hello_text(hello)
        candidate = Candidate(text, hello.mac, hello.boot, hello.n) if text is not None else None

        if (ring is None or ring.key is None) and text is not None:
            # Its code may have been entered before the ring was heard.
            key = self._key_from_waiting_code(hello.dev, text, hello.mac)
            if key is not None:
                known = PendingRing(address, local, hello.fw, time.time(), None, link, ble_address)
                try:
                    ring = self._pair(hello.dev, key, known)
                except HomeAssistantError as err:
                    _LOGGER.warning("Cannot pair ring %s: %s", hello.dev, err)

        if ring is None:
            self.pending.pop(hello.dev, None)
            while len(self.pending) >= MAX_PENDING_RINGS:
                del self.pending[next(iter(self.pending))]
            self.pending[hello.dev] = PendingRing(
                address, local, hello.fw, time.time(), candidate, link, ble_address
            )
            return _HelloResult.REFUSED

        if ring.key is None and link == LINK_BLUETOOTH:
            # Over Bluetooth our answer is signed, and that takes the key.
            ring.candidate = candidate
            return _HelloResult.REFUSED

        verdict = self._verdict(ring, text, hello)
        if verdict is _Verdict.REJECT:
            return _HelloResult.REFUSED
        if verdict is _Verdict.REPEAT:
            return _HelloResult.REPEAT
        if ring.key is not None:
            ring.replay.accept(hello.boot, hello.n)
        else:
            ring.candidate = candidate

        self._ring_seen(hello.dev, address, local, hello, link=link, ble_address=ble_address)

        session = self.updates.get(hello.dev)
        if session is None or session.done.done() or not session.image:
            # No update, or still preparing it (the ring has not been told yet).
            return _HelloResult.ACCEPTED
        if hello.sid == session.start_sid or (
            session.start_sid is None and session.downloaded_at is None
        ):
            # Still the boot that is downloading; nothing installed yet. (Without
            # a known boot session, only hellos after the last block can count.)
            return _HelloResult.ACCEPTED
        if hello.fw != session.version:
            session.done.set_result(
                f"ring restarted with {hello.fw} instead of {session.version}: "
                "the image was rejected or rolled back"
            )
        elif hello.confirmed:
            session.done.set_result(None)
        # else: new image on trial; the ring confirms it now and says hello again.
        return _HelloResult.ACCEPTED

    def _on_firmware_block(
        self, msg: coap.CoapMessage, addr: tuple, local_addr: bytes | None
    ) -> None:
        dev = msg.path.removeprefix(coap.FIRMWARE_PATH + "/")
        session = self.updates.get(dev)
        ring = self.rings.get(dev)
        if (
            session is None
            or session.done.done()
            or not session.image
            or ring is None
            or addr[0] != ring.address
        ):
            self._reply(coap.build_ack(msg, coap.CODE_NOT_FOUND), addr, local_addr)
            return

        try:
            block = coap.decode_block(msg.block2 or 0)
        except ValueError:
            self._reply(coap.build_ack(msg, coap.CODE_BAD_REQUEST), addr, local_addr)
            return

        szx = min(block.szx, coap.MAX_BLOCK_SZX)
        size = 16 << szx
        start = block.num * size
        if start >= len(session.image):
            self._reply(coap.build_ack(msg, coap.CODE_BAD_REQUEST), addr, local_addr)
            return

        data = session.image[start : start + size]
        end = start + len(data)
        more = end < len(session.image)
        response = coap.build_block_response(msg, coap.Block(block.num, more, szx), data)
        self._reply(response, addr, local_addr)

        self._served(dev, session, end)

    @callback
    def _served(self, dev: str, session: UpdateSession, end: int) -> None:
        """Notes that the image was served up to end."""
        # Only forward progress counts; repeated requests for old blocks do
        # not keep a stuck download alive.
        if end > session.next_offset:
            session.next_offset = end
            session.last_progress = time.monotonic()
            if end >= len(session.image):
                session.downloaded_at = session.last_progress
            progress = end * 100 // len(session.image)
            if progress != session.progress:
                session.progress = progress
                async_dispatcher_send(self.hass, SIGNAL_RING_CHANGED, dev)

    # Sending

    def _reply(self, payload: bytes, addr: tuple, local_addr: bytes | None) -> None:
        self._send(payload, addr, local_addr)

    def _send(self, payload: bytes, addr: tuple, local_addr: bytes | None) -> None:
        if self.sock.fileno() < 0:
            return
        ancdata = []
        if local_addr:
            # Interface index 0: only pin the source address, let routing pick the interface.
            pktinfo = local_addr + bytes(4)
            ancdata.append((socket.IPPROTO_IPV6, socket.IPV6_PKTINFO, pktinfo))
        try:
            self.sock.sendmsg([payload], ancdata, 0, addr)
        except OSError as err:
            _LOGGER.debug("Could not send to ring at %s: %s", addr[0], err)

    async def _async_request(
        self, ring: Ring, build: Callable[[int, bytes], bytes]
    ) -> int | None:
        """Sends a confirmable request built by build(mid, token) to a ring.

        Returns the response code, or None if the ring never answered.
        """
        assert ring.address is not None
        mid = self._next_mid
        self._next_mid = (mid + 1) & 0xFFFF
        token = os.urandom(4)
        payload = build(mid, token)
        local_addr = socket.inet_pton(socket.AF_INET6, ring.local) if ring.local else None
        future: asyncio.Future[int] = self.hass.loop.create_future()
        self._requests[mid] = (ring.address, token, future)
        try:
            for timeout in _REQUEST_TIMEOUTS:
                self._send(payload, (ring.address, RING_PORT), local_addr)
                try:
                    return await asyncio.wait_for(asyncio.shield(future), timeout)
                except TimeoutError:
                    continue
            return None
        finally:
            self._requests.pop(mid, None)

    # Updates

    async def async_update_ring(self, dev: str) -> None:
        """Installs the newest firmware on a ring; raises if it fails."""
        ring = self.rings.get(dev)
        if ring is None or (ring.address is None and ring.link != LINK_BLUETOOTH):
            raise HomeAssistantError(
                "This ring has not checked in yet; press its button or wait for its next hello"
            )
        if dev in self.updates:
            raise HomeAssistantError("An update for this ring is already running")

        # Reserve the ring before the first await so a second install cannot
        # start in between.
        session = UpdateSession("", ring.sid, self.hass.loop.create_future())
        self.updates[dev] = session
        async_dispatcher_send(self.hass, SIGNAL_RING_CHANGED, dev)
        try:
            await self._async_run_update(dev, ring, session)
        finally:
            if self.updates.get(dev) is session:
                del self.updates[dev]
            async_dispatcher_send(self.hass, SIGNAL_RING_CHANGED, dev)

    async def _async_run_update(self, dev: str, ring: Ring, session: UpdateSession) -> None:
        await self.async_scan_firmware()
        firmware = self.firmware_for(dev)
        if firmware is None:
            board, variant = self._target(ring)
            raise HomeAssistantError(
                f"No firmware for this ring ({board}, {variant}) found in "
                f"{FIRMWARE_REPOSITORY} releases or {FIRMWARE_DIR}"
            )

        firmware = await self._async_firmware_file(firmware)
        image = await self.hass.async_add_executor_job(firmware.path.read_bytes)
        try:
            if coap.image_version(image) != firmware.version:
                raise ValueError("file changed while reading")
        except ValueError as err:
            raise HomeAssistantError(f"{firmware.path.name}: {err}") from err
        session.version = firmware.version
        # Hellos only count from here on: a boot that started before the
        # notice is the one that will download.
        session.start_sid = ring.sid
        session.image = image

        mac = None
        if ring.key is not None:
            if ring.sid is None:
                raise HomeAssistantError(
                    "This ring has not checked in yet; press its button or wait for its next hello"
                )
            mac = auth.mac(
                ring.key, auth.notify_text(dev, ring.sid, firmware.version, len(image))
            )
        if ring.link == LINK_BLUETOOTH:
            answer = await self._async_notify_bluetooth(dev, session, firmware, mac)
            unauthorized = answer == ble.DFU_UNAUTHORIZED
            refused = None if answer == ble.DFU_ACCEPTED else f"code {answer}"
        else:
            code = await self._async_request(
                ring,
                lambda mid, token: coap.build_notify(mid, token, firmware.version, len(image), mac),
            )
            if code is None:
                raise HomeAssistantError("The ring did not answer the update notice")
            unauthorized = code == coap.CODE_UNAUTHORIZED
            refused = None if code >> 5 == 2 else f"CoAP {code >> 5}.{code & 0x1F:02d}"
        if unauthorized:
            raise HomeAssistantError(
                "The ring did not accept the update notice. Try again in a minute; it may "
                "have just restarted."
                if ring.key is not None
                else "This ring only takes updates from a Home Assistant it is paired with. "
                "Enter its pairing code on the NFC Ring page first."
            )
        if refused is not None:
            raise HomeAssistantError(
                f"The ring refused the update notice ({refused}); "
                "it may still be confirming a previous update or recovering from a failed one"
            )

        _LOGGER.info("Ring %s is downloading firmware %s", dev, firmware.version)
        session.last_progress = time.monotonic()
        if (error := await self._async_wait_for_install(session)) is not None:
            raise HomeAssistantError(f"Update to {firmware.version} failed: {error}")
        _LOGGER.info("Ring %s now runs firmware %s", dev, firmware.version)

    async def _async_notify_bluetooth(
        self, dev: str, session: UpdateSession, firmware: fw.Firmware, mac: str | None
    ) -> int:
        """Has the update notice delivered over Bluetooth; returns the ring's answer."""
        if self.ble is None or mac is None:
            raise HomeAssistantError(
                "This ring uses Bluetooth, which is not available in Home Assistant right now"
            )
        session.notice = _compact({"size": len(session.image), "ver": firmware.version, "mac": mac})
        session.notice_answer = self.hass.loop.create_future()
        self.ble.kick(dev)
        try:
            answer = await asyncio.wait_for(
                asyncio.shield(session.notice_answer), _BLUETOOTH_REACH_SECONDS
            )
        except TimeoutError:
            answer = None
        if answer is None:
            raise HomeAssistantError(
                "The ring could not be reached over Bluetooth. Bring it closer to Home "
                "Assistant's Bluetooth adapter and try again."
            )
        return answer

    async def _async_wait_for_install(self, session: UpdateSession) -> str | None:
        while True:
            try:
                return await asyncio.wait_for(asyncio.shield(session.done), 5)
            except TimeoutError:
                pass

            now = time.monotonic()
            if now - session.started > _UPDATE_DEADLINE_SECONDS:
                return "it took too long"
            if session.downloaded_at is None:
                if now - session.last_progress > _DOWNLOAD_STALL_SECONDS:
                    return f"download stalled at {session.progress}%"
            elif now - session.downloaded_at > _INSTALL_SECONDS:
                return "the ring did not report back after installing"

    # Radio

    @callback
    def radios_for(self, dev: str) -> list[str]:
        """The radios this ring can be switched to."""
        radios = [RADIO_THREAD, RADIO_BLUETOOTH]
        ring = self.rings.get(dev)
        if ring is not None and (ring.board or coap.LEGACY_BOARD, RADIO_ZIGBEE) in self.firmwares:
            radios.append(RADIO_ZIGBEE)
        return radios

    @property
    def bluetooth_available(self) -> bool:
        """True if Home Assistant can connect to a ring over Bluetooth."""
        return self.ble is not None and self.ble.can_connect

    @callback
    def _settings_due(self, dev: str) -> bool:
        """True if the ring has to be told its radio and can be told."""
        ring = self.rings.get(dev)
        return (
            ring is not None
            and ring.key is not None
            and ring.sid is not None
            and ring.radio is not None  # older firmware has no settings
            and ring.wanted in auth.CFG_RADIOS
        )

    @callback
    def _settings_taken(self, dev: str) -> None:
        """The ring confirmed the settings message; it switches a moment later."""
        ring = self.rings[dev]
        _LOGGER.info("Ring %s is switching to %s", dev, ring.wanted)
        ring.radio, ring.wanted = ring.wanted, None
        self._store.async_delay_save(self._stored_rings, 0)
        async_dispatcher_send(self.hass, SIGNAL_RING_CHANGED, dev)

    async def async_set_radio(self, dev: str, radio: str) -> bool:
        """Chooses the radio a ring uses.

        Returns True if the ring took the setting, False if it is noted and
        happens later: at the ring's next contact, or with the firmware
        update the ring needs first. Raises if it cannot work at all.
        """
        ring = self.rings.get(dev)
        if ring is None:
            raise HomeAssistantError("This ring is not set up")
        if radio == RADIO_ZIGBEE and radio not in self.radios_for(dev):
            raise HomeAssistantError("There is no Zigbee firmware for this ring yet")
        if radio not in self.radios_for(dev):
            raise HomeAssistantError(f"Unknown radio {radio}")
        if ring.key is None:
            raise HomeAssistantError("Pair this ring first; settings only go to a paired ring")
        if radio == RADIO_BLUETOOTH and not self.bluetooth_available:
            raise HomeAssistantError(
                "Home Assistant has no Bluetooth adapter that can connect to devices. "
                "Set up Bluetooth first (Settings, Devices & services)."
            )
        # Thread again for a ring that only gets through over Bluetooth: it
        # is given the network once more.
        again = radio == RADIO_THREAD and ring.link == LINK_BLUETOOTH and ring.radio is not None
        wanted = None if radio == (ring.radio or RADIO_THREAD) and not again else radio
        if wanted == RADIO_THREAD:
            await self._async_thread_settings(ring)  # raises without a Thread network
        ring.wanted = wanted
        self._store.async_delay_save(self._stored_rings, 0)
        async_dispatcher_send(self.hass, SIGNAL_RING_CHANGED, dev)
        if ring.wanted is None:
            return True
        if not self._settings_due(dev):
            return False  # takes a firmware update first
        try:
            await self._async_apply_radio(dev)
        except HomeAssistantError as err:
            _LOGGER.info("Ring %s switches to %s at its next contact: %s", dev, radio, err)
            return False
        return True

    async def _async_apply_quietly(self, dev: str) -> None:
        try:
            await self._async_apply_radio(dev)
        except HomeAssistantError as err:
            _LOGGER.warning("Could not switch the radio of ring %s: %s", dev, err)

    async def _async_apply_radio(self, dev: str) -> None:
        """Sends the ring its radio setting; raises if it did not take it."""
        if not self._settings_due(dev):
            return
        ring = self.rings[dev]
        if dev in self._applying:
            raise HomeAssistantError("the setting is already on its way")
        self._applying.add(dev)
        try:
            if ring.link == LINK_BLUETOOTH:
                if self.ble is None:
                    raise HomeAssistantError("Bluetooth is not available in Home Assistant")
                answer = await self.ble.async_send_settings(dev, _BLUETOOTH_REACH_SECONDS)
                if answer is None:
                    raise HomeAssistantError("the ring could not be reached over Bluetooth")
                if answer != ble.CFG_OK:
                    raise HomeAssistantError(f"the ring refused the setting (code {answer})")
                return  # ble_settings_answer has noted it
            else:
                if ring.address is None:
                    raise HomeAssistantError("the ring has not checked in yet")
                sealed = await self._async_sealed_settings(dev, ring)
                code = await self._async_request(
                    ring, lambda mid, token: coap.build_config(mid, token, sealed)
                )
                if code is None:
                    raise HomeAssistantError("the ring did not answer")
                if code >> 5 != 2:
                    raise HomeAssistantError(
                        f"the ring refused the setting (CoAP {code >> 5}.{code & 0x1F:02d})"
                    )
                self._settings_taken(dev)
        finally:
            self._applying.discard(dev)

    async def _async_sealed_settings(self, dev: str, ring: Ring) -> bytes:
        """The encrypted settings message that moves the ring to its chosen radio."""
        settings: dict[str, str] = {"radio": ring.wanted}
        if ring.wanted == RADIO_THREAD:
            settings |= await self._async_thread_settings(ring)
        ring.cfg_ctr = max(ring.cfg_ctr + 1, int(time.time())) & 0xFFFFFFFF
        return auth.seal_cfg(ring.key, dev, ring.sid, ring.replay.boot, ring.cfg_ctr, settings)

    async def _async_thread_settings(self, ring: Ring) -> dict[str, str]:
        """What a ring needs to join Home Assistant's Thread network and find us."""
        dataset = None
        if "thread" in self.hass.config.components:
            from homeassistant.components import thread  # noqa: PLC0415

            try:
                dataset = await thread.async_get_preferred_dataset(self.hass)
            except HomeAssistantError:
                dataset = None
        if not dataset:
            raise HomeAssistantError(
                "Home Assistant has no Thread network. Set up Thread with a border router "
                "first (Settings, Devices & services, Thread)."
            )
        server = ring.local or await self._async_thread_address()
        if server is None:
            raise HomeAssistantError(
                "Home Assistant has no address in the Thread network. It has to run on "
                "the Thread border router."
            )
        return {"dataset": dataset.lower(), "server": server}

    async def _async_thread_address(self) -> str | None:
        """Our address in the Thread network, for a ring that never was there."""
        known = [ring.local for ring in self.rings.values() if ring.local]
        if known:
            return max(set(known), key=known.count)
        from homeassistant.components import network  # noqa: PLC0415

        for adapter in await network.async_get_adapters(self.hass):
            if not str(adapter.get("name", "")).startswith("wpan"):
                continue
            for ip in adapter.get("ipv6") or []:
                address = str(ip.get("address", ""))
                # Not link-local, and not the locator, which changes with the topology.
                if address and not address.startswith("fe80") and "ff:fe00:" not in address:
                    return address
        return None

    # Bluetooth (called by ble_link.BleLink)

    @callback
    def ble_wanted(self, dev: str) -> bool:
        """True if there is something to tell this ring over a connection."""
        session = self.updates.get(dev)
        if session is not None and session.notice_answer is not None and not session.notice_answer.done():
            return True
        ring = self.rings.get(dev)
        return ring is not None and ring.link == LINK_BLUETOOTH and self._settings_due(dev)

    @callback
    def ble_known(self, dev: str) -> bool | None:
        """True for a paired ring, False for one of ours not paired, None for a stranger."""
        ring = self.rings.get(dev)
        return None if ring is None else ring.key is not None

    @callback
    def ble_hello(self, payload: bytes, ble_address: str) -> tuple[coap.Hello | None, bytes | None]:
        """A hello received over a connection: (the hello, our signed answer).

        The answer is None if the ring is not paired with us.
        """
        try:
            hello = coap.parse_hello(payload)
        except ValueError as err:
            _LOGGER.debug("Invalid hello from %s: %s", ble_address, err)
            return None, None
        result = self._process_hello(hello, link=LINK_BLUETOOTH, ble_address=ble_address)
        ring = self.rings.get(hello.dev)
        if result is _HelloResult.REFUSED or ring is None or ring.key is None:
            return hello, None
        mac = auth.mac(ring.key, auth.ack_text(hello.dev, hello.boot, hello.n))
        return hello, _compact({"st": "ok", "mac": mac})

    @callback
    def ble_scan(self, ble_address: str, scan: ble.ScanFrame, dev: str | None = None) -> None:
        """A scan from an advertisement or a connection.

        The frame does not name its ring: whichever paired ring's key fits
        the signature sent it.
        """
        if dev is not None:
            devs = [dev]
        else:
            # Most likely the ring last seen at this address.
            devs = sorted(self.rings, key=lambda d: self.rings[d].ble != ble_address)
        for candidate in devs:
            ring = self.rings.get(candidate)
            if ring is None or ring.key is None:
                continue
            text = auth.ble_scan_text(candidate, scan.uid, scan.boot, scan.n)
            if not auth.verify(ring.key, text, scan.mac):
                continue
            if ring.replay.check(scan.boot, scan.n) is not auth.Freshness.NEW:
                return  # the same scan again (it is repeated for a few seconds), or a replay
            ring.replay.accept(scan.boot, scan.n)
            ring.ble = ble_address
            self._store.async_delay_save(self._stored_rings, _STORAGE_SAVE_DELAY)
            if self._ensure_device(candidate).disabled:
                _LOGGER.debug("Ignoring scan from deactivated ring %s", candidate)
                return
            self.entry.async_create_background_task(
                self.hass, self._handle_ble_scan(candidate, scan.uid), f"{DOMAIN} scan"
            )
            return

    async def _handle_ble_scan(self, dev: str, uid: str) -> None:
        try:
            await async_scan_tag(self.hass, uid, self._ensure_device(dev).id)
        except HomeAssistantError as err:
            _LOGGER.error("Could not record scan of tag %s: %s", uid, err)
            return
        _LOGGER.debug("Ring %s scanned tag %s (Bluetooth)", dev, uid)

    async def async_ble_settings(self, dev: str) -> bytes | None:
        """The settings message due for a connected ring, or None."""
        ring = self.rings.get(dev)
        if (
            ring is not None
            and ring.key is not None
            and ring.radio == RADIO_THREAD
            and ring.wanted is None
            and dev not in self._provisioned
        ):
            # First setup: the ring is set to Thread but came in over
            # Bluetooth, so it does not have the network. Give it ours, once.
            self._provisioned.add(dev)
            try:
                await self._async_thread_settings(ring)
            except HomeAssistantError:
                pass  # no Thread here: the ring stays on Bluetooth
            else:
                ring.wanted = RADIO_THREAD
        if not self._settings_due(dev):
            return None
        try:
            return await self._async_sealed_settings(dev, self.rings[dev])
        except HomeAssistantError as err:
            _LOGGER.warning("Could not switch the radio of ring %s: %s", dev, err)
            return None

    @callback
    def ble_settings_answer(self, dev: str, answer: int | None) -> None:
        """The connected ring's answer to the settings message."""
        if answer == ble.CFG_OK and self._settings_due(dev):
            self._settings_taken(dev)
        elif answer is not None:
            _LOGGER.warning("Ring %s refused the radio setting (code %s)", dev, answer)

    @callback
    def ble_update(self, dev: str) -> UpdateSession | None:
        """The update whose notice still has to be delivered to a connected ring."""
        session = self.updates.get(dev)
        if (
            session is None
            or session.done.done()
            or session.notice is None
            or session.notice_answer is None
            or session.notice_answer.done()
        ):
            return None
        return session

    @callback
    def ble_served(self, dev: str, session: UpdateSession, end: int) -> None:
        self._served(dev, session, end)
