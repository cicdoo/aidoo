# Changelog

All notable changes to **Aidoo** are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
Odoo-style versioning (`19.0.MAJOR.MINOR.PATCH`). Add new entries under
`## [Unreleased]`; a maintainer assigns the version at release time.

## [Unreleased]

### Fixed
- **Dark mode visibility** — the chat UI (`static/src/scss/aidoo.scss`) hardcoded
  light-mode colors, so backgrounds, message bubbles, borders and text became
  unreadable when Odoo's dark mode was enabled. Colors now reference Odoo's
  palette variables (`$o-gray-*`, `$o-view-background-color`, `$o-main-text-color`,
  `$o-component-active-bg`, `$o-danger`, `$o-brand-primary`), which the
  `web.assets_web_dark` bundle flips automatically — so the stylesheet adapts to
  both themes from a single source, with light mode unchanged.

<!--
Add bullets here under ### Added / Changed / Fixed / Removed / Security.
When adding or changing a provider, say which adapter
(`models/providers/<code>.py`) and whether the provider/CLI contract changed.
-->

## [19.0.1.0.0] — First public release

First release of **Aidoo**, the unified successor to the per-provider modules
`claudoo` / `codexoo` / `geminoo` / `grokoo`: one in-Odoo AI assistant with the
**AI provider chosen per conversation**, on Odoo **19.0**.

### Added
- **Per-conversation provider choice** — pick **Claude** (Anthropic Claude Code),
  **Codex** (OpenAI), **Gemini** (Google) or **Grok** (xAI) when starting a chat;
  each `aidoo.session` stores its `provider`. A provider picker and per-session
  badge live in the chat UI; the instance default is set in *Settings → Aidoo*.
- **Provider-adapter architecture** — everything provider-specific lives behind
  `aidoo.provider.<code>` adapter `AbstractModel`s in `models/providers/`
  (`base.py` defines the contract; `claude/codex/gemini/grok.py` implement it).
  Each adapter owns CLI invocation (`_prepare_run`), stream parsing into a common
  op format (`_normalize_event`), prompt assembly (`_build_prompt`) and
  authentication. The session/runner/bridge/uploads/tool-gating core is shared.
- **Per-user, per-provider authentication** with the right flow for each engine:
  Claude OAuth (paste-back code), Codex device-code login (poll), and
  Gemini/Grok credential-import **or** API key. Credentials are stored privately
  per user and per provider, mode `0600`, never in the database.
- **Chat client action** (OWL) with real-time streaming over the Odoo bus, file
  uploads (sandboxed `Read`), and a right-hand **artifact panel** that renders
  charts / HTML reports in a sandboxed iframe (no scripts). A `report` message
  role plus `aidoo.session._post_report(html)` let server code push a report into
  the conversation.
- **Permission-aware ORM tools** — every action runs as the *current user*
  (never superuser); `ir.model.access` and record rules are always enforced.
- **Read-only SQL reporting** (`sql_select`) gated to the *AI SQL Analyst* group,
  guarded by a SELECT-only validator plus Postgres `SET TRANSACTION READ ONLY`
  and a statement timeout.
- **Hard-denied built-ins** — the model can act *only* through `mcp__odoo__*`
  tools; Bash/Write/Web/agent-spawners are denied (each CLI via its own
  allow/deny/exclude mechanism).
- **Immutable audit log** (`aidoo.tool_log`) written on a separate committed
  cursor so it survives rollbacks.
- **Zero-trust mode** (global default + per-user override) that strips all write
  tools, and per-user tool grants (`res.users.aidoo_tool_ids`).
- **Test suite** (`--test-tags aidoo`) covering the SQL guard, bridge-token
  mint/verify, tool-access policy, the `ai_bridge` auth method, the audit log,
  and a cross-provider adapter contract suite (`test_providers.py`).

### Changed
- **One set of models, one config namespace.** Models are `aidoo.*`; system
  parameters are `aidoo.<provider>.<key>` with fallback to a shared `aidoo.<key>`
  (see `aidoo.session._pconfig`). The session id field is `cli_session_id` and
  the message id field is `cli_msg_id` (provider-neutral).
- **Read sandbox parity** — the `read_guard.py` upload-dir sandbox now applies
  across all providers (each enforces it with its CLI's own mechanism).

### Ported to Odoo 19.0
- `res.groups` grouping moved to `res.groups.privilege` (groups lost
  `category_id`); user groups field is `group_ids`/`all_group_ids`; controller
  routes use `type="jsonrpc"`; SQL constraints use `models.Constraint`;
  `from odoo.modules.registry import Registry`; the `act_window` `inline` target
  was removed.

### Licensing
- **Dual-licensed**: open-source **LGPL-3.0-or-later** *or* a **commercial
  license** from CICDoo (see `COMMERCIAL_LICENSE.md`). Every source file carries
  an SPDX `LGPL-3.0-or-later OR LicenseRef-Aidoo-Commercial` notice.
- Runs on **both Odoo Community and Enterprise** (depends only on `web`, `bus`).
  Each provider's CLI (`claude` / `codex` / `gemini` / `grok`) must be installed
  on the server for that provider to be usable.

[Unreleased]: https://github.com/cicdoo/aidoo/compare/19.0.1.0.0...HEAD
[19.0.1.0.0]: https://github.com/cicdoo/aidoo/releases/tag/19.0.1.0.0
