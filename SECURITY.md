# Security Policy

Aidoo bridges a large-language-model agent to your Odoo database, so we take
security seriously and welcome responsible disclosure.

## Supported versions

| Version  | Supported |
|----------|-----------|
| 19.0.x   | ✅        |

## Reporting a vulnerability

**Please do not open a public GitHub issue for security vulnerabilities.**

Instead, email **security@cicdoo.com** with:

- a description of the issue and its impact,
- steps to reproduce (a proof-of-concept is ideal),
- the Aidoo version and your Odoo version.

We aim to acknowledge reports within **3 business days** and to provide a
remediation timeline after triage. We will credit reporters in the release notes
unless you prefer to remain anonymous.

## Scope & design notes

Aidoo's threat model assumes the LLM output is **untrusted**. The defenses that
are in scope for security reports include:

- **No superuser execution** — tool enforcement + audit live in one place, the
  `aidoo.tool.exec` model, which aborts if `self.env.su` is true; every ORM call
  runs as the acting user with `ir.model.access` + record rules. Both callers go
  through it: the loopback MCP bridge controller (CLI providers) and the native
  in-process providers — so the per-user grant, model denylist and immutable
  audit log apply identically regardless of how a tool is reached.
- **Native (in-process) providers** — some providers (Gemini, Z.AI/GLM) run the
  agent loop inside Odoo and execute tools by calling `aidoo.tool.exec` directly
  (no CLI subprocess, no loopback HTTP, no MCP bridge token). They still run as the
  acting user (`su=False`) with the same gating + audit; the model's built-in
  surface is limited to the declared Odoo tools (no shell/file/web). The Z.AI
  provider only advertises the user's granted Odoo tools as OpenAI function specs;
  the executor re-checks the grant authoritatively at call time, so a model that
  fabricates a tool name or arguments still cannot exceed the user's permissions.
- **Read-only SQL** — `sql_select` is validated (SELECT/WITH only, no stacked
  statements, no row locks, no dangerous functions) *and* executed under
  `SET TRANSACTION READ ONLY` with a statement timeout. It is gated to the
  *AI SQL Analyst* group. Note that **raw SQL bypasses ORM record rules and model
  ACLs** — that group is effectively an instance-wide *read* grant, not a
  per-model one. To keep sensitive tables out of reach even for its members, set
  an **SQL table denylist** (Settings → *Tools & Security*, or the
  `aidoo.sql_table_denylist` parameter): fnmatch patterns (e.g. `account_*, hr_*,
  *_payslip`) matched against the real table references parsed from each query's
  `FROM`/`JOIN` clauses (schema-qualified and quoted names included), so a
  denied-table read is refused before execution. SQL access is resolved
  per user in `aidoo.tool.exec._check_sql_access`: AI SQL Analyst members may read
  everything except that denylist; **every other user is default-deny**, unless a
  manager sets a **per-user table allowlist** (`res.users.aidoo_sql_table_allowlist`)
  — then they may read *only* tables matching it (e.g. grant `account_*` to one
  user without the full-database analyst role). The global `aidoo.sql_enabled`
  toggle disables the SQL tool for everyone.
- **Capability tokens** — the MCP bridge holds only a short-lived, session-scoped
  HMAC bearer token (no DB credentials) and is reachable on loopback only.
- **Denied built-ins** — the model cannot reach Bash/Read/Write/Web; it acts only
  through the `mcp__odoo__*` tools.
- **Audio transcription is an isolated local subprocess** — uploaded voice notes
  are transcribed by `bridge/transcribe_audio.py`, a standalone script that never
  imports Odoo, runs under its own dedicated Python venv
  (`aidoo.whisper_python`) as the Odoo service user, and loads a **local**
  faster-whisper model from disk (`aidoo.whisper_model_dir`) — no audio or
  transcript ever leaves the server. Its input is a file that already passed the
  upload validation (extension allow-list + size cap) and lives in the
  session-scoped uploads directory; its only output is a `.transcript.txt`
  written next to the audio in that same directory (the path-scoped Read guard
  applies unchanged). The subprocess runs with a hard timeout
  (`aidoo.whisper_timeout_s`, default 300 s) and any failure degrades to an
  explanatory transcript note — it can neither block nor crash the turn, and a
  failed transcription caches nothing.
