"""Overview page in the sidebar: the rings, the tags and what each tag triggers.

The page is a small web component (frontend/nfc-ring-panel.js) that asks for
everything it shows with one websocket command, nfc_ring/overview, and pairs
a ring with nfc_ring/pair. It can be switched off in the integration's
options; a ring can then still be paired there.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import voluptuous as vol

from homeassistant.components import frontend, websocket_api
from homeassistant.components.http import StaticPathConfig
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_ON, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.loader import async_get_integration

from .const import CONF_SHOW_PANEL, DOMAIN

_LOGGER = logging.getLogger(__name__)

PANEL_URL_PATH = "nfc-ring"
_PANEL_ELEMENT = "nfc-ring-panel"
_STATIC_URL = f"/{DOMAIN}_static"
_STATIC_DIR = Path(__file__).parent / "frontend"
_DATA_SETUP = f"{DOMAIN}_panel_setup"
_DATA_VERSION = f"{DOMAIN}_panel_version"


async def async_setup(hass: HomeAssistant) -> None:
    """Serves the page's script and registers its data command (once per run)."""
    if hass.data.get(_DATA_SETUP):
        return
    hass.data[_DATA_SETUP] = True
    integration = await async_get_integration(hass, DOMAIN)
    hass.data[_DATA_VERSION] = str(integration.version or "0")
    await hass.http.async_register_static_paths(
        [StaticPathConfig(_STATIC_URL, str(_STATIC_DIR), cache_headers=False)]
    )
    websocket_api.async_register_command(hass, _ws_overview)
    websocket_api.async_register_command(hass, _ws_pair)


