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

Scans and hellos are not authenticated yet: any host that can reach this
port can report a tag scan or announce a ring (at most MAX_RINGS of them).
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import timedelta
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

from . import coap, firmware as fw
from .const import (
    DOMAIN,
    FIRMWARE_DIR,
    FIRMWARE_REPOSITORY,
    RING_PORT,
    SIGNAL_FIRMWARE_CHANGED,
    SIGNAL_RING_ADDED,
    SIGNAL_RING_CHANGED,
)

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.UPDATE]

# Until rings are paired with a key, this bounds how many devices bogus
# hellos or scans can create.
MAX_RINGS = 16

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

type NfcRingConfigEntry = ConfigEntry[RingServer]


@dataclass
class Ring:
    address: str | None = None  # the ring's last source IPv6 address
    local: str | None = None  # our address the ring talks to (its "server")
    fw: str | None = None  # firmware version from the last hello
    sid: int | None = None  # boot session from the last hello


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
    entry.async_on_unload(
        async_track_time_interval(hass, server.async_scan_firmware, _FIRMWARE_SCAN_INTERVAL)
    )
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_create_background_task(
        hass, server.async_scan_firmware(), f"{DOMAIN} firmware release check"
    )
    return True


async def async_unload_entry(hass: HomeAssistant, entry: NfcRingConfigEntry) -> bool:
    """Stop listening."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
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
        self.firmware: fw.Firmware | None = None  # newest of local and release
        self._local_firmware: fw.Firmware | None = None
        self._release_firmware: fw.Firmware | None = None
        self._release_checked: float | None = None
        # Serializes scans, GitHub checks and release downloads.
        self._firmware_lock = asyncio.Lock()
        self.updates: dict[str, UpdateSession] = {}
        self._store: Store[dict[str, dict]] = Store(hass, _STORAGE_VERSION, f"{DOMAIN}.rings")
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
            self.rings.setdefault(
                dev, Ring(address=data.get("address"), local=data.get("local"), fw=data.get("fw"))
            )
        # GitHub is checked in the background once setup is done.
        await self.async_scan_firmware(skip_release_check=True)

    def close(self) -> None:
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
        self, dev: str, addr: tuple, local_addr: bytes | None, hello: coap.Hello | None = None
    ) -> bool:
        """Records where a ring is; False if it is unknown and there is no room."""
        ring = self.rings.get(dev)
        added = ring is None
        if added:
            if len(self.rings) >= MAX_RINGS:
                _LOGGER.warning("Ignoring ring %s: already %d rings known", dev, MAX_RINGS)
                return False
            ring = self.rings[dev] = Ring()

        before = (ring.address, ring.local, ring.fw)
        ring.address = addr[0]
        if local_addr:
            ring.local = socket.inet_ntop(socket.AF_INET6, local_addr)
        if hello is not None:
            ring.fw = hello.fw
            ring.sid = hello.sid

        self._ensure_device(dev)
        if (ring.address, ring.local, ring.fw) != before:
            self._store.async_delay_save(self._stored_rings, _STORAGE_SAVE_DELAY)
            if not added:
                async_dispatcher_send(self.hass, SIGNAL_RING_CHANGED, dev)
        if added:
            async_dispatcher_send(self.hass, SIGNAL_RING_ADDED)
        return True

    @callback
    def _stored_rings(self) -> dict[str, dict]:
        return {
            dev: {"address": r.address, "local": r.local, "fw": r.fw}
            for dev, r in self.rings.items()
        }

    @callback
    def _ensure_device(self, dev: str) -> dr.DeviceEntry:
        return dr.async_get(self.hass).async_get_or_create(
            config_entry_id=self.entry.entry_id,
            identifiers={(DOMAIN, dev)},
            name=f"NFC Ring {dev[-4:].upper()}",
            manufacturer="DIY",
            model="NFC Ring prototype",
            sw_version=self.rings[dev].fw,
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
        """Finds the newest image: the local folder always, GitHub when due."""
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

            if self._release_firmware is not None:
                self._release_firmware = await self.hass.async_add_executor_job(
                    fw.cached_release, self._release_cache_dir, self._release_firmware
                )

            self._select_firmware()

    @callback
    def _select_firmware(self) -> None:
        firmware = fw.newest(self._local_firmware, self._release_firmware)
        if firmware != self.firmware:
            self.firmware = firmware
            async_dispatcher_send(self.hass, SIGNAL_FIRMWARE_CHANGED)

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
            if self._release_firmware and self._release_firmware.version == firmware.version:
                self._release_firmware = firmware
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

        if not self._ring_seen(event.dev, addr, local_addr):
            self._reply(coap.build_ack(msg, coap.CODE_FORBIDDEN), addr, local_addr)
            return

        state = self.duplicates.admit(event.key)
        if state is coap.ScanState.DONE:
            # Retransmission: the ring missed our first reply.
            self._reply(coap.build_ack(msg, coap.CODE_CHANGED), addr, local_addr)
        elif state is coap.ScanState.PENDING:
            # Still recording the first copy; the ring retries and gets the
            # real result then.
            pass
        elif state is coap.ScanState.FULL:
            _LOGGER.debug("Too many scans in flight, refusing scan from %s", addr[0])
            self._reply(coap.build_ack(msg, coap.CODE_SERVICE_UNAVAILABLE), addr, local_addr)
        else:
            self.entry.async_create_background_task(
                self.hass, self._handle_scan(msg, event, addr, local_addr), f"{DOMAIN} scan"
            )

    async def _handle_scan(
        self, msg: coap.CoapMessage, event: coap.ScanEvent, addr: tuple, local_addr: bytes | None
    ) -> None:
        try:
            device = self._ensure_device(event.dev)
            await async_scan_tag(self.hass, event.uid, device.id)
        except HomeAssistantError as err:
            _LOGGER.error("Could not record scan of tag %s: %s", event.uid, err)
            self.duplicates.forget(event.key)
            self._reply(coap.build_ack(msg, coap.CODE_INTERNAL_ERROR), addr, local_addr)
            return
        except BaseException:
            # Includes cancellation on unload: let a retransmission try again.
            self.duplicates.forget(event.key)
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

        if not self._ring_seen(hello.dev, addr, local_addr, hello):
            self._reply(coap.build_ack(msg, coap.CODE_FORBIDDEN), addr, local_addr)
            return
        # Acknowledging a trial hello is what lets the ring confirm the image.
        self._reply(coap.build_ack(msg, coap.CODE_CHANGED), addr, local_addr)

        session = self.updates.get(hello.dev)
        if session is None or session.done.done() or not session.image:
            # No update, or still preparing it (the ring has not been told yet).
            return
        if hello.sid == session.start_sid or (
            session.start_sid is None and session.downloaded_at is None
        ):
            # Still the boot that is downloading; nothing installed yet. (Without
            # a known boot session, only hellos after the last block can count.)
            return
        if hello.fw != session.version:
            session.done.set_result(
                f"ring restarted with {hello.fw} instead of {session.version}: "
                "the image was rejected or rolled back"
            )
        elif hello.confirmed:
            session.done.set_result(None)
        # else: new image on trial; the ring confirms it now and says hello again.

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

        # Only forward progress counts; repeated requests for old blocks do
        # not keep a stuck download alive.
        if end > session.next_offset:
            session.next_offset = end
            session.last_progress = time.monotonic()
            if not more:
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
        if ring is None or ring.address is None:
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
        firmware = self.firmware
        if firmware is None:
            raise HomeAssistantError(
                f"No firmware found in {FIRMWARE_REPOSITORY} releases or {FIRMWARE_DIR}"
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

        code = await self._async_request(
            ring, lambda mid, token: coap.build_notify(mid, token, firmware.version, len(image))
        )
        if code is None:
            raise HomeAssistantError("The ring did not answer the update notice")
        if code >> 5 != 2:
            raise HomeAssistantError(
                f"The ring refused the update notice (CoAP {code >> 5}.{code & 0x1F:02d}); "
                "it may still be confirming a previous update or recovering from a failed one"
            )

        _LOGGER.info("Ring %s is downloading firmware %s", dev, firmware.version)
        session.last_progress = time.monotonic()
        if (error := await self._async_wait_for_install(session)) is not None:
            raise HomeAssistantError(f"Update to {firmware.version} failed: {error}")
        _LOGGER.info("Ring %s now runs firmware %s", dev, firmware.version)

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
