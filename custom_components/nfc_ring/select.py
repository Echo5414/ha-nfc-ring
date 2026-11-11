"""Radio select for each NFC ring: Thread, Bluetooth or (as its own firmware) Zigbee."""

from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import NfcRingConfigEntry, RingServer
from .const import (
    DOMAIN,
    RADIO_THREAD,
    SIGNAL_FIRMWARE_CHANGED,
    SIGNAL_RING_ADDED,
    SIGNAL_RING_CHANGED,
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: NfcRingConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add a radio select for every known ring, now and when new ones appear."""
    server = entry.runtime_data
    added: set[str] = set()

    @callback
    def add_new_rings() -> None:
        added.intersection_update(server.rings)
        new = [dev for dev in server.rings if dev not in added]
        added.update(new)
        async_add_entities(RingRadioSelect(server, dev) for dev in new)

    add_new_rings()
    entry.async_on_unload(async_dispatcher_connect(hass, SIGNAL_RING_ADDED, add_new_rings))


class RingRadioSelect(SelectEntity):
    """The radio a ring uses to reach Home Assistant.

    Shows the chosen radio at once; the ring follows at its next contact, or
    with the firmware update it needs for it (see RingServer.async_set_radio).
    """

    _attr_has_entity_name = True
    _attr_translation_key = "radio"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_should_poll = False

    def __init__(self, server: RingServer, dev: str) -> None:
        self._server = server
        self._dev = dev
        self._attr_unique_id = f"{dev}_radio"
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, dev)})

    @property
    def options(self) -> list[str]:
        return self._server.radios_for(self._dev)

    @property
    def current_option(self) -> str | None:
        ring = self._server.rings.get(self._dev)
        if ring is None:
            return None
        return ring.wanted or ring.radio or RADIO_THREAD

    async def async_select_option(self, option: str) -> None:
        await self._server.async_set_radio(self._dev, option)

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
