"""Constants for the Copilot Usage integration."""

from __future__ import annotations

from datetime import timedelta
from typing import Final

DOMAIN: Final = "copilot_usage"

CONF_AUTH_TYPE: Final = "auth_type"
CONF_TOKEN: Final = "token"
CONF_REFRESH_TOKEN: Final = "refresh_token"
CONF_EXPIRES_AT: Final = "expires_at"
CONF_TARGET_TYPE: Final = "target_type"
CONF_TARGET: Final = "target"
CONF_SCAN_INTERVAL: Final = "scan_interval"

AUTH_DEVICE: Final = "device"
AUTH_TOKEN: Final = "token"
AUTH_BILLING: Final = "billing"

TARGET_USER: Final = "user"
TARGET_ORG: Final = "organization"

DEFAULT_SCAN_QUOTA: Final = timedelta(minutes=10)
DEFAULT_SCAN_BILLING: Final = timedelta(hours=1)
MIN_SCAN_INTERVAL: Final = 60

# GitHub OAuth device flow, using the public client id of the Copilot editor plugins.
OAUTH_CLIENT_ID: Final = "Iv1.b507a08c87ecfe98"
DEVICE_CODE_URL: Final = "https://github.com/login/device/code"
ACCESS_TOKEN_URL: Final = "https://github.com/login/oauth/access_token"
OAUTH_SCOPE: Final = "read:user"

API_BASE: Final = "https://api.github.com"
COPILOT_USER_URL: Final = f"{API_BASE}/copilot_internal/user"
GITHUB_API_VERSION: Final = "2022-11-28"
AI_CREDIT_API_VERSION: Final = "2026-03-10"
EDITOR_VERSION: Final = "vscode/1.105.0"
EDITOR_PLUGIN_VERSION: Final = "copilot-chat/0.32.0"
USER_AGENT: Final = "GitHubCopilotChat/0.32.0 (ha-copilot-usage)"
