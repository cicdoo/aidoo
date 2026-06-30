# Changelog

All notable changes to **Aidoo** are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
Odoo-style versioning (`19.0.MAJOR.MINOR.PATCH`). Add new entries under
`## [Unreleased]`; a maintainer assigns the version at release time.

## [Unreleased]

### Added
- **Floating companion is fully view-context aware** — the "Ask Aidoo" companion now seeds a conversation with what the user is actually looking at across **all** multi-record views (list, kanban, pivot, graph, calendar), not just list/form: the **selected records** (ticked rows), the active **filters**, **group-by** and **domain**. Captured via shared view-controller patches reading `env.searchModel` (domain/groupBy/facets) and `model.root.selection` (`static/src/view_patches.js`), forwarded by `floating_button.js` to `/aidoo/new` (new params: `context_view_type` / `context_groupby` / `context_filters` / `context_selected_ids`) and described to the model in `aidoo.session._context_note()`. Records are still re-read under the user's own rights at tool time. The companion now also appears on kanban/pivot/graph/calendar (was form/list only).
- **Per-user SQL access (allowlist) + working SQL toggle** — SQL access is now
  resolved per user in `aidoo.tool.exec._check_sql_access`. AI SQL Analyst members
  still read everything except the global denylist; every other user is
  **default-deny** unless a manager grants them a per-user table allowlist
  (`res.users.aidoo_sql_table_allowlist`, fnmatch globs e.g. `account_*`) — they
  may then read only those tables. This lets you give one user accounting reads
  without the full-database analyst role. The global `aidoo.sql_enabled` setting
  (previously defined but unenforced) now actually disables the SQL tool.
- **Configurable SQL table denylist** — a new `aidoo.sql_table_denylist` setting (Settings -> Tools & Security) lets admins block the read-only `sql_select` tool from reading sensitive tables (fnmatch globs, e.g. `account_*, hr_*`). Because raw SQL bypasses ORM record rules, this keeps tables like invoices/payroll out of reach even for AI SQL Analyst members. Enforced in `aidoo.tool.exec` by parsing the real FROM/JOIN table references (schema-qualified and quoted names included) before execution. Documented in SECURITY.md.

### Changed
- **Provider icon in the chat list** — each conversation in the sidebar now shows
  its provider's icon (tinted with the provider accent, provider name on hover)
  instead of a generic comment icon, and the uppercase provider text badge is
  removed. Each adapter declares a `_provider_icon` (Font Awesome) exposed via the
  provider descriptor (`models/providers/*.py`, `static/src/chat_action.xml`).

### Added
- **Delete a conversation** — a trash control on each conversation in the chat
  sidebar (`static/src/chat_action.js`/`.xml`) deletes the whole chat and its
  messages after a confirmation dialog, via a new owner-scoped `/aidoo/delete`
  route. It refuses while a run is in flight, and (like the other chat routes)
  only ever touches the caller's own sessions; the messages cascade with the
  session. Previously only empty conversations could be removed (`/aidoo/discard`).

### Changed
- **Chat history loads incrementally** — the conversation view no longer fetches
  every message up front. `aidoo.session._messages_page(limit, before_id)` and
  the `/aidoo/messages` route now return a newest-anchored page (default 30); the
  OWL client (`static/src/chat_action.js`, `components/message_list.*`) loads the
  most recent page first and fetches older messages on scroll-up — preserving the
  viewport so content doesn't jump — with a "Load earlier messages" button as a
  fallback when the page doesn't overflow. The floating companion keeps the
  unlimited legacy behavior (no `limit` passed). Keeps long conversations fast to
  open and light on the bus/DB.
### Added
- **Collapsible conversation list on mobile** — the chat sidebar (provider
  picker + conversation history) now collapses into an off-canvas drawer on
  small screens (`< md`). A hamburger button in a new mobile top bar
  (`static/src/chat_action.xml`/`.js`, `scss/aidoo.scss`) opens it; a backdrop
  tap or selecting a conversation closes it, returning focus to the chat. On
  desktop the sidebar is unchanged (always visible). The artifact preview panel
  is hidden on mobile so the conversation stays usable on a narrow viewport.

<!--
Add bullets here under ### Added / Changed / Fixed / Removed / Security.
When adding or changing a provider, say which adapter
(`models/providers/<code>.py`) and whether the provider/CLI contract changed.
-->

## [19.0.1.1.0] — 2026-06-30

### Added
- **Per-run context for scheduled agents** — a scheduled agent
  (`aidoo.scheduled`) can now carry context from its previous runs into each new
  run, configurable per agent (`context_mode`: none / last N messages / last N
  runs / full history, with `context_amount` = N). The prior context is read
  fresh from the agent's own session each run and fed in as a **non-persisted**
  prompt prefix (never stored, so it cannot compound), while each run keeps its
  isolated CLI conversation id so resume-based providers never replay an
  ever-growing transcript. Native adapters (Gemini, Z.AI) now gate their implicit
  history replay on the first-turn flag so the configured prefix is the single,
  non-duplicated source of prior context. Defaults to `none` (unchanged behavior).
