"""Config flow for NFC Ring: a single entry; the only option is the sidebar page.

The options form can also take a ring's pairing code, for when the page is off.
"""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback

from homeassistant.exceptions import HomeAssistantError

from .const import CONF_PAIRING_CODE, CONF_SHOW_PANEL, DOMAIN


def _schema(show_panel: bool) -> vol.Schema:
    return vol.Schema({vol.Required(CONF_SHOW_PANEL, default=show_panel): bool})


class NfcRingConfigFlow(ConfigFlow, domain=DOMAIN):
    """Adds the scan receiver."""

    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(
                title="NFC Ring",
                data={},
                options={CONF_SHOW_PANEL: user_input.get(CONF_SHOW_PANEL, True)},
            )
        return self.async_show_form(step_id="user", data_schema=_schema(True))

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return NfcRingOptionsFlow()


class NfcRingOptionsFlow(OptionsFlow):
    """Switches the overview page in the sidebar on or off; pairs a ring."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        shown = self.config_entry.options.get(CONF_SHOW_PANEL, True)
        if user_input is not None:
            shown = user_input.get(CONF_SHOW_PANEL, shown)
            code = (user_input.get(CONF_PAIRING_CODE) or "").strip()
            if code:
                try:
                    # Without a ring that fits yet, the code waits for its hello.
                    self.config_entry.runtime_data.async_pair(code)
                except ValueError:
                    errors[CONF_PAIRING_CODE] = "invalid_code"
                except (AttributeError, HomeAssistantError):
                    errors[CONF_PAIRING_CODE] = "pairing_failed"
            if not errors:
                return self.async_create_entry(data={CONF_SHOW_PANEL: shown})
        schema = _schema(shown).extend({vol.Optional(CONF_PAIRING_CODE): str})
        return self.async_show_form(step_id="init", data_schema=schema, errors=errors)
