"""Data update coordinator for Copilot Usage."""

from __future__ import annotations

from datetime import timedelta
import logging
import time

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import (
    BillingUsage,
    CopilotAuthError,
    CopilotUsageError,
    QuotaUsage,
    fetch_billing_usage,
    fetch_quota_usage,
    refresh_oauth_token,
)
from .const import (
    AUTH_BILLING,
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
)

_LOGGER = logging.getLogger(__name__)

type CopilotUsageConfigEntry = ConfigEntry[CopilotUsageCoordinator]


class CopilotUsageCoordinator(DataUpdateCoordinator[QuotaUsage | BillingUsage]):
    """Polls copilot_internal/user or the billing usage API."""

    config_entry: CopilotUsageConfigEntry

    def __init__(self, hass: HomeAssistant, entry: CopilotUsageConfigEntry) -> None:
        self.auth_type: str = entry.data[CONF_AUTH_TYPE]
        default = DEFAULT_SCAN_BILLING if self.auth_type == AUTH_BILLING else DEFAULT_SCAN_QUOTA
        seconds = entry.options.get(CONF_SCAN_INTERVAL)
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN}_{entry.entry_id}",
            update_interval=timedelta(seconds=seconds) if seconds else default,
        )
        self._session = async_get_clientsession(hass)
        self.options_snapshot = dict(entry.options)

    async def _async_update_data(self) -> QuotaUsage | BillingUsage:
        data = self.config_entry.data
        try:
            if self.auth_type == AUTH_BILLING:
                return await fetch_billing_usage(
                    self._session, data[CONF_TOKEN], data[CONF_TARGET_TYPE], data[CONF_TARGET]
                )
            expires = data.get(CONF_EXPIRES_AT)
            if expires and data.get(CONF_REFRESH_TOKEN) and float(expires) - 300 < time.time():
                await self._refresh()
            return await fetch_quota_usage(self._session, self.config_entry.data[CONF_TOKEN])
        except CopilotAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except CopilotUsageError as err:
            raise UpdateFailed(str(err)) from err

    async def _refresh(self) -> None:
        token = await refresh_oauth_token(
            self._session, self.config_entry.data[CONF_REFRESH_TOKEN]
        )
        self.hass.config_entries.async_update_entry(
            self.config_entry,
            data={
                **self.config_entry.data,
                CONF_TOKEN: token.access_token,
                CONF_REFRESH_TOKEN: token.refresh_token
                or self.config_entry.data.get(CONF_REFRESH_TOKEN),
                CONF_EXPIRES_AT: token.expires_at,
            },
        )
