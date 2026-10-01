"""Config flow for Copilot Usage."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    SOURCE_REAUTH,
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .api import (
    CopilotAuthError,
    CopilotNotEntitledError,
    CopilotUsageError,
    DeviceCode,
    OAuthToken,
    fetch_billing_usage,
    fetch_login,
    fetch_quota_usage,
    poll_device_token,
    request_device_code,
)
from .const import (
    AUTH_BILLING,
    AUTH_DEVICE,
    AUTH_TOKEN,
    CONF_AUTH_TYPE,
    CONF_EXPIRES_AT,
    CONF_REFRESH_TOKEN,
    CONF_SCAN_INTERVAL,
    CONF_TARGET,
    CONF_TARGET_TYPE,
    CONF_TOKEN,
    DEFAULT_SCAN_BILLING,
    DEFAULT_SCAN_QUOTA,
    DOMAIN,
    MIN_SCAN_INTERVAL,
    TARGET_ORG,
    TARGET_USER,
)

_LOGGER = logging.getLogger(__name__)


class _FlowError(Exception):
    """Carries a translation key for a form/abort error."""


class CopilotUsageConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow."""

    VERSION = 1

    def __init__(self) -> None:
        self._device: DeviceCode | None = None
        self._login_task: asyncio.Task[OAuthToken] | None = None
        self._token: OAuthToken | None = None

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Pick a mode."""
        return self.async_show_menu(
            step_id="user", menu_options=[AUTH_DEVICE, AUTH_TOKEN, AUTH_BILLING]
        )

    # ----------------------------------------------------------- device flow
    async def async_step_device(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """GitHub device login."""
        session = async_get_clientsession(self.hass)
        if self._device is None:
            try:
                self._device = await request_device_code(session)
            except CopilotUsageError as err:
                _LOGGER.warning("Could not start GitHub device flow: %s", err)
                return self.async_abort(reason="cannot_connect")
        if self._login_task is None:
            self._login_task = self.hass.async_create_task(
                poll_device_token(session, self._device)
            )
        if not self._login_task.done():
            return self.async_show_progress(
                step_id="device",
                progress_action="wait_for_device",
                description_placeholders={
                    "url": self._device.verification_uri,
                    "code": self._device.user_code,
                },
                progress_task=self._login_task,
            )
        try:
            self._token = self._login_task.result()
        except CopilotUsageError as err:
            _LOGGER.warning("GitHub device login failed: %s", err)
            self._device = self._login_task = None
            return self.async_show_progress_done(next_step_id="device_failed")
        return self.async_show_progress_done(next_step_id="device_finish")

    async def async_step_device_failed(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Device flow failed or expired."""
        return self.async_abort(reason="device_failed")

    async def async_step_device_finish(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Validate the token from the device flow."""
        assert self._token
        try:
            return await self._create_quota_entry(
                self._token.access_token,
                AUTH_DEVICE,
                refresh_token=self._token.refresh_token,
                expires_at=self._token.expires_at,
            )
        except _FlowError as err:
            return self.async_abort(reason=str(err))

    # ------------------------------------------------------------- paste token
    async def async_step_token(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Paste an existing GitHub OAuth token (e.g. from `gh auth token`)."""
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                return await self._create_quota_entry(user_input[CONF_TOKEN].strip(), AUTH_TOKEN)
            except _FlowError as err:
                errors["base"] = str(err)
        return self.async_show_form(
            step_id="token",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_TOKEN): TextSelector(
                        TextSelectorConfig(type=TextSelectorType.PASSWORD)
                    )
                }
            ),
            errors=errors,
        )

    async def _create_quota_entry(
        self,
        token: str,
        auth_type: str,
        refresh_token: str | None = None,
        expires_at: float | None = None,
    ) -> ConfigFlowResult:
        """Check the token can read Copilot quotas, then create the entry."""
        session = async_get_clientsession(self.hass)
        try:
            usage = await fetch_quota_usage(session, token)
        except CopilotAuthError as err:
            raise _FlowError("invalid_auth") from err
        except CopilotNotEntitledError as err:
            raise _FlowError("no_copilot") from err
        except CopilotUsageError as err:
            raise _FlowError("cannot_connect") from err
        login = usage.login
        if not login:
            try:
                login = await fetch_login(session, token)
            except CopilotUsageError:
                login = None
        data = {
            CONF_AUTH_TYPE: auth_type,
            CONF_TOKEN: token,
            CONF_REFRESH_TOKEN: refresh_token,
            CONF_EXPIRES_AT: expires_at,
        }
        return await self._finish(
            f"quota_{login or token[-8:]}", f"Copilot {login or ''}".strip(), data
        )

    # ---------------------------------------------------------------- billing
    async def async_step_billing(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Fine-grained PAT for the billing usage API."""
        errors: dict[str, str] = {}
        if user_input is not None:
            token = user_input[CONF_TOKEN].strip()
            target_type = user_input[CONF_TARGET_TYPE]
            target = (user_input.get(CONF_TARGET) or "").strip()
            session = async_get_clientsession(self.hass)
            try:
                if not target:
                    if target_type == TARGET_ORG:
                        raise ValueError
                    target = await fetch_login(session, token) or ""
                    if not target:
                        raise ValueError
                await fetch_billing_usage(session, token, target_type, target)
            except ValueError:
                errors[CONF_TARGET] = "target_required"
            except CopilotAuthError as err:
                _LOGGER.warning("Billing API validation failed: %s", err)
                errors["base"] = "invalid_auth"
            except CopilotUsageError as err:
                _LOGGER.warning("Billing API validation failed: %s", err)
                errors["base"] = "cannot_connect"
            else:
                data = {
                    CONF_AUTH_TYPE: AUTH_BILLING,
                    CONF_TOKEN: token,
                    CONF_TARGET_TYPE: target_type,
                    CONF_TARGET: target,
                }
                return await self._finish(
                    f"billing_{target_type}_{target.lower()}", f"Copilot billing {target}", data
                )
        return self.async_show_form(
            step_id="billing",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_TOKEN): TextSelector(
                        TextSelectorConfig(type=TextSelectorType.PASSWORD)
                    ),
                    vol.Required(CONF_TARGET_TYPE, default=TARGET_USER): SelectSelector(
                        SelectSelectorConfig(
                            options=[TARGET_USER, TARGET_ORG],
                            translation_key="target_type",
                            mode=SelectSelectorMode.LIST,
                        )
                    ),
                    vol.Optional(CONF_TARGET): str,
                }
            ),
            errors=errors,
        )

    # ---------------------------------------------------------------- shared
    async def _finish(self, unique_id: str, title: str, data: dict[str, Any]) -> ConfigFlowResult:
        if self.source == SOURCE_REAUTH:
            return self.async_update_reload_and_abort(self._get_reauth_entry(), data_updates=data)
        await self.async_set_unique_id(unique_id)
        self._abort_if_unique_id_configured(updates=data)
        return self.async_create_entry(title=title, data=data)

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        """Token revoked."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Route to the right step."""
        if user_input is None:
            return self.async_show_form(step_id="reauth_confirm")
        auth_type = self._get_reauth_entry().data.get(CONF_AUTH_TYPE)
        if auth_type == AUTH_BILLING:
            return await self.async_step_billing()
        if auth_type == AUTH_TOKEN:
            return await self.async_step_token()
        return await self.async_step_device()

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Options flow."""
        return CopilotUsageOptionsFlow()


class CopilotUsageOptionsFlow(OptionsFlow):
    """Polling interval."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(
                data={CONF_SCAN_INTERVAL: int(user_input[CONF_SCAN_INTERVAL])}
            )
        default = (
            DEFAULT_SCAN_BILLING
            if self.config_entry.data.get(CONF_AUTH_TYPE) == AUTH_BILLING
            else DEFAULT_SCAN_QUOTA
        )
        current = self.config_entry.options.get(CONF_SCAN_INTERVAL, int(default.total_seconds()))
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_SCAN_INTERVAL, default=current): NumberSelector(
                        NumberSelectorConfig(
                            min=MIN_SCAN_INTERVAL,
                            max=86400,
                            step=60,
                            unit_of_measurement="s",
                            mode=NumberSelectorMode.BOX,
                        )
                    )
                }
            ),
        )
