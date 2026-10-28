<div align="center">

<img src="https://cicdoo.com/assets/img/cicdoo_colored.png" alt="CICDoo" width="120" />

# Aidoo

### Chat with your Odoo — safely.

[![Deploy on CICDoo](https://img.shields.io/badge/🚀%20Deploy%20on%20CICDoo-1a73e8?style=for-the-badge)](https://www.cicdoo.com)
[![Get Support](https://img.shields.io/badge/🛟%20Get%20Support-0f9d58?style=for-the-badge)](https://www.cicdoo.com)
[![Sponsor](https://img.shields.io/badge/♥%20Sponsor-EA4AAA?style=for-the-badge)](https://github.com/sponsors/cicdoo)

**An in-Odoo AI assistant with a choice of engine per conversation — Claude,
Codex (OpenAI), Gemini (Google) or Grok (xAI). It answers questions, queries your
data, and builds reports — running as the logged-in user, never as superuser,
with a full audit trail.**

[![License: LGPL-3.0](https://img.shields.io/badge/License-LGPL%20v3-blue.svg)](LICENSE)
[![Odoo](https://img.shields.io/badge/Odoo-18.0-714B67.svg)](https://www.odoo.com)
[![PR checks](https://github.com/cicdoo/aidoo/actions/workflows/pr-checks.yml/badge.svg)](https://github.com/cicdoo/aidoo/actions/workflows/pr-checks.yml)
[![PRs Welcome](https://img.shields.io/badge/PRs-welcome-brightgreen.svg)](CONTRIBUTING.md)

[Quick start](#-quick-start) · [Security model](#-security-model) ·
[Configuration](#-configuration) · [Commercial support](#-commercial-support) ·
[Roadmap](#-roadmap)

</div>

---

## Why Aidoo?

Most "AI for Odoo" add-ons either ship your data to a third-party prompt or hand
the model a superuser connection and hope for the best. **Aidoo is built the
other way around — security first:**

| | Aidoo |
|---|---|
| 🔒 **Runs as the user** | Every ORM action executes with `su=False`, so the user's `ir.model.access` rights and record rules are enforced automatically. The model can never see or do more than the person using it. |
| 🧮 **Read-only SQL, really** | `sql_select` is double-guarded: a SELECT-only text validator (no stacked statements, row locks, or dangerous functions) **and** Postgres `SET TRANSACTION READ ONLY` + a statement timeout. Gated to a dedicated *AI SQL Analyst* group. |
| 🧾 **Full audit log** | Every tool call — model, method, SQL, row counts, outcome, even denials — is written to an immutable log on a separate committed cursor, so it survives rollbacks. |
| 🔌 **Pick your engine per chat** | Choose **Claude**, **Codex**, **Gemini** or **Grok** when you start a conversation. One UI, one safety model; each engine runs behind its own `aidoo.provider.<code>` adapter. |
| 🔑 **Per-user, per-provider auth, no shared key** | Each user connects their own account from the chat — Claude OAuth, Codex device-code, Gemini/Grok credential-import or API key. Credentials are stored per user and per provider, mode `0600`, never in the database, never echoed back. |
| 🚫 **No shell, no web, no surprises** | Claude's built-in tools (Bash, file write, web, agent spawners) are hard-denied. The model can act **only** through a small set of Odoo tools over a loopback bridge that holds *no database credentials*. |
| ⚡ **Streaming UX** | Replies and tool "chips" stream into the chat in real time over the Odoo bus. |

If you sell or run Odoo for clients, the security posture **is** the feature: you
can give business users a natural-language window into their ERP without widening
anyone's access by a single record.

---

## 🏗 How it works

```
OWL chat UI ──/aidoo/send (auth=user)──► Odoo worker ──► daemon thread
   ▲ bus stream (deltas, tool chips)                          │ spawns
   │                          claude -p --output-format stream-json (OAuth)
   │                          tool surface = mcp__odoo__* ONLY
   │                                       │ stdio MCP
   │                                       ▼
   │               bridge/mcp_server.py  (no DB creds, loopback only)
   │                                       │ Bearer(session token)
   └──── bus.bus ◄── controllers/tools.py (auth='ai_bridge', runs AS the user) ──► Postgres
```

- **Only Odoo touches the database.** The CLI subprocess and the MCP forwarder
  hold no credentials — only a short-lived, session-scoped HMAC bearer token.
- The bridge is reachable on **loopback only**; the auth method rejects any
  non-local caller and rebinds the request to the session's user (`su=False`).
- Built-in tools are denied through a generated `settings.json` deny-list plus a
  `PreToolUse` hook, so the model is confined to `mcp__odoo__*`.

---

## 🚀 Quick start

### Requirements

- **Odoo 18.0**
- For **Claude**, the Claude Code CLI (`claude`) installed on the Odoo server
  (auto-detected; the path is configurable). **Codex**, **Gemini**, **Grok**
  and **Z.AI** are native in-process providers and need no binary.
- Python **`requests`** (declared as an external dependency).
- A per-user account for whichever provider(s) you use — connected from the chat
  (Claude OAuth, Codex device-code, Gemini/Grok credential-import or API key).

### Install

```bash
# Put the module on your addons_path (the repo is the module folder),
# then install it:
odoo-bin -c odoo.conf -d <your-db> -i aidoo --stop-after-init
# Restart your live server afterwards (Odoo does not re-import changed Python).
```

### First run

1. Open the **Aidoo** app from the top menu.
2. Pick a provider for the conversation, then connect your account when prompted
   (e.g. **Login with Claude**, or import Gemini/Grok credentials).
3. Ask away: *"How many sale orders are still in draft this month?"*

Grant the **AI SQL Analyst** group to users who should be able to run read-only
SQL reports, and **AI Assistant Manager** to those who configure it and read the
audit log.

---

## 🔐 Security model

Aidoo treats LLM output as **untrusted** and defends accordingly:

- **Never superuser.** Tool endpoints abort if `request.env.su` is true.
- **ACLs + record rules** apply to every read and write, because the call runs as
  the user.
- **Writes are opt-in.** Read tools are the default; each write tool
  (`orm_create/write/unlink`, `orm_action`, `run_wizard`, `run_server_action`)
  must be granted per user, and **zero-trust mode** strips them entirely.
- **SQL can only read.** See the guard above.
- **Capability tokens** expire, are session-scoped, single-jti, and HMAC-signed
  with the database secret.

See [SECURITY.md](SECURITY.md) for the full threat model and how to report a
vulnerability.

---

## ⚙️ Configuration

**Settings → Aidoo** (or System Parameters `aidoo.*`):

| Parameter | Purpose |
|---|---|
| `cli_path` / `cli_glob` | Path (or glob) to the Claude Code binary (auto-detected if blank) |
| `model` | Claude model id (default `claude-sonnet-4-5`) |
| `max_turns`, `timeout_s` | Per-turn limits |
| `home_root`, `scratch_root` | Per-user OAuth home and CLI scratch directories |
| `base_url` | Loopback URL the bridge calls back on |
| `sql_enabled` | Master switch for `sql_select` |
| `zero_trust_default` | Global default for read-only (zero-trust) mode |
| `python_bin` | Interpreter used to run the bundled bridge scripts (defaults to Odoo's) |

### Permissions / groups

- **AI Assistant User** — can use the chat (ORM reads).
- **AI SQL Analyst** — additionally may run `sql_select`.
- **AI Assistant Manager** — configuration + audit log; sees all sessions.

---

## 🔌 Remote MCP server

Aidoo can also act as an MCP server for external clients. Enable it in
**Settings → Aidoo → Remote MCP Server** (`aidoo.mcp_enabled`, on by default; untick to disable),
each user creates an API key under *Preferences → Account Security → New
API Key* with scope `aidoo_mcp`. The endpoint is `POST /aidoo/mcp` (Streamable
HTTP, JSON responses). Tools are exactly the user's granted tools and every call
is audited in the tool log.

```
curl -H "Authorization: Bearer <key>" -d '{"jsonrpc":"2.0","id":1,"method":"ping"}' https://HOST/aidoo/mcp
```

```json
{"mcpServers":{"odoo":{"type":"http","url":"https://HOST/aidoo/mcp","headers":{"Authorization":"Bearer <key>"}}}}
```

### Connect ChatGPT / Claude

Add `https://<host>/aidoo/mcp` as a custom connector. The client discovers
OAuth on its own (the endpoint answers `401` with a `WWW-Authenticate:
Bearer resource_metadata=...` challenge), registers itself, and sends you to
Odoo: sign in, review the consent page and approve. Users need the *AI
Assistant User* group. API keys remain an alternative (above).

| Endpoint | Purpose |
|---|---|
| `/.well-known/oauth-protected-resource` (also `.../aidoo/mcp`) | RFC 9728 resource metadata |
| `/.well-known/oauth-authorization-server` | RFC 8414 server metadata |
| `POST /aidoo/oauth/register` | RFC 7591 dynamic client registration |
| `/aidoo/oauth/authorize` | authorization code + PKCE (`S256` only), consent page |
| `POST /aidoo/oauth/token` | code exchange and refresh-token rotation |

Access tokens are ordinary Odoo API keys (scope `aidoo_mcp`, 1 hour); refresh
tokens last 30 days and are rotated on each use (replaying an old one revokes
the grant). Revoke access any time under *Preferences → Account Security*
(delete the "Aidoo OAuth: ..." key). The OAuth routes follow the same
`aidoo.mcp_enabled` switch and return `403` when MCP is disabled.

```
curl -s https://HOST/.well-known/oauth-protected-resource
curl -s https://HOST/.well-known/oauth-authorization-server
curl -si -X POST https://HOST/aidoo/mcp   # 401 + resource_metadata
```

---

## 🧪 Tests

```bash
odoo-bin -c odoo.conf -d <db> -i aidoo --test-enable --test-tags aidoo --stop-after-init
```

Covers the SQL guard, bridge-token mint/verify, the tool-access policy, the
`ai_bridge` auth method, and the audit log. CI runs them on every push and PR.

---

## 💼 Commercial support

Aidoo is **free and open source (LGPL-3.0)** and always will be. It is built and
maintained by **[CICDoo](https://cicdoo.com)**, who also offer:

- 🚀 **Managed deployment & hosting** — Aidoo running securely on your Odoo, you manage your business and we manage your infrastructure
- 🛟 **Priority support & SLAs** for production rollouts.
- 🧩 **Custom tools & integrations** — extend the safe tool surface to your models
  and workflows.
- 📜 **Commercial license** for organizations that cannot adopt LGPL terms —
  modify Aidoo and keep your changes private, embed it in a closed product, or
  get warranty/indemnification. See [COMMERCIAL_LICENSE.md](COMMERCIAL_LICENSE.md).
- 🎓 **Training & security review** for teams enabling AI access to their ERP.

👉 **Get in touch: [sam@cicdoo.com](mailto:sam@cicdoo.com) · [cicdoo.com](https://cicdoo.com)**

If Aidoo saves your team time, consider [sponsoring](https://github.com/sponsors/cicdoo)
its development.

---

## 🗺 Roadmap

- Approval-gated `orm_create`/`orm_write`/`orm_unlink` with before→after diff cards
- An admin allow/deny **policy model** for tools and models
- A dedicated `ai_sql_ro` Postgres role + view scoping
- OS-level sandboxing of the CLI subprocess
- Per-session rate and tool budgets

Have an idea? Open a [feature request](.github/ISSUE_TEMPLATE/feature_request.yml)
or a PR — see [CONTRIBUTING.md](CONTRIBUTING.md).

---

## 🙌 Acknowledgements

Aidoo drives each vendor's own command-line tool: Anthropic's
**[Claude Code CLI](https://docs.claude.com/en/docs/claude-code)**, OpenAI's
**Codex CLI**, Google's **Gemini CLI** and xAI's **Grok CLI**. "Claude" and
"Anthropic", "OpenAI" and "Codex", "Gemini" and "Google", "Grok" and "xAI" are
trademarks of their respective owners. Aidoo is an independent, community project
and is not affiliated with or endorsed by any of them.

## 📄 License

Aidoo is **dual-licensed** — choose whichever fits you:

- 🆓 **Open source:** [GNU LGPL-3.0-or-later](LICENSE). Free for any use, including
  on both Odoo Community and Enterprise; modifications to Aidoo's own files stay
  under the LGPL.
- 💼 **Commercial:** a proprietary-friendly license from CICDoo that lifts the LGPL
  obligations (keep modifications private, embed in closed products, warranty/SLA).
  See [COMMERCIAL_LICENSE.md](COMMERCIAL_LICENSE.md) · **license@cicdoo.com**.

`SPDX-License-Identifier: LGPL-3.0-or-later OR LicenseRef-Aidoo-Commercial`

© 2026 [CICDoo](https://cicdoo.com)
