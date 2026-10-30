# Changelog

All notable changes to **Aidoo** are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
Odoo-style versioning (`18.0.MAJOR.MINOR.PATCH`). Add new entries under
`## [Unreleased]`; a maintainer assigns the version at release time.

## [Unreleased]

### Added
- **Change requests and approvals** — a persona proposes a change through `orm_action` (`aidoo.change.request`); one of the persona's principals (`principal_user_ids`) approves it in Odoo and it applies with their rights, or goes stale if the record moved. `aidoo_guard.require_human` lets a method refuse a tool call; every tool call runs with `aidoo_tool_call` and the session id in the context. `aidoo.action_methods_deny` (default `action_archive,action_unarchive`) is refused to tools but can still be proposed.
- **Message groups and chat title** — collapsible, draggable message groups per conversation (`aidoo.message.group`), message timestamps, and a model-written conversation title.
- **Sidebar folders** — per-user conversation folders (`aidoo.session.folder`), collapsible Favorites/folder/date sections, drag a conversation between folders.
- **Claude streaming** — assistant text deltas are streamed into the chat; native text deltas are batched and the bus carries deltas instead of the full body.
- **Codex `read_attachment`** — a local tool so native Codex can open attachments.
- **Grok is now a native in-process provider** — `aidoo.provider.grok` talks to xAI's OpenAI-compatible API (inherits the Z.AI native loop), so no `grok` binary is required. Credentials are unchanged (`grok login` auth.json with client-side refresh, or an xAI API key); default model `grok-4.7`, optional `aidoo.grok.base_url`. The legacy subprocess adapter is kept unregistered in `grok_cli.py`.

### Changed
- **Path-scoped OAuth discovery** — the issuer is now `<host>/aidoo`; metadata is served at `/.well-known/oauth-authorization-server/aidoo` (and `/aidoo/.well-known/oauth-authorization-server`) and `/.well-known/oauth-protected-resource/aidoo/mcp`. The bare `/.well-known/*` URLs are no longer claimed.
- Tool results are bounded in bytes, not only in rows; `orm_action` passes the calling session id in the context; the chat shows the server's error text.
- OAuth consent/error pages are wrapped in a centred container.

