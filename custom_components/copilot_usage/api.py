"""Small async client for GitHub Copilot quotas and billing usage."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
import logging
import time
from typing import Any

import aiohttp

from .const import (
    ACCESS_TOKEN_URL,
    AI_CREDIT_API_VERSION,
    API_BASE,
    COPILOT_USER_URL,
    DEVICE_CODE_URL,
    EDITOR_PLUGIN_VERSION,
    EDITOR_VERSION,
    GITHUB_API_VERSION,
    OAUTH_CLIENT_ID,
    OAUTH_SCOPE,
    TARGET_ORG,
    USER_AGENT,
)

_LOGGER = logging.getLogger(__name__)
TIMEOUT = aiohttp.ClientTimeout(total=30)


class CopilotUsageError(Exception):
    """Generic error."""


class CopilotAuthError(CopilotUsageError):
    """Token invalid / lacks access."""


class CopilotNotEntitledError(CopilotUsageError):
    """Account has no Copilot access."""


# --------------------------------------------------------------------------- #
# Device flow
# --------------------------------------------------------------------------- #


@dataclass
class DeviceCode:
    """Device flow state."""

    device_code: str
    user_code: str
    verification_uri: str
    interval: int
    expires_at: float


@dataclass
class OAuthToken:
    """GitHub user token (+ refresh token when the app issues expiring tokens)."""

    access_token: str
    refresh_token: str | None
    expires_at: float | None


async def request_device_code(session: aiohttp.ClientSession) -> DeviceCode:
    """Start the device flow."""
    try:
        async with session.post(
            DEVICE_CODE_URL,
            data={"client_id": OAUTH_CLIENT_ID, "scope": OAUTH_SCOPE},
            headers={"Accept": "application/json", "User-Agent": USER_AGENT},
            timeout=TIMEOUT,
        ) as resp:
            data = await resp.json(content_type=None)
    except (aiohttp.ClientError, TimeoutError, ValueError) as err:
        raise CopilotUsageError(f"Device code request failed: {err}") from err
    if "device_code" not in data:
        raise CopilotUsageError(f"Unexpected device code response: {data}")
    return DeviceCode(
        device_code=data["device_code"],
        user_code=data["user_code"],
        verification_uri=data.get("verification_uri", "https://github.com/login/device"),
        interval=int(data.get("interval", 5)),
        expires_at=time.time() + int(data.get("expires_in", 900)),
    )


def _token_from(data: dict[str, Any]) -> OAuthToken:
    expires_in = data.get("expires_in")
    return OAuthToken(
        access_token=data["access_token"],
        refresh_token=data.get("refresh_token"),
        expires_at=time.time() + float(expires_in) if expires_in else None,
    )


async def poll_device_token(session: aiohttp.ClientSession, device: DeviceCode) -> OAuthToken:
    """Poll until the user approves (or the code expires)."""
    interval = device.interval
    while time.time() < device.expires_at:
        await asyncio.sleep(interval)
        try:
            async with session.post(
                ACCESS_TOKEN_URL,
                data={
                    "client_id": OAUTH_CLIENT_ID,
                    "device_code": device.device_code,
                    "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                },
                headers={"Accept": "application/json", "User-Agent": USER_AGENT},
                timeout=TIMEOUT,
            ) as resp:
                data = await resp.json(content_type=None)
        except (aiohttp.ClientError, TimeoutError, ValueError) as err:
            _LOGGER.debug("Device token poll failed: %s", err)
            continue
        if "access_token" in data:
            return _token_from(data)
        error = data.get("error")
        if error == "authorization_pending":
            continue
        if error == "slow_down":
            interval = int(data.get("interval", interval + 5))
            continue
        raise CopilotAuthError(f"Device flow failed: {error}")
    raise CopilotAuthError("Device code expired")


async def refresh_oauth_token(session: aiohttp.ClientSession, refresh_token: str) -> OAuthToken:
    """Refresh an expiring GitHub App user token."""
    try:
        async with session.post(
            ACCESS_TOKEN_URL,
            data={
                "client_id": OAUTH_CLIENT_ID,
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
            },
            headers={"Accept": "application/json", "User-Agent": USER_AGENT},
            timeout=TIMEOUT,
        ) as resp:
            data = await resp.json(content_type=None)
    except (aiohttp.ClientError, TimeoutError, ValueError) as err:
        raise CopilotUsageError(f"Token refresh failed: {err}") from err
    if "access_token" not in data:
        raise CopilotAuthError(f"Token refresh rejected: {data.get('error')}")
    return _token_from(data)


# --------------------------------------------------------------------------- #
# Copilot quotas (copilot_internal/user)
# --------------------------------------------------------------------------- #

QUOTA_NAMES = {
    "premium_interactions": "Premium requests",
    "chat": "Chat",
    "completions": "Completions",
}


@dataclass
class Quota:
    """One quota bucket."""

    key: str
    name: str
    entitlement: float | None
    remaining: float | None
    percent_remaining: float | None
    unlimited: bool
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def used(self) -> float | None:
        if self.entitlement is None or self.remaining is None:
            return None
        return max(self.entitlement - self.remaining, 0)

    @property
    def percent_used(self) -> float | None:
        if self.unlimited:
            return None
        if self.percent_remaining is not None:
            return round(100 - self.percent_remaining, 2)
        if self.entitlement and self.remaining is not None:
            return round((1 - self.remaining / self.entitlement) * 100, 2)
        return None


@dataclass
class QuotaUsage:
    """Parsed copilot_internal/user response."""

    login: str | None
    plan: str | None
    sku: str | None
    reset_at: datetime | None
    quotas: dict[str, Quota]
    raw: dict[str, Any] = field(repr=False)


def _num(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _parse_date(value: Any) -> datetime | None:
    if not value:
        return None
    text = str(value)
    try:
        if len(text) == 10:
            d = date.fromisoformat(text)
            return datetime(d.year, d.month, d.day, tzinfo=UTC)
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def parse_quota_usage(data: dict[str, Any]) -> QuotaUsage:
    """Parse both the quota_snapshots format and the older free-plan format."""
    quotas: dict[str, Quota] = {}
    snapshots = data.get("quota_snapshots")
    if isinstance(snapshots, dict):
        for key, snap in snapshots.items():
            if not isinstance(snap, dict):
                continue
            known = {"entitlement", "remaining", "quota_remaining", "percent_remaining", "unlimited"}
            remaining = snap.get("remaining", snap.get("quota_remaining"))
            quotas[key] = Quota(
                key=key,
                name=QUOTA_NAMES.get(key, key.replace("_", " ").capitalize()),
                entitlement=_num(snap.get("entitlement")),
                remaining=_num(remaining),
                percent_remaining=_num(snap.get("percent_remaining")),
                unlimited=bool(snap.get("unlimited")),
                extra={k: v for k, v in snap.items() if k not in known},
            )
    else:
        remaining = data.get("limited_user_quotas") or {}
        totals = data.get("monthly_quotas") or {}
        for key in set(remaining) | set(totals):
            ent, rem = _num(totals.get(key)), _num(remaining.get(key))
            quotas[key] = Quota(
                key=key,
                name=QUOTA_NAMES.get(key, key.capitalize()),
                entitlement=ent,
                remaining=rem,
                percent_remaining=(rem / ent * 100) if ent and rem is not None else None,
                unlimited=False,
            )
    reset = (
        data.get("quota_reset_date_utc")
        or data.get("quota_reset_date")
        or data.get("limited_user_reset_date")
    )
    return QuotaUsage(
        login=data.get("login"),
        plan=data.get("copilot_plan"),
        sku=data.get("access_type_sku"),
        reset_at=_parse_date(reset),
        quotas=quotas,
        raw=data,
    )


def _copilot_headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"token {token}",
        "Accept": "application/json",
        "Editor-Version": EDITOR_VERSION,
        "Editor-Plugin-Version": EDITOR_PLUGIN_VERSION,
        "User-Agent": USER_AGENT,
        "X-GitHub-Api-Version": "2025-04-01",
    }


async def fetch_quota_usage(session: aiohttp.ClientSession, token: str) -> QuotaUsage:
    """GET copilot_internal/user."""
    try:
        async with session.get(
            COPILOT_USER_URL, headers=_copilot_headers(token), timeout=TIMEOUT
        ) as resp:
            if resp.status == 401:
                raise CopilotAuthError("GitHub token rejected")
            if resp.status in (403, 404):
                raise CopilotNotEntitledError(
                    f"HTTP {resp.status}: {(await resp.text())[:200]}"
                )
            if resp.status != 200:
                raise CopilotUsageError(f"HTTP {resp.status}: {(await resp.text())[:200]}")
            data = await resp.json(content_type=None)
    except (aiohttp.ClientError, TimeoutError) as err:
        raise CopilotUsageError(f"Copilot request failed: {err}") from err
    return parse_quota_usage(data)


# --------------------------------------------------------------------------- #
# Billing REST API
# --------------------------------------------------------------------------- #


def _api_headers(token: str, version: str = GITHUB_API_VERSION) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": version,
        "User-Agent": USER_AGENT,
    }


async def fetch_login(session: aiohttp.ClientSession, token: str) -> str | None:
    """Return the login of the token owner."""
    try:
        async with session.get(
            f"{API_BASE}/user", headers=_api_headers(token), timeout=TIMEOUT
        ) as resp:
            if resp.status == 401:
                raise CopilotAuthError("GitHub token rejected")
            if resp.status != 200:
                return None
            return (await resp.json(content_type=None)).get("login")
    except (aiohttp.ClientError, TimeoutError) as err:
        raise CopilotUsageError(str(err)) from err


@dataclass
class BillingTotals:
    """Summed usage items."""

    quantity: float = 0.0
    gross_amount: float = 0.0
    net_amount: float = 0.0
    by_model: dict[str, float] = field(default_factory=dict)


@dataclass
class BillingUsage:
    """Month-to-date and today's billed Copilot usage."""

    month_start: datetime
    day_start: datetime
    premium_month: BillingTotals | None
    premium_today: BillingTotals | None
    credits_month: BillingTotals | None
    credits_today: BillingTotals | None