@callback
def async_apply(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Shows or hides the page according to the entry's options."""
    if not entry.options.get(CONF_SHOW_PANEL, True):
        async_remove(hass)
        return
    frontend.async_register_built_in_panel(
        hass,
        component_name="custom",
        sidebar_title="NFC Ring",
        sidebar_icon="mdi:ring",
        frontend_url_path=PANEL_URL_PATH,
        config={
            "_panel_custom": {
                "name": _PANEL_ELEMENT,
                "embed_iframe": False,
                "trust_external": False,
                # The version makes browsers fetch the script again after an update.
                "module_url": f"{_STATIC_URL}/{_PANEL_ELEMENT}.js?v={hass.data.get(_DATA_VERSION, '0')}",
            }
        },
        require_admin=False,
        update=True,
    )


@callback
def async_remove(hass: HomeAssistant) -> None:
    frontend.async_remove_panel(hass, PANEL_URL_PATH, warn_if_unknown=False)


@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/overview"})
@callback
def _ws_overview(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    connection.send_result(msg["id"], build_overview(hass))


@websocket_api.websocket_command(
    {vol.Required("type"): f"{DOMAIN}/pair", vol.Required("code"): str}
)
@websocket_api.require_admin
@callback
def _ws_pair(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Pairs the ring whose code was entered; "waiting" if it has not said hello yet."""
    server = _server(hass)
    if server is None:
        connection.send_error(msg["id"], "not_loaded", "NFC Ring is not running")
        return
    try:
        dev = server.async_pair(msg["code"])
    except ValueError:
        connection.send_error(msg["id"], "invalid_code", "This is not a pairing code")
        return
    except HomeAssistantError as err:
        connection.send_error(msg["id"], "failed", str(err))
        return
    connection.send_result(msg["id"], {"status": "paired" if dev else "waiting", "dev": dev})


@callback
def _server(hass: HomeAssistant):
    for entry in hass.config_entries.async_loaded_entries(DOMAIN):
        return entry.runtime_data
    return None


def _as_list(value: Any) -> list:
    if value is None:
        return []
    return list(value) if isinstance(value, (list, tuple, set)) else [value]


@callback
def _tag_automations(hass: HomeAssistant) -> dict[str, list[dict[str, Any]]]:
    """Automations with a tag trigger, by tag id.

    Reads each automation's configuration as the user wrote it. Triggers that
    are built from templates or blueprints inputs are not resolved.
    """
    by_tag: dict[str, list[dict[str, Any]]] = {}
    component = hass.data.get("automation")
    for entity in list(getattr(component, "entities", None) or []):
        raw = getattr(entity, "raw_config", None)
        if not isinstance(raw, dict):
            continue
        state = hass.states.get(entity.entity_id)
        last = state.attributes.get("last_triggered") if state else None
        info_base = {
            "entity_id": entity.entity_id,
            "id": getattr(entity, "unique_id", None),
            "name": (state.name if state else None) or raw.get("alias") or entity.entity_id,
            "enabled": state is not None and state.state == STATE_ON,
            "last_triggered": last.isoformat() if hasattr(last, "isoformat") else last,
        }
        for trigger in _as_list(raw.get("triggers")) + _as_list(raw.get("trigger")):
            if not isinstance(trigger, dict):
                continue
            if (trigger.get("trigger") or trigger.get("platform")) != "tag":
                continue
            devices = [str(d) for d in _as_list(trigger.get("device_id"))]
            for tag_id in _as_list(trigger.get("tag_id")):
                entries = by_tag.setdefault(str(tag_id), [])
                if all(e["entity_id"] != entity.entity_id for e in entries):
                    entries.append({**info_base, "device_ids": devices})
    return by_tag


@callback
def build_overview(hass: HomeAssistant) -> dict[str, Any]:
    """Everything the page shows."""
    dev_reg = dr.async_get(hass)
    ent_reg = er.async_get(hass)
    automations = _tag_automations(hass)

    tags: list[dict[str, Any]] = []
    last_scan: dict[str, str] = {}  # device id -> newest scan
    for state in hass.states.async_all("tag"):
        tag_id = state.attributes.get("tag_id")
        if tag_id is None:
            continue
        scanned = state.state if state.state not in (STATE_UNKNOWN, STATE_UNAVAILABLE) else None
        device_id = state.attributes.get("last_scanned_by_device_id")
        device = dev_reg.async_get(device_id) if device_id else None
        if scanned and device_id and scanned > last_scan.get(device_id, ""):
            last_scan[device_id] = scanned
        tags.append(
            {
                "id": str(tag_id),
                "entity_id": state.entity_id,
                "name": state.name,
                "last_scanned": scanned,
                "device_id": device_id,
                "device_name": (device.name_by_user or device.name) if device else None,
                "automations": automations.get(str(tag_id), []),
            }
        )
    tags.sort(key=lambda t: (t["last_scanned"] is not None, t["last_scanned"] or ""), reverse=True)

    server = _server(hass)
    known = server.rings if server else {}

    rings: list[dict[str, Any]] = []
    for entry in hass.config_entries.async_entries(DOMAIN):
        for device in dr.async_entries_for_config_entry(dev_reg, entry.entry_id):
            update_available = False
            latest = None
            for ent in er.async_entries_for_device(ent_reg, device.id):
                if ent.domain != "update" or ent.platform != DOMAIN:
                    continue
                if (state := hass.states.get(ent.entity_id)) is not None:
                    update_available = state.state == STATE_ON
                    latest = state.attributes.get("latest_version")
            dev = next((i for domain, i in device.identifiers if domain == DOMAIN), None)
            ring = known.get(dev)
            rings.append(
                {
                    "id": device.id,
                    "dev": dev,
                    # False: known from before pairing existed, still unsigned.
                    "paired": ring is not None and ring.key is not None,
                    # Its firmware signs, so entering the code is all it takes.
                    "can_pair": ring is not None and ring.candidate is not None,
                    "name": device.name_by_user or device.name,
                    "firmware": device.sw_version,
                    "latest_firmware": latest,
                    "update_available": update_available,
                    "disabled": device.disabled_by is not None,
                    "last_scan": last_scan.get(device.id),
                }
            )
    rings.sort(key=lambda r: (r["name"] or "").lower())

    # Unknown rings that said hello: refused until their code is entered.
    pending = [
        {
            "dev": dev,
            "firmware": p.fw,
            "can_pair": p.candidate is not None,
            "seen": datetime.fromtimestamp(p.seen, UTC).isoformat(),
        }
        for dev, p in (server.pending if server else {}).items()
    ]
    pending.sort(key=lambda p: p["seen"], reverse=True)

    return {
        "rings": rings,
        "pending": pending,
        "waiting": bool(server and server.waiting_for_ring),
        "tags": tags,
    }