- **Per-user credential isolation** — OAuth credentials and per-user API keys
  (e.g. a user's Z.AI key under `.zai/api_key`) are stored per user with mode
  `0600` and never exposed on a record. The optional *shared* fallback keys
  (`aidoo.<provider>.api_key`, including `aidoo.zai.api_key`) live in
  `ir.config_parameter` and are readable by system-settings administrators —
  prefer per-user keys, and treat a shared key as an instance-wide secret.
- **Native (in-process) providers keep the same isolation** — Codex, Gemini and
  Z.AI talk to the provider backend directly from the Python worker instead of
  spawning a CLI binary. This removes the subprocess/CLI attack surface, but the
  credential model is unchanged: tokens are read from the acting/credential
  user's own per-user config dir (`$CODEX_HOME/auth.json` etc., `0600`), token
  refresh writes back to that same file, and tools still run in-process through
  `aidoo.tool.exec` as the invoking user (never superuser). No credential is
  placed on a record or shared across users.
- **Antigravity is experimental / opt-in** — the Antigravity (`agy`) provider
  ships built-in web tools (read_url_content / search_web / browser) that its
  permission engine **cannot deny** — so it has ungated internet access, a
  data-exfiltration vector under the untrusted-LLM threat model. It is hidden
  from the picker unless `aidoo.antigravity.enabled` is set, and must not be
  enabled for zero-trust deployments. Its shell + file-write tools *are* denied.
- **Background & bot runs act as a real user** — scheduled agents
  (`aidoo.scheduled`) and the Discuss bot (`models/discuss_channel.py`) do not add
  a new execution path: each goes through the same `aidoo.session.send_message` /
  runner as the chat, executing as the agent's configured user or the messaging
  user respectively (never superuser). Tool gating, per-user credentials, record
  rules and the audit log all apply unchanged. The Discuss bot only answers
  internal users in the *AI Assistant User* group and is loop-guarded against its
  own messages. A scheduled agent may optionally carry a bounded, admin-configured
  slice of its **own** prior-run conversation into the next run (`context_mode` /
  `context_amount`); this is the agent's existing session content fed back as a
  non-persisted prompt prefix (never stored, so it cannot compound), stays within
  the same session and run-as user, and crosses no tenant, user or session
  boundary — it is the same data the run-as user could already see in that chat.
- **View context is access-checked** — the "Ask Aidoo" actions and the floating
  companion seed a conversation with the record or list the user is viewing.
  `/aidoo/new` validates the user's read access to that model/record before
  storing it (`context_model` / `context_res_id` / `context_domain`), and the
  first-turn focus note is resolved under the user's own rights — a record or
  model the user cannot read is silently dropped, so context seeding never
  discloses data the user could not already see.
- **Generated HTML artifacts are served sandboxed** — assistant-generated HTML
  is persisted as `ir.attachment` and served only through
  `GET /aidoo/artifact/<id>`, which refuses anything that is not a
  session-linked attachment carrying the artifact marker (it is not a generic
  attachment reader, even with a valid token). Responses are hardened against
  the untrusted-LLM threat model: `Content-Security-Policy: sandbox;
  default-src 'none'; style-src 'unsafe-inline'; img-src data:; font-src data:`
  gives the document an opaque origin with **no script execution and no network
  subresources** (no exfiltration channel, no cookie-bearing same-origin
  requests), plus `X-Content-Type-Options: nosniff`. Access is (a) the session
  owner, (b) an internal member of the session's Discuss channel, or (c) a
  bearer of the owner-minted share token (core `ir.attachment` access token,
  `consteq`-checked; **anyone with the link can view until the owner revokes
  it** — share/revoke routes are owner-only).
- **Conversation management is owner-scoped** — the favorite (`/aidoo/favorite`),
  discard (`/aidoo/discard`), delete (`/aidoo/delete`), messages (`/aidoo/messages`)
  and listing routes resolve the session through the same ownership check as the
  rest of the chat API (`_session()` rejects a session the acting user does not own,
  on top of the `aidoo.session` record rule). Deletion additionally refuses while a
  run is in flight and only unlinks the caller's own session (its messages cascade);
  it cannot remove another user's conversation. History paging does not widen this: the
  `/aidoo/messages` `limit`/`before_id` arguments only page within the *already
  authorized* session's own messages — `before_id` is cast to `int` and used as a
  `<` cursor in a domain scoped to that session — so it discloses nothing the
  caller could not already read, and is purely a performance/UX bound. The
  empty-conversation cleanup (`aidoo.session._prune_empty()`) only ever unlinks the
  acting user's own message-less, non-running sessions and explicitly skips
  Discuss- and scheduled-agent-linked sessions; `/aidoo/discard` is additionally
  guarded server-side to delete only when the session has no messages, so a real
  conversation — or another user's — can never be removed through these endpoints.
- **AI skills are instructions, not privileges** — a skill (`aidoo.skill`) is
  user-authored prompt text injected into the per-turn identity block. It can shape
  *how* the assistant answers but cannot widen *what* it may do: the tool grant,
  model denylist, read-only SQL and `su=False` execution in `aidoo.tool.exec` are
  unchanged and authoritative, so a skill can never grant a tool or exceed the
  acting user's ACLs. Skills are owner-scoped — record rules let a user read their
  own plus any `is_shared` skill but write only their own, and `is_shared`
  (publishing to all users) is gated to *AI Assistant Managers* both by rule and by
  a guard on `create`/`write`. The `/aidoo/new` and `/aidoo/skill` routes attach a
  skill only after re-resolving it through the caller's own read rights (a private
  skill of another user is simply not found), and a scheduled agent's skill is
  applied as the agent's own run-as user.
