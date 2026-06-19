# Changelog

All notable changes to **Aidoo** are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
Odoo-style versioning (`18.0.MAJOR.MINOR.PATCH`). Add new entries under
`## [Unreleased]`; a maintainer assigns the version at release time.

## [Unreleased]

### Added
- **Native (in-process) Gemini provider** — Gemini now talks to the Google Code
  Assist API directly from Python (`models/providers/codeassist.py`) and runs the
  agent tool-calling loop in-process, instead of driving the Node `gemini` CLI,
  which cannot complete HTTPS to Google in some egress environments (responses
  reset mid-body). Reuses the existing `oauth_creds.json` import flow; streams
  responses and executes Odoo tools in-process. The legacy CLI adapter is kept
  unregistered in `models/providers/gemini_cli.py` as a fallback.
- **Antigravity provider** — a new `aidoo.provider.antigravity` adapter drives
  Google's Antigravity `agy` CLI for individual / Google One users who are blocked
  from the Gemini CLI. Text-only; experimental and opt-in
  (`aidoo.antigravity.enabled`) because its web tools cannot be sandboxed (see
  SECURITY.md).
- **In-process provider runner path** — `aidoo.runner` gains a native execution
  path (`_is_native` providers drive an `_run_stream` generator) that reuses the
  existing op/message/bus machinery without spawning a subprocess.
- **Scheduled AI agents** — a new `aidoo.scheduled` model
  (`models/aidoo_scheduled.py`) lets a user save a prompt + provider + schedule
  that runs autonomously. A master cron ("Aidoo: run scheduled agents",
  `data/aidoo_action.xml`, every 15 min) launches each due agent as a normal
  `aidoo.session` owned by the agent's user — so it runs with that user's
  credentials, tool grant and permissions through the existing runner — and
  delivers the answer by **email**, to a **record's chatter**, or leaves it as a
  **chat** conversation. Backend list/form views + menu
  (`views/aidoo_scheduled_views.xml`) with own/manager record rules and ACLs.
