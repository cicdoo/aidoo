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
- **View context is access-checked** — the "Ask Aidoo" actions and the floating
  companion seed a conversation with the record or list the user is viewing.
  `/aidoo/new` validates the user's read access to that model/record before
  storing it (`context_model` / `context_res_id` / `context_domain`), and the
  first-turn focus note is resolved under the user's own rights — a record or
  model the user cannot read is silently dropped, so context seeding never
  discloses data the user could not already see.
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
