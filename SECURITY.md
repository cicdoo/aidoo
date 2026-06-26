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
  `SET TRANSACTION READ ONLY` with a statement timeout.
- **Capability tokens** — the MCP bridge holds only a short-lived, session-scoped
  HMAC bearer token (no DB credentials) and is reachable on loopback only.
- **Denied built-ins** — the model cannot reach Bash/Read/Write/Web; it acts only
  through the `mcp__odoo__*` tools.
- **Per-user credential isolation** — OAuth credentials and per-user API keys
  (e.g. a user's Z.AI key under `.zai/api_key`) are stored per user with mode
  `0600` and never exposed on a record. The optional *shared* fallback keys
  (`aidoo.<provider>.api_key`, including `aidoo.zai.api_key`) live in
  `ir.config_parameter` and are readable by system-settings administrators —
  prefer per-user keys, and treat a shared key as an instance-wide secret.
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
- **Conversation management is owner-scoped** — the favorite (`/aidoo/favorite`),
  discard (`/aidoo/discard`) and listing routes resolve the session through the
  same ownership check as the rest of the chat API (`_session()` rejects a session
  the acting user does not own, on top of the `aidoo.session` record rule). The
  empty-conversation cleanup (`aidoo.session._prune_empty()`) only ever unlinks the
  acting user's own message-less, non-running sessions and explicitly skips
  Discuss- and scheduled-agent-linked sessions; `/aidoo/discard` is additionally
  guarded server-side to delete only when the session has no messages, so a real
  conversation — or another user's — can never be removed through these endpoints.
- **Sandboxed report/chart rendering** — HTML the model emits (charts, reports,
  and `aidoo.session._post_report` output) is rendered in an
  `<iframe sandbox="allow-same-origin">` **without** `allow-scripts`, so its own
  scripts never execute and its styles are isolated from the Odoo UI; the parent
  only reads the frame to size it. `allow-scripts` is deliberately never combined
  with `allow-same-origin` (that pairing lets a frame drop its own sandbox).

If you find a way to (a) escalate beyond the acting user's ACLs, (b) mutate data
through `sql_select`, (c) forge or replay a bridge token, (d) make the model
reach a denied built-in, or (e) execute script or escape the iframe sandbox via
rendered model HTML, that is a security bug — please report it.
