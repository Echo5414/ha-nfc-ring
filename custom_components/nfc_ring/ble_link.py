"""Rings over Bluetooth: listens for their advertisements and connects to them.

Uses Home Assistant's Bluetooth integration, so any adapter it knows works,
including Bluetooth proxies. A scan arrives as a signed advertisement and
needs no connection. A connection is made when a ring asks for one (it has a
hello to deliver) or when there is something to tell it (a radio setting, a
firmware update), one ring at a time, and closed again right after.

The protocol is in ble.py; what a message means is decided by the RingServer.
This module is only loaded while Home Assistant has Bluetooth set up.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
import logging
import time
from typing import TYPE_CHECKING

from bleak.exc import BleakError
from bleak_retry_connector import BleakClientWithServiceCache, establish_connection

from homeassistant.components import bluetooth
from homeassistant.components.bluetooth import (
    BluetoothCallbackMatcher,
    BluetoothChange,
    BluetoothScanningMode,
    BluetoothServiceInfoBleak,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_track_time_interval

from . import ble
from .const import DOMAIN

if TYPE_CHECKING:
    from . import RingServer

_LOGGER = logging.getLogger(__name__)

# Home Assistant only reports an advertisement when its content changes, so a
# ring that keeps asking for a connection is looked at again this often.
_TICK = timedelta(seconds=15)

_HELLO_SECONDS = 10  # the ring says hello as soon as we subscribe
_ANSWER_SECONDS = 5
_REQUEST_SECONDS = 30  # between two firmware requests
# A ring confirms freshly installed firmware after our answer and says hello
# once more; stay for that. Otherwise leave soon: a connection costs battery.
_LINGER_TRIAL_SECONDS = 10
_LINGER_SECONDS = 2
_SWITCH_SECONDS = 8  # the ring changes its radio 1.5 s after its answer

_RETRY_SECONDS = 20
# A ring that is not paired here asks for a connection all the time; it may
# be a neighbour's. Connect rarely, unless a pairing code is waiting for it.
_STRANGER_RETRY_SECONDS = 5 * 60
_STRANGER_RETRY_MAX_SECONDS = 60 * 60
_PAIRING_RETRY_SECONDS = 15
# After a setting the ring did not take, do not connect for it again at once.
_QUIET_SECONDS = 10 * 60

_PACKET_MIN = 20
_PACKET_MAX = 244

_CLOSED = (-1, b"")  # put in the inbox when the connection is gone


class BleLink:
    """Finds rings in Bluetooth advertisements and talks to them."""

    def __init__(self, hass: HomeAssistant, server: RingServer) -> None:
        self.hass = hass
        self._server = server
        self._frames: dict[str, ble.IdleFrame] = {}  # Bluetooth address -> last idle frame
        self._retry_at: dict[str, float] = {}  # dev -> time.monotonic()
        self._stranger_delay: dict[str, float] = {}
        self._quiet_until: dict[str, float] = {}
        self._kicked: set[str] = set()
        self._settings_waiters: dict[str, asyncio.Future[int | None]] = {}
        self._task: asyncio.Task | None = None
        self._unsubs: list = []

    @callback
    def start(self) -> None:
        self._unsubs = [
            bluetooth.async_register_callback(
                self.hass,
                self._on_advertisement,
                BluetoothCallbackMatcher(manufacturer_id=ble.COMPANY_ID, connectable=True),
                BluetoothScanningMode.ACTIVE,
            ),
            async_track_time_interval(self.hass, self._on_tick, _TICK),
        ]

    @callback
    def stop(self) -> None:
        for unsub in self._unsubs:
            unsub()
        self._unsubs = []
        if self._task is not None:
            self._task.cancel()

    @property
    def can_connect(self) -> bool:
        """True if an adapter (or proxy) that can connect to devices is there."""
        return bluetooth.async_scanner_count(self.hass, connectable=True) > 0

    @callback
    def kick(self, dev: str) -> None:
        """Connect to this ring as soon as it is in range, whatever the waiting times."""
        self._kicked.add(dev)
        self._retry_at.pop(dev, None)
        self._quiet_until.pop(dev, None)
        self._consider()

    async def async_send_settings(self, dev: str, seconds: float) -> int | None:
        """Connects and delivers the ring's settings; its answer, None if not reached."""
        waiter = self._settings_waiters.get(dev)
        if waiter is None or waiter.done():
            waiter = self._settings_waiters[dev] = self.hass.loop.create_future()
        self.kick(dev)
        try:
            return await asyncio.wait_for(asyncio.shield(waiter), seconds)
        except TimeoutError:
            return None
        finally:
            if self._settings_waiters.get(dev) is waiter:
                del self._settings_waiters[dev]

    # Advertisements

    @callback
    def _on_advertisement(
        self, info: BluetoothServiceInfoBleak, change: BluetoothChange
    ) -> None:
        self._read(info)
        self._consider()

    @callback
    def _read(self, info: BluetoothServiceInfoBleak) -> None:
        data = info.manufacturer_data.get(ble.COMPANY_ID)
        frame = ble.parse_frame(bytes(data)) if data else None
        if isinstance(frame, ble.ScanFrame):
            sender = self._frames.get(info.address)
            self._server.ble_scan(
                info.address, frame, named=sender.dev if sender is not None else None
            )
        elif isinstance(frame, ble.IdleFrame):
            for address, known in list(self._frames.items()):
                if known.dev == frame.dev and address != info.address:
                    del self._frames[address]
            self._frames[info.address] = frame

    @callback
    def _on_tick(self, _now) -> None:
        for address in list(self._frames):
            info = bluetooth.async_last_service_info(self.hass, address, connectable=True)
            if info is None:
                del self._frames[address]  # out of range
            else:
                self._read(info)
        self._consider()

    @callback
    def _consider(self) -> None:
        """Starts a connection if a ring in range needs one and none is open."""
        if self._task is not None and not self._task.done():
            return
        now = time.monotonic()
        choice: tuple[int, str, str] | None = None
        for address, frame in self._frames.items():
            dev = frame.dev
            if dev in self._kicked:
                rank = 0
            elif self._server.ble_wanted(dev) and now >= self._quiet_until.get(dev, 0):
                rank = 1
            elif frame.wants_connection and now >= self._retry_at.get(dev, 0):
                rank = 2 if self._server.ble_known(dev) else 3
            else:
                continue
            if now < self._retry_at.get(dev, 0):
                continue
            if choice is None or rank < choice[0]:
                choice = (rank, dev, address)
        if choice is None:
            return
        _, dev, address = choice
        self._task = self._server.entry.async_create_background_task(
            self.hass, self._async_run(dev, address), f"{DOMAIN} bluetooth {dev}"
        )

    # Connections

    async def _async_run(self, dev: str, address: str) -> None:
        self._kicked.discard(dev)
        paired = False
        try:
            paired = await self._async_session(dev, address)
        except (BleakError, TimeoutError, OSError) as err:
            _LOGGER.debug("Bluetooth connection to ring %s failed: %s", dev, err)
            paired = bool(self._server.ble_known(dev))
        except Exception:
            _LOGGER.exception("Unexpected error talking to ring %s over Bluetooth", dev)
        finally:
            now = time.monotonic()
            if paired:
                self._stranger_delay.pop(dev, None)
                delay = _RETRY_SECONDS
            elif self._server.waiting_for_ring:
                delay = _PAIRING_RETRY_SECONDS
            else:
                delay = self._stranger_delay.get(dev, _STRANGER_RETRY_SECONDS)
                self._stranger_delay[dev] = min(delay * 2, _STRANGER_RETRY_MAX_SECONDS)
            self._retry_at[dev] = now + delay
            # Whoever waits for this ring's answer hears that there was none.
            waiter = self._settings_waiters.get(dev)
            if waiter is not None and not waiter.done() and not paired:
                waiter.set_result(None)
            session = self._server.ble_update(dev)
            if session is not None and not paired:
                session.notice_answer.set_result(None)
            self._task = None
            self.hass.loop.call_soon(self._consider)

    async def _async_session(self, dev: str, address: str) -> bool:
        """One connection to a ring; True if it is paired with us."""
        device = bluetooth.async_ble_device_from_address(self.hass, address, connectable=True)
        if device is None:
            raise BleakError("out of range")

        inbox: asyncio.Queue[tuple[int, bytes]] = asyncio.Queue()
        client = await establish_connection(
            BleakClientWithServiceCache,
            device,
            f"NFC Ring {dev}",
            disconnected_callback=lambda _client: inbox.put_nowait(_CLOSED),
            max_attempts=2,
        )
        try:
            return await _Session(self, self._server, client, inbox, dev, address).async_run()
        finally:
            await client.disconnect()

    @callback
    def settings_answered(self, dev: str, answer: int | None) -> None:
        if answer != ble.CFG_OK:
            self._quiet_until[dev] = time.monotonic() + _QUIET_SECONDS
        waiter = self._settings_waiters.get(dev)
        if waiter is not None and not waiter.done():
            waiter.set_result(answer)