- **Personas own an account, not privileges** — a persona (`aidoo.persona`) carries
  its own AI *provider* credentials but never its own *data* rights. It splits the
  session into a **credential user** (a dedicated, login-less **portal** service
  `res.users` — `share=True`, only `base.group_portal`, so it holds no internal
  privileges and consumes no internal seat; it only carries the provider account on
  disk and the Discuss identity) and the
  **acting user** (the human who @mentioned it): `aidoo.session._credential_user()`
  chooses whose credentials load, while the background worker always runs with
  `ctx["uid"]` set to the invoking user, so every tool goes through `aidoo.tool.exec`
  as that user (`su=False`) under their ACLs, record rules, tool grant, model
  denylist and audit log. A persona therefore cannot escalate: a low-privilege user
  mentioning a powerful persona still reaches only their own data, and the persona's
  `instructions`/`skill_ids` are prompt text (like skills) that cannot grant a tool.
  The persona's service user never logs in and is not used as an acting identity.
  Creating/editing personas — and connecting their provider accounts — is gated to
  *AI Assistant Managers* (ir.model.access + record rules); other AI users may only
  read active personas to resolve a mention. Persona discoverability in Discuss
  (the "New message"/"Invite people" partner search, via the
  `res.partner._search_for_channel_invite` override) is likewise gated to the
  *AI Assistant User* group — non-AI users are never offered a persona to DM or
  invite, and core's own search domain already excludes the portal service users.
  Credentials are written to the service
  user's per-user, per-provider config dir exactly like a human's (0700/0600), never
  stored in the database; the transient connect-form inputs (API key / paste creds /
  login code) are cleared immediately after use. Inbound channel messages are
  flattened to their visible text before reaching the model — anchor tags (including
  @mention links, whose href is a record URL such as `/odoo/res.partner/1034`) are
  reduced to their label so a link target is never fed to the model as an implicit
  instruction.
- **Sandboxed report/chart rendering** — HTML the model emits (charts, reports,
  and `aidoo.session._post_report` output) is rendered in an
  `<iframe sandbox="allow-same-origin">` **without** `allow-scripts`, so its own
  scripts never execute and its styles are isolated from the Odoo UI; the parent
  only reads the frame to size it. `allow-scripts` is deliberately never combined
  with `allow-same-origin` (that pairing lets a frame drop its own sandbox).
- **Discuss replies are escape-first** — a persona's answer (model-authored
  Markdown) is converted to HTML by `aidoo.session.aidoo_md_to_html` before it is
  posted to the channel. The whole reply is HTML-escaped first and only a fixed set
  of formatting tags (`p`, `br`, `strong`, `em`, `code`, `pre`, `h3`–`h6`, `ul`/`ol`/
  `li`, `table`/`thead`/`tbody`/`tr`/`th`/`td`, and `http(s)` `a`) is re-introduced —
  cell and heading text is itself escaped — so nothing the model emits — including a
  literal `<script>` or an inline event handler — can inject markup into Discuss; it
  is rendered as text. Odoo's own `message_post` sanitizer applies on top.

If you find a way to (a) escalate beyond the acting user's ACLs, (b) mutate data
through `sql_select`, (c) forge or replay a bridge token, (d) make the model
reach a denied built-in, or (e) execute script or escape the iframe sandbox via
rendered model HTML, that is a security bug — please report it.