### Fixed
- **MCP OAuth consent `access_denied` fix** — the consent page no longer uses the login form class (whose script could drop the clicked button's value) and posts an explicit hidden `decision`; a missing decision re-renders the page instead of returning `access_denied`, and consent decisions are logged.

### Added
- **MCP OAuth for ChatGPT / Claude connectors** — `/aidoo/mcp` now answers unauthenticated requests with a `WWW-Authenticate: Bearer resource_metadata=...` challenge and Aidoo serves an OAuth 2.1 authorization server (RFC 9728/8414 metadata, RFC 7591 dynamic client registration, authorization code + PKCE `S256`, rotating refresh tokens, consent page). Access tokens are Odoo API keys of scope `aidoo_mcp` (revocable under Preferences → Account Security); manual API keys keep working. OAuth follows `aidoo.mcp_enabled`.
- **Remote MCP server** — `POST /aidoo/mcp` (Streamable HTTP, JSON-only) lets external MCP clients (Claude Desktop, Cursor, Claude Code `--transport http`) use the assistant's tools. Enabled by default; the Settings → Aidoo → Remote MCP Server checkbox (`aidoo.mcp_enabled`) disables it (disabled returns 403 with a hint; `GET` returns 405 with a JSON description). Authenticates with a user's own API key of scope `aidoo_mcp`; calls run through the shared `aidoo.tool.exec` pipeline (per-user tool grant, model denylist, non-superuser, `aidoo.tool_log` audit with empty session). Tool definitions are shared in `models/mcp_tools.py`; the stdio bridge is unchanged.

### Changed
- **Codex is now a native in-process provider** — like the native Gemini and
  Z.AI adapters, `aidoo.provider.codex` no longer drives a CLI subprocess: it
  talks to the ChatGPT Codex backend (`chatgpt.com/backend-api/codex/responses`,
  Responses-API SSE) directly from Python with the user's ChatGPT-subscription
  OAuth tokens (`$CODEX_HOME/auth.json`, unchanged location/format), and runs
  the tool-calling loop in-process via `aidoo.tool.exec`. **No `codex` binary is
  required anymore.** The device-code login is reimplemented in pure Python
  (`codex_client.device_code_start` / `device_code_poll_and_exchange`, same OWL
  device-poll panel), an `auth.json` produced by `codex login` elsewhere can be
  pasted via credential import, and token refresh (with refresh-token rotation)
  is handled client-side. New stdlib-only client in
  `models/providers/codex_client.py`; the legacy subprocess adapter is preserved
  unregistered in `codex_cli.py`. Settings: default model is now `gpt-5.5`
  (the native request must pin a model, and the ChatGPT backend rejects
  codex-branded ids like `gpt-5-codex` — those are API-key-only), the CLI-path
  setting left the UI, and `aidoo.codex.base_url` + `aidoo.codex.rpm_limit`
  were added.

### Added
- **Audio / voice-note uploads with local transcription** — the chat composer and
  Discuss attachments now accept audio files (`.mp3 .wav .m4a .ogg .opus .flac
  .aac .webm`). Audio is never handed to the model directly (the CLI's Read tool
  cannot consume it): a standalone [faster-whisper](https://github.com/SYSTRAN/faster-whisper)
  wrapper (`bridge/transcribe_audio.py`, running under its own venv, never
  importing Odoo) transcribes it **on the server** in the background runner
  thread before the provider dispatches, and the prompt manifest points the
  model at the resulting `<file>.transcript.txt` in the session uploads dir.
  Transcripts are cached on the attachment (`description`, marker-prefixed) so
  re-materialization never re-transcribes; a transcription failure writes an
  explanatory note instead of crashing the turn. Configurable via
  `ir.config_parameter` (`aidoo.whisper_python` / `whisper_model` /
  `whisper_model_dir` / `whisper_threads` / `whisper_timeout_s` /
  `whisper_language`). Documented in SECURITY.md.
- **Sessions and scheduled agents take multiple skills** — `skill_id` becomes a
  `skill_ids` M2M on both `aidoo.session` and `aidoo.scheduled` (migrated in
  18.0.1.4.0); the chat's skill picker multi-selects, and `/aidoo/skill` /
  `/aidoo/new` accept `skill_ids` (the single-id `skill_id` param stays as a
  legacy alias).
- **Personas are first-class Discuss contacts** — persona bots now show up in
  Discuss's "New message" and "Invite people" searches (they were hidden because
  their service users are portal accounts), so a persona can be DM'd or invited
  from the UI; a 1:1 chat answers every message without needing an @mention. A
  new **Open chat** button on the persona form jumps straight into the DM, and a
  persona can carry its own **email identity** (written through to its service
  user) used when it sends or posts. Files attached to a Discuss message are
  materialized into the persona's session uploads so the model can read them.
- **Scheduled agents can run as a persona** — a new `persona_id` on
  `aidoo.scheduled`: the persona's provider, model override and connected
  service account drive the run (the agent's own Provider / Run As stay the
  fallback), its skills are copied onto the form as a starting point, and
  email/chatter deliveries are sent under the persona's identity. Tools still
  execute with the Run As user's Odoo permissions.

### Fixed
- **Artifact viewer can now load same-origin images and fonts** — the
  `GET /aidoo/artifact/<id>` CSP allows `img-src/font-src 'self'` (e.g.
  `/logo.png`) in addition to `data:`; still sandboxed, no scripts, no
  cross-origin subresources.

### Added
- **Generated HTML artifacts are persisted and shareable** — every ```` ```html ````
  block the assistant produces (and every report) is stored as an `ir.attachment`
  on its session at run end and served through the new hardened
  `GET /aidoo/artifact/<id>` route (explicit `text/html` + CSP
  `sandbox; default-src 'none'; style-src 'unsafe-inline'` — no scripts, no
  network subresources, opaque origin) instead of a client-side blob. The chat's
  artifact panel and "open in new tab" load the server URL; a new **Share** button
  mints a public tokenized link (core `ir.attachment` access token, owner-only,
  revocable via **Revoke**), so artifacts can be shared internally or externally.
  Persona replies in Discuss append `View:` links that any channel member can
  open (the route grants channel members read access). Historical messages keep
  rendering inline as before.

### Fixed
- **Persona replies in Discuss are now formatted** — the assistant's Markdown
  answer was posted verbatim (raw `*`, `#`, backticks). It is now rendered to
  sanitized HTML (`aidoo.session.aidoo_md_to_html`: headings, bold/italic, links,
  inline + fenced code, ordered/unordered lists, **GFM tables**) before posting,
  escape-first so model output cannot inject markup. The renderer returns `Markup`
  so `message_post` renders it instead of showing escaped `<p>…` tags as text.
- **Personas now actually adopt their system prompt** — the persona's
  `instructions` were appended after (and dominated by) the generic "you are the
  user's assistant" identity block, so the model largely ignored them. `_run_identity`
  now leads with the persona identity/instructions marked authoritative and reframes
  the Odoo user context as the interlocutor + permission scope, not the model's own
  identity.
- **Posting in a Discuss channel could crash with a duplicate `persona_default`
  xmlid** — the default persona was (re)seeded lazily on every inbound message via
  `env.ref`, whose ormcache a raw `ir.model.data` create never invalidated, so it
  kept missing and retried the insert (`UniqueViolation`), aborting the whole
  `message_post` transaction. The default persona is now seeded only by the
  post-init hook; `_ensure_default_persona` resolves the xmlid via a direct
  `ir.model.data` lookup and registers it cache-safely with `_update_xmlids`, and
  `_active_personas` is read-only (no creation on the hot path).
- **@mentioning a persona no longer leaves the sender's message greyed out with an
  error** — persona service users are portal (email-only) accounts with a bot
  address, so Odoo tried to email them on a mention and failed. `discuss.channel`
  now excludes persona bots from notification recipients (`_notify_get_recipients`);
  the persona still receives the message through `_maybe_handle_discuss`.

### Added
- **AI Personas — named Discuss bots** — admins (AI Assistant Managers) can create
  multiple named personas (`aidoo.persona`, new **Aidoo -> Personas** menu), each
  with its **own dedicated AI provider account**, an optional model override, a
  personality/system prompt and a bundle of `skill_ids`. Users invoke a persona in
  Discuss by `@mention` (e.g. `@SalesBot`) — it auto-joins the channel on first
  mention, answers every message in a 1:1 DM, and shows a live **"typing…"**
  indicator while it works. Each persona owns a dedicated, login-less **portal**
  service `res.users` (`bot_user_id`, `share=True`, so it consumes no internal seat
  and never acts) that holds its provider credentials on disk and its Discuss
  identity; connect/disconnect the account from the persona form (reusing
  the provider `_auth_*` flows). Credentials load under the persona's account while
  every ORM/SQL tool still runs as the **invoking user** with their own ACLs — see
  the credential-user vs acting-user split in `aidoo.session._credential_user()`.
  The existing `@aidoo` bot is now the built-in **default persona** (adopts the
  legacy bot partner, so existing DMs/mentions keep working). Documented in
  SECURITY.md.
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
- **Minute-level agent schedules** — `aidoo.scheduled` gains `Minutes` as an
  interval unit (`models/aidoo_scheduled.py`), and the master dispatcher cron's
  tick is now configurable from **Settings → Aidoo AI Assistant** via a new
  `Scheduler Check Interval (minutes)` setting (`aidoo.dispatch_interval_minutes`,
  default 15) that writes through to the `cron_ai_scheduled` record — so an admin
  can let agents run more frequently than the previous hard-coded 15-minute floor.
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
- **Scheduled-agents search view bare Group By** — the `aidoo.scheduled.search`
  Group By container now uses a bare `<group>` (dropping the `expand="0"`/`string`
  attributes) to match core views (`views/aidoo_scheduled_views.xml`), keeping the
  view definition aligned with the 19.0 source.
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
