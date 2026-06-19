# Security Policy

Aidoo bridges a large-language-model agent to your Odoo database, so we take
security seriously and welcome responsible disclosure.

## Supported versions

| Version  | Supported |
|----------|-----------|
| 18.0.x   | ✅        |

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

- **No superuser execution** — tool endpoints abort if `request.env.su` is true;
  every ORM call runs as the acting user with `ir.model.access` + record rules.
- **Read-only SQL** — `sql_select` is validated (SELECT/WITH only, no stacked
  statements, no row locks, no dangerous functions) *and* executed under
  `SET TRANSACTION READ ONLY` with a statement timeout.
- **Capability tokens** — the MCP bridge holds only a short-lived, session-scoped
  HMAC bearer token (no DB credentials) and is reachable on loopback only.
- **Denied built-ins** — the model cannot reach Bash/Read/Write/Web; it acts only
  through the `mcp__odoo__*` tools.
- **Per-user credential isolation** — OAuth credentials are stored per user with
  mode `0600` and never exposed on a record.
- **Background & bot runs act as a real user** — scheduled agents
  (`aidoo.scheduled`) and the Discuss bot (`models/discuss_channel.py`) do not add
  a new execution path: each goes through the same `aidoo.session.send_message` /
  runner as the chat, executing as the agent's configured user or the messaging
  user respectively (never superuser). Tool gating, per-user credentials, record
  rules and the audit log all apply unchanged. The Discuss bot only answers
  internal users in the *AI Assistant User* group and is loop-guarded against its
  own messages.
- **Per-provider tool gating — and the Antigravity exception.** Every provider
  confines the model to the gated `mcp__odoo__*` tools by disabling the CLI's own
  built-ins (Claude/Gemini/Codex/Grok deny shell/write/web through their CLI tool
  config). The **experimental Antigravity (`agy`) provider is the one exception**:
  `agy`'s permission engine only recognises the actions `command`, `write_file` and
  `mcp`, so the adapter denies the built-in **shell** (`command(*)`) and
  **file-write** (`write_file(*)`) via the per-user
  `~/.gemini/antigravity-cli/settings.json`, but `agy` exposes **no permission
  action for its web tools** (`read_url_content` / `search_web` / `browser`), which
  therefore **cannot be disabled**. An Antigravity conversation thus has **ungated
  outbound internet access** — a data-exfiltration vector under the untrusted-LLM
  threat model (the model could read data via MCP and post it to an attacker URL).
  For this reason Antigravity is **off by default and opt-in**
  (`aidoo.antigravity.enabled`, hidden from the picker until an admin enables it)
  and **must not be enabled for zero-trust / untrusted-data deployments** until
  `agy` gains a way to deny its web tools. (`agy` also exits `0` even on hard
  errors, writing them only to its log; the adapter surfaces the logged error so a
  failure is not silently reported as success.)
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
