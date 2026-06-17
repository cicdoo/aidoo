<div align="center">

<img src="https://cicdoo.com/assets/img/cicdoo_colored.png" alt="CICDoo" width="120" />

# Claudoo

### Chat with your Odoo — safely.

[![Deploy on CICDoo](https://img.shields.io/badge/🚀%20Deploy%20on%20CICDoo-1a73e8?style=for-the-badge)](https://www.cicdoo.com)
[![Get Support](https://img.shields.io/badge/🛟%20Get%20Support-0f9d58?style=for-the-badge)](https://www.cicdoo.com)
[![Sponsor](https://img.shields.io/badge/♥%20Sponsor-EA4AAA?style=for-the-badge)](https://github.com/sponsors/cicdoo)

**An in-Odoo AI assistant powered by the Claude Code CLI. It answers questions,
queries your data, and builds reports — running as the logged-in user, never as
superuser, with a full audit trail.**

[![License: LGPL-3.0](https://img.shields.io/badge/License-LGPL%20v3-blue.svg)](LICENSE)
[![Odoo](https://img.shields.io/badge/Odoo-18.0-714B67.svg)](https://www.odoo.com)
[![PR checks](https://github.com/cicdoo/claudoo/actions/workflows/pr-checks.yml/badge.svg)](https://github.com/cicdoo/claudoo/actions/workflows/pr-checks.yml)
[![PRs Welcome](https://img.shields.io/badge/PRs-welcome-brightgreen.svg)](CONTRIBUTING.md)

[Quick start](#-quick-start) · [Security model](#-security-model) ·
[Configuration](#-configuration) · [Commercial support](#-commercial-support) ·
[Roadmap](#-roadmap)

</div>

---

## Why Claudoo?

Most "AI for Odoo" add-ons either ship your data to a third-party prompt or hand
the model a superuser connection and hope for the best. **Claudoo is built the
other way around — security first:**

| | Claudoo |
|---|---|
| 🔒 **Runs as the user** | Every ORM action executes with `su=False`, so the user's `ir.model.access` rights and record rules are enforced automatically. The model can never see or do more than the person using it. |
| 🧮 **Read-only SQL, really** | `sql_select` is double-guarded: a SELECT-only text validator (no stacked statements, row locks, or dangerous functions) **and** Postgres `SET TRANSACTION READ ONLY` + a statement timeout. Gated to a dedicated *AI SQL Analyst* group. |
| 🧾 **Full audit log** | Every tool call — model, method, SQL, row counts, outcome, even denials — is written to an immutable log on a separate committed cursor, so it survives rollbacks. |
| 🔑 **Per-user OAuth, no shared key** | Each user clicks **Login with Claude** and authorizes with their own subscription. Credentials are stored per user, mode `0600`, never in the database, never echoed back. |
| 🚫 **No shell, no web, no surprises** | Claude's built-in tools (Bash, file write, web, agent spawners) are hard-denied. The model can act **only** through a small set of Odoo tools over a loopback bridge that holds *no database credentials*. |
| ⚡ **Streaming UX** | Replies and tool "chips" stream into the chat in real time over the Odoo bus. |

If you sell or run Odoo for clients, the security posture **is** the feature: you
can give business users a natural-language window into their ERP without widening
anyone's access by a single record.

---

## 🏗 How it works

```
OWL chat UI ──/claudoo/send (auth=user)──► Odoo worker ──► daemon thread
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
- The **[Claude Code CLI](https://docs.claude.com/en/docs/claude-code)** installed
  on the Odoo server (Claudoo auto-detects the binary; the path is configurable).
- Python **`requests`** (declared as an external dependency).
- A **Claude subscription** per user (OAuth login) — no API key required.

### Install

```bash
# Put the module on your addons_path (the repo is the module folder),
# then install it:
odoo-bin -c odoo.conf -d <your-db> -i claudoo --stop-after-init
# Restart your live server afterwards (Odoo does not re-import changed Python).
```

### First run

1. Open the **Claudoo** app from the top menu.
2. Click **Login with Claude** and authorize with your subscription.
3. Ask away: *"How many sale orders are still in draft this month?"*

Grant the **AI SQL Analyst** group to users who should be able to run read-only
SQL reports, and **AI Assistant Manager** to those who configure it and read the
audit log.

---

## 🔐 Security model

Claudoo treats LLM output as **untrusted** and defends accordingly:

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

**Settings → Claudoo** (or System Parameters `claudoo.*`):

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

## 🧪 Tests

```bash
odoo-bin -c odoo.conf -d <db> -i claudoo --test-enable --test-tags claudoo --stop-after-init
```

Covers the SQL guard, bridge-token mint/verify, the tool-access policy, the
`ai_bridge` auth method, and the audit log. CI runs them on every push and PR.

---

## 💼 Commercial support

Claudoo is **free and open source (LGPL-3.0)** and always will be. It is built and
maintained by **[CICDoo](https://cicdoo.com)**, who also offer:

- 🚀 **Managed deployment & hosting** — Claudoo running securely on your Odoo, you manage your business and we manage your infrastructure
- 🛟 **Priority support & SLAs** for production rollouts.
- 🧩 **Custom tools & integrations** — extend the safe tool surface to your models
  and workflows.
- 📜 **Commercial license** for organizations that cannot adopt LGPL terms —
  modify Claudoo and keep your changes private, embed it in a closed product, or
  get warranty/indemnification. See [COMMERCIAL_LICENSE.md](COMMERCIAL_LICENSE.md).
- 🎓 **Training & security review** for teams enabling AI access to their ERP.

👉 **Get in touch: [sam@cicdoo.com](mailto:sam@cicdoo.com) · [cicdoo.com](https://cicdoo.com)**

If Claudoo saves your team time, consider [sponsoring](https://github.com/sponsors/cicdoo)
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

Claudoo wraps Anthropic's **[Claude Code CLI](https://docs.claude.com/en/docs/claude-code)**.
"Claude" and "Anthropic" are trademarks of Anthropic. Claudoo is an independent,
community project and is not affiliated with or endorsed by Anthropic.

## 📄 License

Claudoo is **dual-licensed** — choose whichever fits you:

- 🆓 **Open source:** [GNU LGPL-3.0-or-later](LICENSE). Free for any use, including
  on both Odoo Community and Enterprise; modifications to Claudoo's own files stay
  under the LGPL.
- 💼 **Commercial:** a proprietary-friendly license from CICDoo that lifts the LGPL
  obligations (keep modifications private, embed in closed products, warranty/SLA).
  See [COMMERCIAL_LICENSE.md](COMMERCIAL_LICENSE.md) · **license@cicdoo.com**.

`SPDX-License-Identifier: LGPL-3.0-or-later OR LicenseRef-Claudoo-Commercial`

© 2026 [CICDoo](https://cicdoo.com)