- **Native (in-process) Z.AI (GLM) provider** — a new `aidoo.provider.zai`
  adapter (`models/providers/zai.py`) talks to z.ai's **GLM Coding Plan**
  endpoint — the OpenAI-compatible chat API at
  `https://api.z.ai/api/coding/paas/v4` — directly from Python
  (`models/providers/zai_client.py`, stdlib only) and runs the agent
  tool-calling loop in-process (`_is_native`), like the native Gemini adapter.
  Per-user auth is a z.ai API key (stored mode 0600 under `.zai`, never the DB)
  with an optional shared fallback (`aidoo.zai.api_key`); model and base URL are
  configurable (`aidoo.zai.model`, `aidoo.zai.base_url`). Tools execute through
  `aidoo.tool.exec`, preserving su=False enforcement and the audit log.
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
- **Minute-level agent schedules** — `aidoo.scheduled` gains `Minutes` as an
  interval unit (`models/aidoo_scheduled.py`), and the master dispatcher cron's
  tick is now configurable from **Settings → Aidoo AI Assistant** via a new
  `Scheduler Check Interval (minutes)` setting (`aidoo.dispatch_interval_minutes`,
  default 15) that writes through to the `cron_ai_scheduled` record — so an admin
  can let agents run more frequently than the previous hard-coded 15-minute floor.

### Changed
- **Scheduled agents reuse one conversation, isolated per run** — instead of
  spawning a new `aidoo.session` every run (flooding the chat list with identical
  agent chats), `aidoo.scheduled._run_once()` now reuses the agent's single
  conversation. Each run is still isolated: it mints a fresh `cli_session_id` and
  resets `turn_count` (so the turn is treated as the first — no resumed transcript
  and no replayed history for any provider), and inserts a new `divider`-role
  message (`models/aidoo_message.py`, rendered in `static/src/components/message.*`)
  as a visible run boundary. A run is skipped while the previous one is still
  running rather than stacking onto it.
- **Gemini default model is now `gemini-2.5-flash`** (was `gemini-2.5-pro`). On
  Code Assist the rate limit is **per-model**, and pro's is far lower than
  flash's — so pro 429s on nearly every turn while flash has the headroom for a
  smooth chat. Set `aidoo.gemini.model = gemini-2.5-pro` to opt back into pro
  (keep `aidoo.gemini.fallback_model = gemini-2.5-flash` for graceful 429s). Also
  added a proactive `aidoo.gemini.rpm_limit` (default 120) client-side cap on
  Code Assist requests/min (per project, per worker), and quieted the rate-limit
  notice so quick transient retries no longer flash a status.
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
- **Scheduled-agents search view installs on Odoo 19** — the
  `aidoo.scheduled.search` Group By container used `<group expand="0"
  string="Group By">`, but Odoo 19's search-view RNG no longer allows `expand`
  or `string` on `<group>`, which raised a `ParseError` and blocked module
  installation. Now uses a bare `<group>`, matching core views.
- **Native Gemini honors the configured Code Assist project** — the native
  adapter ignored `res.users.aidoo_cloud_project` / `aidoo.gemini.cloud_project`
  and always auto-onboarded into Google's individual/standard managed project. It
  now uses the configured project (per-user first, then shared) as the
  authoritative `cloudaicompanionProject`, which is required to use a Gemini Code
  Assist **Enterprise** tier (granted per GCP project + per-account license). No
  configured project keeps the previous auto behavior.
- **Gemini unavailable-model (404) fallback** — if the configured model is not
  enabled on the account/tier (404 NOT_FOUND, e.g. `gemini-3.5-flash`), the
  adapter now falls back to `aidoo.gemini.fallback_model` with a notice instead of
  failing. Available on the tested tier: `gemini-2.5-flash` (default),
  `gemini-2.5-pro`, `gemini-3-flash` (rate-limited).
- **Gemini 400 on tool schemas (gemini-2.5-flash)** — flash rejects an array
  property with no `items` (`GenerateContentRequest...parameters.properties[...]
  .items: missing field`), which pro tolerated. Odoo's freeform arrays (`domain`,
  `*args`, sql `params`) can't be typed in the OpenAPI subset, so they are now
  exposed to the model as a JSON-string param (with a hint), and `aidoo.tool.exec`
  parses the string back to a list/dict. The MCP bridge path (Claude/Codex/Grok)
  is unaffected — it still sends real arrays, which pass through unchanged.
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
- **Pre-release stabilization pass** — hardened the new in-process/native paths
  and closed Odoo 19 compatibility gaps ahead of merging to stable:
  - the scheduled-agent form view shipped a malformed `<group>` that failed to
    load; repaired.
  - `aidoo.scheduled.run_count` now declares `@api.depends("session_ids")`, and
    the `aidoo.tool_log` acting-user link uses `ondelete="set null"` so deleting
    a user no longer blocks on audit rows.
  - the runner drains CLI **stderr** on a dedicated thread (a >64KB stderr could
    deadlock the run) and exposes the turn deadline to native adapters; the Z.AI
    and Code Assist HTTP clients now cap their retry/backoff budget to that
    deadline so retries can't overrun the run.
  - `codeassist.ensure_project()` raises a clear error instead of returning a
    `None` project (which surfaced later as a confusing 401/403); the native
    Gemini round loop is bounded (no open `while True`); both native adapters
    hard-stop at the tool-round cap.
  - the shared bridge tool-schema cache is lock-guarded against a concurrent
    first-load; several silently-swallowed errors now log.
  - **Tests:** Odoo 19 renames fixed in the suite (`res.users.groups_id` →
    `group_ids`, `discuss.channel.channel_get` → `_get_or_create_chat`); added
    coverage for the native tool-calling loop (Z.AI + Gemini) and round cap, the
    shared tool executor (`aidoo.tool.exec`) enforcement contract, Discuss-bot
    error/guard paths, the `/aidoo/favorite` route, and the Antigravity
    descriptor. (Known gap: OWL/JS components still have no automated tests.)

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