def _sum_items(data: dict[str, Any]) -> BillingTotals:
    totals = BillingTotals()
    for item in data.get("usageItems") or []:
        qty = float(item.get("grossQuantity") or item.get("quantity") or 0)
        totals.quantity += qty
        totals.gross_amount += float(item.get("grossAmount") or 0)
        totals.net_amount += float(item.get("netAmount") or 0)
        model = item.get("model") or "unknown"
        totals.by_model[model] = totals.by_model.get(model, 0) + qty
    totals.gross_amount = round(totals.gross_amount, 4)
    totals.net_amount = round(totals.net_amount, 4)
    return totals


async def _billing_get(
    session: aiohttp.ClientSession,
    token: str,
    target_type: str,
    target: str,
    kind: str,
    params: dict[str, str],
) -> BillingTotals | None:
    """Return None if this report doesn't exist for the account."""
    scope = "organizations" if target_type == TARGET_ORG else "users"
    url = f"{API_BASE}/{scope}/{target}/settings/billing/{kind}/usage"
    versions = (GITHUB_API_VERSION, AI_CREDIT_API_VERSION)
    for version in versions:
        try:
            async with session.get(
                url, headers=_api_headers(token, version), params=params, timeout=TIMEOUT
            ) as resp:
                if resp.status == 401:
                    raise CopilotAuthError("GitHub token rejected")
                if resp.status == 403:
                    raise CopilotAuthError(
                        "Token lacks permission (fine-grained PAT with 'Plan: read' "
                        "or org 'Administration: read' is required)"
                    )
                if resp.status in (400, 404, 410, 422):
                    continue  # try next API version / report not available
                if resp.status != 200:
                    raise CopilotUsageError(f"{kind} HTTP {resp.status}")
                return _sum_items(await resp.json(content_type=None))
        except (aiohttp.ClientError, TimeoutError) as err:
            raise CopilotUsageError(f"Billing request failed: {err}") from err
    return None


async def fetch_billing_usage(
    session: aiohttp.ClientSession, token: str, target_type: str, target: str
) -> BillingUsage:
    """Premium-request and AI-credit usage for the current UTC month and day."""
    now = datetime.now(UTC)
    month = {"year": str(now.year), "month": str(now.month)}
    day = {**month, "day": str(now.day)}
    results = await asyncio.gather(
        _billing_get(session, token, target_type, target, "premium_request", month),
        _billing_get(session, token, target_type, target, "premium_request", day),
        _billing_get(session, token, target_type, target, "ai_credit", month),
        _billing_get(session, token, target_type, target, "ai_credit", day),
    )
    if all(r is None for r in results):
        raise CopilotUsageError(f"No billing usage reports available for {target}")
    return BillingUsage(
        month_start=now.replace(day=1, hour=0, minute=0, second=0, microsecond=0),
        day_start=now.replace(hour=0, minute=0, second=0, microsecond=0),
        premium_month=results[0],
        premium_today=results[1],
        credits_month=results[2],
        credits_today=results[3],
    )
