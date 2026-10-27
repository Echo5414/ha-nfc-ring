"""Firmware update entity for each NFC ring."""

from __future__ import annotations

from typing import Any

from homeassistant.components.update import (
    UpdateDeviceClass,
    UpdateEntity,
    UpdateEntityFeature,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import NfcRingConfigEntry, RingServer
from .coap import version_key
from .const import DOMAIN, SIGNAL_FIRMWARE_CHANGED, SIGNAL_RING_ADDED, SIGNAL_RING_CHANGED
from .firmware import Firmware

# Home Assistant shows at most 255 characters of the release summary.
_SUMMARY_MAX = 255


async def async_setup_entry(
    hass: HomeAssistant,
    entry: NfcRingConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add an update entity for every known ring, now and when new ones appear."""
    server = entry.runtime_data
    added: set[str] = set()

    @callback
    def add_new_rings() -> None:
        new = [dev for dev in server.rings if dev not in added]
        added.update(new)
        async_add_entities(RingFirmwareUpdate(server, dev) for dev in new)

    add_new_rings()
    entry.async_on_unload(async_dispatcher_connect(hass, SIGNAL_RING_ADDED, add_new_rings))


class RingFirmwareUpdate(UpdateEntity):
    """Offers the newest released or local firmware image to one ring."""

    _attr_has_entity_name = True
    _attr_name = "Firmware"
    _attr_device_class = UpdateDeviceClass.FIRMWARE
    _attr_supported_features = (
        UpdateEntityFeature.INSTALL
        | UpdateEntityFeature.PROGRESS
        | UpdateEntityFeature.RELEASE_NOTES
    )
    _attr_title = "NFC Ring firmware"
    _attr_should_poll = False

    def __init__(self, server: RingServer, dev: str) -> None:
        self._server = server
        self._dev = dev
        self._attr_unique_id = f"{dev}_firmware"
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, dev)})

    @property
    def _offered(self) -> Firmware | None:
        """The newest image if it is newer than the running firmware."""
        installed = self.installed_version
        firmware = self._server.firmware
        # Never offer an older image than the one running.
        if firmware is None or (
            installed is not None and version_key(firmware.version) <= version_key(installed)
        ):
            return None
        return firmware

    @property
    def installed_version(self) -> str | None:
        return self._server.rings[self._dev].fw

    @property
    def latest_version(self) -> str | None:
        offered = self._offered
        return offered.version if offered else self.installed_version

    @property
    def release_url(self) -> str | None:
        offered = self._offered
        return offered.release_url if offered else None

    @property
    def release_summary(self) -> str | None:
        offered = self._offered
        if offered is None or offered.release_notes is None:
            return None
        first_line = offered.release_notes.strip().splitlines()[0]
        return first_line[:_SUMMARY_MAX]

    @property
    def in_progress(self) -> bool:
        return self._dev in self._server.updates

    @property
    def update_percentage(self) -> int | None:
        session = self._server.updates.get(self._dev)
        return session.progress if session else None

    async def async_added_to_hass(self) -> None:
        @callback
        def ring_changed(dev: str) -> None:
            if dev == self._dev:
                self.async_write_ha_state()

        self.async_on_remove(
            async_dispatcher_connect(self.hass, SIGNAL_RING_CHANGED, ring_changed)
        )
        self.async_on_remove(
            async_dispatcher_connect(self.hass, SIGNAL_FIRMWARE_CHANGED, self.async_write_ha_state)
        )

    async def async_update(self) -> None:
        """Looks for new firmware ("check for updates"), including GitHub."""
        await self._server.async_scan_firmware(force_release_check=True)

    async def async_release_notes(self) -> str | None:
        offered = self._offered
        return offered.release_notes if offered else None

    async def async_install(self, version: str | None, backup: bool, **kwargs: Any) -> None:
        await self._server.async_update_ring(self._dev)
