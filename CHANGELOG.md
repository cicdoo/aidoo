# Changelog

All notable changes to **Claudoo** are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
Odoo-style versioning (`18.0.MAJOR.MINOR.PATCH`). Add new entries under
`## [Unreleased]`; a maintainer assigns the version at release time.

## [Unreleased]

### Added
- **Rendered charts & HTML reports in chat** — the assistant can now produce
  visual output instead of raw markup. HTML emitted in a fenced ` ```html `
  block (or an untagged fence whose content is HTML) renders as a CSS/SVG chart
  or report inside a **sandboxed iframe** (`sandbox="allow-same-origin"`, no
  scripts — styles are isolated and the markup's own JS never runs). Rendered
  output opens in a dedicated **artifact panel on the right**, with the latest
  chart auto-opening and inline "Chart" cards to reopen any previous one. A new
  `report` message role plus a `claudoo.session._post_report(html)` helper let
  server-side code push a full report straight into the conversation.

- **Contribution gates** — a `pr-checks` CI workflow (CHANGELOG, DCO sign-off,
  SECURITY.md-when-relevant, flake8) plus branch protection, and `CONTRIBUTING.md`
  guidance covering AI-assisted contributions and the Developer Certificate of
  Origin (`DCO`).

### Changed
- **Tool calls are collapsed by default** in each assistant message, behind a
  toggle that shows the call count (and a spinner while any call is still
  running); expand to see the per-tool chips.

## [18.0.1.0.1] — 2026-06-11

### Added
- **Max Concurrent Runs** setting (`claudoo.max_concurrent_runs`, under
  *Settings → Claudoo AI Assistant*) — a soft global cap on how many CLI
  subprocesses may run at once across all users, to bound peak memory and
  protect the host from OOM. `0` = unlimited.

### Changed
- **Namespaced all `res.config.settings` and `res.users` fields** from the
  generic `ai_*` prefix to `claudoo_*`, matching the module's own
  `claudoo_tool_ids` and removing any overlap with sibling modules on these
  shared models (notably the previously shared `res.users` zero-trust column).
  A pre-migration (`18.0.1.0.1`) preserves each user's zero-trust selection
  across the rename. Field *labels* and behaviour are unchanged.
- **Dropped the unused `mail` dependency** — the addon drives `web` and `bus`
  directly and never used any `mail` feature; `depends` is now `['web', 'bus']`.

### Removed
- GitHub Actions CI workflow and tracked `__pycache__/*.pyc` build artifacts.

[18.0.1.0.1]: https://github.com/cicdoo/claudoo/releases/tag/18.0.1.0.1

## [18.0.1.0.0] — First public release

First open-source release of Claudoo, an in-Odoo AI assistant that drives the
Claude Code CLI over a sandboxed, permission-aware bridge.

### Features
- **Chat client action** (OWL) with real-time streaming over the Odoo bus.
- **Permission-aware ORM tools** — every action runs as the *current user*
  (never superuser); `ir.model.access` and record rules are always enforced.
- **Read-only SQL reporting** (`sql_select`) gated to the *AI SQL Analyst* group,
  guarded by a SELECT-only text validator plus Postgres `SET TRANSACTION READ ONLY`
  and a statement timeout.
- **Per-user OAuth** ("Login with Claude") — no shared API key; credentials are
  stored privately per user, mode `0600`, never in the database.
- **Hard-denied built-ins** — the model can act *only* through the `mcp__odoo__*`
  tools; Bash/Read/Write/Web/agent-spawners are denied.
- **Immutable audit log** (`claudoo.tool_log`) written on a separate committed
  cursor so it survives rollbacks.
- **Zero-trust mode** (global default + per-user override) that strips all write
  tools.
- **Test suite** covering the SQL guard, bridge-token mint/verify, tool-access
  policy, the `ai_bridge` auth method, and the audit log.

### Licensing
- **Dual-licensed**: open-source **LGPL-3.0-or-later** *or* a **commercial license**
  from CICDoo (see `COMMERCIAL_LICENSE.md`). Every source file carries an SPDX
  `LGPL-3.0-or-later OR LicenseRef-Claudoo-Commercial` notice.
- Runs on **both Odoo Community and Enterprise** editions (depends only on
  Community modules: `web`, `bus`, `mail`).

[18.0.1.0.0]: https://github.com/cicdoo/claudoo/releases/tag/18.0.1.0.0
