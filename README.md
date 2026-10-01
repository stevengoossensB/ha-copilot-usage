# Copilot Usage for Home Assistant

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://github.com/hacs/integration)
[![Validate](https://github.com/stevengoossensB/ha-copilot-usage/actions/workflows/validate.yml/badge.svg)](https://github.com/stevengoossensB/ha-copilot-usage/actions/workflows/validate.yml)

Track your **GitHub Copilot** usage in Home Assistant:

- **Copilot quotas** (Free / Pro / Pro+ / Business…): monthly premium-request quota used % and remaining, chat/completions quotas on the Free plan, plan and quota reset date. The same numbers as the Copilot status menu in VS Code.
- **Billed usage** (GitHub billing REST API): premium requests and AI credits this month and today, plus the amount billed (USD), for a personal account or an organisation.

Companion integrations: [ha-claude-usage](https://github.com/stevengoossensB/ha-claude-usage) · [ha-codex-usage](https://github.com/stevengoossensB/ha-codex-usage)

## Installation

1. HACS → ⋮ → **Custom repositories** → add `https://github.com/stevengoossensB/ha-copilot-usage` as type **Integration**.
2. Install **Copilot Usage** and restart Home Assistant.
3. Settings → Devices & services → **Add integration** → *Copilot Usage*.

[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=stevengoossensB&repository=ha-copilot-usage&category=integration)

## Setup

### Copilot quotas – sign in with GitHub (recommended)
Choose *Copilot quotas — sign in with GitHub*. Home Assistant shows a code; open github.com/login/device, enter it and approve. The flow continues on its own.

### Copilot quotas – paste a token
Paste a GitHub OAuth token of the Copilot user, e.g. the output of `gh auth token`.

### Billed usage
Create a **fine-grained personal access token** with the user permission **Plan → Read** (for an organisation, a token of an org owner/billing manager with **Administration → Read**). Choose *Copilot billed usage*, paste it, select personal account or organisation. Leave the name empty to use the token owner.

## Entities

| Mode | Entity | Notes |
|---|---|---|
| Quotas | `Premium requests used` | % of the monthly premium-request allowance; attributes: entitlement, remaining, overage |
| Quotas | `Premium requests remaining` | count |
| Quotas | `Chat used` / `Completions used` … | only for limited (non-unlimited) quotas, e.g. Copilot Free |
| Quotas | `Plan`, `Quota reset` | |
| Billing | `Premium requests this month` / `today` | gross quantity; `by_model` attribute |
| Billing | `Premium requests billed this month` / `today` | net USD (after included allowance) |
| Billing | `AI credits this month` / `today`, `AI credits billed …` | for accounts on AI-credit billing; unavailable otherwise |

Default polling: 10 min (quotas), 1 h (billing).

## Caveats

- Quotas come from `api.github.com/copilot_internal/user`, the endpoint the Copilot editor extensions use. It is not a documented public API and may change.
- The billing endpoints are documented (`/users/{user}/settings/billing/premium_request/usage` and `/ai_credit/usage`) but report with some delay; days and months are in UTC.

## License

MIT
