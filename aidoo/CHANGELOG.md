# Changelog

## Unreleased
- **MCP OAuth consent `access_denied` fix** — the consent page no longer uses the login form class (whose script could drop the clicked button's value) and posts an explicit hidden `decision`; a missing decision re-renders the page instead of returning `access_denied`, and consent decisions are logged.
- **MCP OAuth for ChatGPT / Claude connectors** — `/aidoo/mcp` now answers unauthenticated requests with a `WWW-Authenticate: Bearer resource_metadata=...` challenge and Aidoo serves an OAuth 2.1 authorization server (RFC 9728/8414 metadata, RFC 7591 dynamic client registration, authorization code + PKCE `S256`, rotating refresh tokens, consent page). Access tokens are Odoo API keys of scope `aidoo_mcp` (revocable under Preferences → Account Security); manual API keys keep working. OAuth follows `aidoo.mcp_enabled`.
- **Remote MCP server** — `POST /aidoo/mcp` (Streamable HTTP, JSON-only) lets external MCP clients (Claude Desktop, Cursor, Claude Code `--transport http`) use the assistant's tools. Enabled by default; the Settings → Aidoo → Remote MCP Server checkbox (`aidoo.mcp_enabled`) disables it (disabled returns 403 with a hint; `GET` returns 405 with a JSON description). Authenticates with a user's own API key of scope `aidoo_mcp`; calls run through the shared `aidoo.tool.exec` pipeline (per-user tool grant, model denylist, non-superuser, `aidoo.tool_log` audit with empty session). Tool definitions are shared in `models/mcp_tools.py`; the stdio bridge is unchanged.

## 20.0.1.0.0
- Frontend ported to Owl 3: `proxy`/`signal.ref()` replace `reactive`/`useState`/`useRef`, `useProps` replaces `static props`, templates use `this.` members, and `t-model` is replaced by `t-att-value` + `t-on-input`. Fixes `reactive is not a function` at web client start.
- Port to Odoo 20.0: `res.groups.privilege` replaces the module category, `group_ids` replaces `groups_id`, `models.Constraint` replaces `_sql_constraints`, mail hook signatures (`_message_post_after_hook(message)`, `_notify_get_recipients(message, **kwargs)`), `type="jsonrpc"` routes, `Domain` replaces `odoo.osv.expression`.
- `ir.access` (`security/ir.access.csv`) replaces `ir.model.access` and `ir.rule`; the record rules were folded into per-group accesses with domains.
- `ir.config_parameter`: `get_param`/`set_param` replaced by `get_str`/`set_str`.
- Discuss: persona search hooks `res.partner._search_for_channel_invite` (`im_search` is gone); `discuss.channel._get_or_create_chat` and `_add_members(partners=...)` replace `channel_get` / `add_members`.
- `_auth_method_ai_bridge(cls, routing)` takes the routing dict.
- Artifact route: share-token check via `_can_return_content`, `att.raw.content` for the lazy binary value.
