"""Tests for the Copilot Usage integration."""

from __future__ import annotations

import asyncio
from unittest.mock import patch

from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from custom_components.copilot_usage.api import OAuthToken, parse_quota_usage
from custom_components.copilot_usage.const import (
    API_BASE,
    COPILOT_USER_URL,
    DEVICE_CODE_URL,
    DOMAIN,
)

QUOTA = {
    "login": "octo",
    "copilot_plan": "individual_pro",
    "access_type_sku": "plus_monthly_subscriber",
    "quota_reset_date": "2026-11-01",
    "quota_snapshots": {
        "chat": {"entitlement": 0, "remaining": 0, "percent_remaining": 100, "unlimited": True, "overage_count": 0},
        "completions": {"entitlement": 0, "remaining": 0, "percent_remaining": 100, "unlimited": True},
        "premium_interactions": {
            "entitlement": 1500, "remaining": 1125, "quota_remaining": 1125, "percent_remaining": 75.0,
            "unlimited": False, "overage_count": 0, "overage_permitted": True, "quota_id": "premium_interactions",
        },
    },
}


def test_parse() -> None:
    u = parse_quota_usage(QUOTA)
    q = u.quotas["premium_interactions"]
    assert q.percent_used == 25.0
    assert q.used == 375
    assert u.reset_at.month == 11
    free = parse_quota_usage(
        {"limited_user_quotas": {"chat": 40, "completions": 1500}, "monthly_quotas": {"chat": 50, "completions": 2000},
         "limited_user_reset_date": "2026-10-15"}
    )
    assert free.quotas["chat"].percent_used == 20.0


async def test_token_flow_and_sensors(hass: HomeAssistant, aioclient_mock) -> None:
    aioclient_mock.get(COPILOT_USER_URL, json=QUOTA)
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "token"})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"token": "gho_x"})
    assert result["type"] is FlowResultType.CREATE_ENTRY, result
    assert result["title"] == "Copilot octo"
    await hass.async_block_till_done()
    states = {s.entity_id: s.state for s in hass.states.async_all("sensor")}
    assert states["sensor.copilot_octo_premium_requests_used"] == "25.0"
    assert states["sensor.copilot_octo_premium_requests_remaining"] == "1125.0"
    assert states["sensor.copilot_octo_plan"] == "individual_pro"
    assert "sensor.copilot_octo_chat_used" not in states  # unlimited quotas are skipped
    assert states["sensor.copilot_octo_quota_reset"].startswith("2026-11-01")


async def test_token_flow_not_entitled(hass: HomeAssistant, aioclient_mock) -> None:
    aioclient_mock.get(COPILOT_USER_URL, status=404, text="Not Found")
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "token"})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"token": "gho_x"})
    assert result["errors"] == {"base": "no_copilot"}


async def test_device_flow(hass: HomeAssistant, aioclient_mock) -> None:
    aioclient_mock.post(
        DEVICE_CODE_URL,
        json={"device_code": "dc", "user_code": "ABCD-1234", "verification_uri": "https://github.com/login/device",
              "interval": 5, "expires_in": 900},
    )
    aioclient_mock.get(COPILOT_USER_URL, json=QUOTA)

    approved = asyncio.Event()

    async def fake_poll(session, device):
        await approved.wait()
        return OAuthToken("ghu_token", None, None)

    with patch("custom_components.copilot_usage.config_flow.poll_device_token", fake_poll):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "device"})
        assert result["type"] is FlowResultType.SHOW_PROGRESS
        assert result["description_placeholders"]["code"] == "ABCD-1234"
        approved.set()
        await hass.async_block_till_done()
        result = await hass.config_entries.flow.async_configure(result["flow_id"])
        assert result["type"] is FlowResultType.CREATE_ENTRY, result
        assert result["data"]["token"] == "ghu_token"


async def test_billing_flow(hass: HomeAssistant, aioclient_mock) -> None:
    aioclient_mock.get(f"{API_BASE}/user", json={"login": "octo"})
    aioclient_mock.get(
        f"{API_BASE}/users/octo/settings/billing/premium_request/usage",
        json={"usageItems": [
            {"product": "Copilot", "model": "GPT-5", "grossQuantity": 100, "grossAmount": 4, "netAmount": 0},
            {"product": "Copilot", "model": "Claude", "grossQuantity": 50, "grossAmount": 2, "netAmount": 1.5},
        ]},
    )
    aioclient_mock.get(f"{API_BASE}/users/octo/settings/billing/ai_credit/usage", status=404)
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "billing"})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"token": "github_pat_x", "target_type": "user"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY, result
    assert result["data"]["target"] == "octo"
    await hass.async_block_till_done()
    states = {s.entity_id: s.state for s in hass.states.async_all("sensor")}
    assert states["sensor.copilot_billing_octo_premium_requests_this_month"] == "150.0"
    assert states["sensor.copilot_billing_octo_premium_requests_billed_this_month"] == "1.5"
    assert states["sensor.copilot_billing_octo_ai_credits_this_month"] == "unavailable"