class _Session:
    """What happens over one connection."""

    def __init__(
        self,
        link: BleLink,
        server: RingServer,
        client: BleakClientWithServiceCache,
        inbox: asyncio.Queue[tuple[int, bytes]],
        dev: str,
        address: str,
    ) -> None:
        self._link = link
        self._server = server
        self._client = client
        self._inbox = inbox
        self._dev = dev
        self._address = address
        self._parts = ble.Reassembler()
        self._rx = None
        self._size = _PACKET_MIN
        self._hellos = 0
        self._paired = False
        self._trial = False
        self._closed = False

    async def async_run(self) -> bool:
        client = self._client
        self._rx = client.services.get_characteristic(ble.RX_UUID)
        if self._rx is None or client.services.get_characteristic(ble.TX_UUID) is None:
            await client.clear_cache()
            raise BleakError("not a ring: its service is missing")
        self._size = max(
            _PACKET_MIN, min(_PACKET_MAX, self._rx.max_write_without_response_size)
        )
        await client.start_notify(ble.TX_UUID, self._on_packet)

        # The ring says hello as soon as we listen.
        await self._async_pump(_HELLO_SECONDS, until=lambda: self._hellos > 0)
        if not self._hellos:
            raise BleakError("the ring did not say hello")
        if not self._paired:
            return False

        dev = self._dev
        sealed = await self._server.async_ble_settings(dev)
        if sealed is not None:
            await self._async_send(ble.MSG_CFG, sealed)
            payload = await self._async_expect(ble.MSG_CFG_ACK, _ANSWER_SECONDS)
            answer = payload[0] if payload else None
            self._server.ble_settings_answer(dev, answer)
            self._link.settings_answered(dev, answer)
            if answer == ble.CFG_OK:
                # It switches now and says hello again, here or over Thread.
                seen = self._hellos
                await self._async_pump(_SWITCH_SECONDS, until=lambda: self._hellos > seen)
            return True
        self._link.settings_answered(dev, None)  # nothing was due after all

        session = self._server.ble_update(dev)
        if session is not None:
            await self._async_send(ble.MSG_DFU, session.notice)
            payload = await self._async_expect(ble.MSG_DFU_ACK, _ANSWER_SECONDS)
            answer = payload[0] if payload else None
            session.notice_answer.set_result(answer)
            if answer == ble.DFU_ACCEPTED:
                await self._async_serve(session)
            return True

        await self._async_pump(_LINGER_TRIAL_SECONDS if self._trial else _LINGER_SECONDS)
        return True

    async def _async_serve(self, session) -> None:
        """Answers the ring's requests for the firmware image until it has it all."""
        image = session.image
        while not self._closed and not session.done.done():
            payload = await self._async_expect(ble.MSG_DFU_REQ, _REQUEST_SECONDS)
            if payload is None:
                return  # gone to restart, or stuck; the update's own timeouts decide
            request = ble.parse_dfu_request(payload)
            packets = ble.dfu_packets(image, *request, self._size) if request else []
            if not packets:
                _LOGGER.debug("Ring %s sent an invalid firmware request", self._dev)
                return
            for packet in packets[:-1]:
                await self._client.write_gatt_char(self._rx, packet, response=False)
            # The last one waits for the ring, so the next request finds us ready.
            await self._client.write_gatt_char(self._rx, packets[-1], response=True)
            self._server.ble_served(self._dev, session, request[0] + request[1])

    @callback
    def _on_packet(self, _characteristic, data: bytearray) -> None:
        message = self._parts.feed(bytes(data))
        if message is not None:
            self._inbox.put_nowait(message)

    async def _async_send(self, msg_type: int, payload: bytes) -> None:
        for packet in ble.split(msg_type, payload, self._size):
            await self._client.write_gatt_char(self._rx, packet, response=True)

    async def _async_expect(self, msg_type: int, seconds: float) -> bytes | None:
        """The payload of the next message of this type; None if none comes."""
        deadline = time.monotonic() + seconds
        while not self._closed and (left := deadline - time.monotonic()) > 0:
            try:
                got, payload = await asyncio.wait_for(self._inbox.get(), left)
            except TimeoutError:
                break
            if got == msg_type:
                return payload
            await self._async_handle(got, payload)
        return None

    async def _async_pump(self, seconds: float, until=None) -> None:
        """Handles what the ring sends on its own for a while."""
        deadline = time.monotonic() + seconds
        while (
            not self._closed
            and not (until is not None and until())
            and (left := deadline - time.monotonic()) > 0
        ):
            try:
                got, payload = await asyncio.wait_for(self._inbox.get(), left)
            except TimeoutError:
                break
            await self._async_handle(got, payload)

    async def _async_handle(self, msg_type: int, payload: bytes) -> None:
        if (msg_type, payload) == _CLOSED:
            self._closed = True
        elif msg_type == ble.MSG_SCAN:
            scan = ble.parse_scan(payload)
            if scan is not None:
                # Only a paired ring's scan counts, and only with its own key.
                self._server.ble_scan(self._address, scan, self._dev)
        elif msg_type == ble.MSG_HELLO:
            hello, answer = self._server.ble_hello(payload, self._address)
            if hello is None or hello.dev != self._dev:
                _LOGGER.debug("Ring %s sent a hello that is not its own", self._dev)
                answer = None
            self._hellos += 1
            self._paired = answer is not None
            self._trial = hello is not None and not hello.confirmed
            await self._async_send(ble.MSG_ACK, answer or b'{"st":"unpaired"}')
        else:
            _LOGGER.debug("Ring %s sent message %#04x unasked", self._dev, msg_type)