- **Aidoo in Discuss** — Aidoo now answers as a bot in the Discuss app.
  `models/discuss_channel.py` extends `discuss.channel._message_post_after_hook`
  (OdooBot's pattern): a human message in a channel the bot belongs to launches a
  turn as the message author and posts the reply back into the channel. It is
  loop-guarded against the bot's own posts, and tells unauthenticated users to
  connect their provider in the Aidoo app. The bot partner is created at runtime
  (see note below).
- **Floating "Ask Aidoo" companion** — a button on form and list views
  (`static/src/floating_button.js`/`.xml`) opens a chat panel that **floats over
  the current view** (the record/list stays visible and usable behind it), with
  **expand**, **minimize** and **close**. It reuses the existing message list,
  composer and bus streaming. Seeded with the view's context — the record on a
  form, or the **filtered record set** (the list's effective search domain) on a
  list — captured live via a reactive service (`static/src/view_context_service.js`)
  fed by form/list controller patches (`static/src/view_patches.js`).
- **"Ask Aidoo" record action** — a form-view cog-menu item
  (`static/src/cog_menu_item.js`/`.xml`) opens the full Aidoo app seeded with the
  current record, for users in the *AI Assistant User* group.
- **Conversation context seeding** — `aidoo.session` gains `context_model`,
  `context_res_id` and `context_domain`. `_compose_prompt` prepends a focus note
  on the first turn (the record, or the model + filter domain), resolved under the
  user's own rights so an unreadable record is silently dropped. `/aidoo/new`
  accepts and validates the context.
- **Bookmark & search conversations** — `aidoo.session` gains an `is_favorite`
  boolean. The full app's sidebar (`static/src/chat_action.js`/`.xml`) adds a
  per-conversation **star** toggle (persisted via a new `/aidoo/favorite` route),
  a **search box** that filters the list by name, and a **"favorites only"**
  filter; starred conversations sort to the top.

### Changed
- **Single tool-execution path** — tool enforcement + audit moved out of the HTTP
  controller into a shared `aidoo.tool.exec` model (`models/aidoo_tool_exec.py`).
  The loopback MCP bridge controller (`controllers/tools.py`) is now a thin
  delegator, and native in-process providers call the same executor directly, so
  `su=False` and the immutable audit log apply identically on both paths.
- **Empty conversations are pruned automatically** — a session is created eagerly
  when a chat opens, so opening the floating bubble (or the app) and never typing
  left an empty "New conversation" in history. The app now cleans these up:
  `aidoo.session._prune_empty()` deletes the user's own message-less, non-running
  sessions (excluding Discuss/scheduled-linked ones) and runs on every
  `/aidoo/sessions` load; the floating companion discards its own empty session on
  **close**, **new chat** and unmount via a new `/aidoo/discard` route (server-side
  guarded to delete only when the session has no messages).
- **Post-run delivery hook** — the runner now calls
  `aidoo.session._deliver_result(ok)` at every terminal state
  (`models/aidoo_runner.py`); plain chat is unaffected, while scheduled agents and
  the Discuss bot use it to deliver their answer. Adds a `_last_assistant_text()`
  helper on the session.
- **New module dependency: `mail`** — required by the Discuss bot and the
  email/chatter delivery of scheduled agents (`__manifest__.py`).

### Note
- **Discuss bot partner is created at runtime**, not via XML data or a
  `post_init_hook` — creating a `res.partner` during module data-load races other
  apps' required fields (e.g. `account.autopost_bills`), whose defaults the data
  loader does not fill in. `aidoo.session._aidoo_bot_partner()` creates it lazily
  via the ORM (defaults applied correctly) and is invoked when the Aidoo app is
  opened (`/aidoo/providers`) and from the Discuss hook.

### Fixed
- **Gemini rate-limit (429) resilience** — the native Gemini adapter now mirrors
  the node CLI's retry instead of failing the turn: exponential backoff with
  enough attempts, honoring the `Retry-After` header and gRPC
  `RetryInfo`/quota-reset hints, plus a one-shot fallback to a lighter model
  (`aidoo.gemini.fallback_model`, default `gemini-2.5-flash` — a separate quota
  bucket) when the primary stays limited. A transient "rate-limited, retrying…"
  notice (new `notice` bus event) shows during the wait. Replies no longer
  hard-fail on transient `429 RESOURCE_EXHAUSTED`.
- **Gemini session resume error** — Gemini headless cannot reliably resume a
  server-side session, yet the adapter passed `--resume <id>` with a pre-minted
  UUID the CLI had never stored, aborting the turn with "No previous sessions
  found for this project." Gemini no longer passes `--resume` (history is replayed
  into the prompt), and `cli_session_id` is only pre-minted for providers that
  client-mint it (`_mints_session_id`, i.e. Claude); the others learn their id
  from the CLI stream.
- **Tool-call chip stuck spinning** — a completed tool call could keep showing the
  running spinner after its turn finished. Two causes in `models/aidoo_runner.py`:
  (1) `_patch_tool_result` mutated the `tool_calls` JSON list in place and
  reassigned the same object, so Odoo saw no change and skipped the DB write —
  the `done`/`error` status was lost on the frontend's post-turn reload. It now
  rebuilds the list with fresh dicts so the write persists. (2) A call whose
  result event never streamed (CLI exited mid-tool, timed out, or was stopped)
  stayed `running` forever; a new `_finalize_pending_tools` helper settles any
  lingering `running` calls to a terminal status on every turn-end path (clean
  done, non-zero exit, timeout, and the worker's exception handler).
- **Stacked tool-call folds** — a single assistant turn arrives as several
  messages (Claude tags each with its own id), so tool-only messages each
  rendered a separate collapsible "N tool calls" fold. `message_list.js` now
  exposes a `displayMessages` getter that merges consecutive assistant messages
  into one bubble — combining their bodies, tool calls and attachments — so a
  turn shows a single fold instead of a stack. Merging works on shallow clones,
  leaving the upstream chat state untouched.
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

## [18.0.1.0.0] — Odoo 18.0 backport

Backport of the 19.0 release to **Odoo 18.0**. No functional change — only the
framework APIs that differ between 18.0 and 19.0 were reverted. Feature parity
with `19.0.1.0.0` is preserved.

### Changed (ported to Odoo 18.0)
- **Groups & app category** — reverted `res.groups.privilege` (19-only) back to a
  `category_id` Many2one on each `res.groups` record, pointing at the existing
  `module_category_aidoo` (`security/aidoo_security.xml`). The role block in
  `models/res_users.py` now reads `group.category_id` over `self.groups_id`
  instead of `group.privilege_id` over `self.all_group_ids`.
- **Controller routes** — `type="jsonrpc"` → `type="json"` on every
  `@http.route` in `controllers/main.py` and `controllers/tools.py` (Odoo 18
  registers only the `http` and `json` dispatchers).
- **SQL constraints** — `models.Constraint(...)` (19-only) → the classic
  `_sql_constraints` list in `models/aidoo_tool.py`.
- **System prompt** — provider-neutral prompt now says "Odoo 18" instead of
  "Odoo 19" (`models/providers/base.py`).
- Test fixture in `tests/test_ir_http_auth.py` uses the 18 field name `groups_id`
  (was `group_ids`).
- **Docs & metadata** — `README.md` (Odoo badge + Requirements), `SECURITY.md`
  (supported versions), `CONTRIBUTING.md` (target version + rebase branch),
  `.github/pull_request_template.md` and `.github/ISSUE_TEMPLATE/bug_report.yml`
  now reference **18.0**.

### Removed
- Dropped `name_get` and `fields_view_get` from the `orm_call` `SAFE_METHODS`
  allowlist in `controllers/tools.py`; both were removed from the Odoo framework
  in 17.0 (superseded by `display_name`/`_compute_display_name` and
  `get_views`/`get_view`), so they were dead entries that no model exposes on 18.

> `from odoo.modules.registry import Registry` and the absence of an `act_window`
> `inline` target are valid on both 18.0 and 19.0, so they were left unchanged.
> `partner._bus_send(...)` also works as-is — `res.partner` inherits
> `bus.listener.mixin` on 18.0.

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

[Unreleased]: https://github.com/cicdoo/aidoo/compare/18.0.1.0.0...HEAD
[18.0.1.0.0]: https://github.com/cicdoo/aidoo/compare/19.0.1.0.0...18.0.1.0.0
[19.0.1.0.0]: https://github.com/cicdoo/aidoo/releases/tag/19.0.1.0.0
