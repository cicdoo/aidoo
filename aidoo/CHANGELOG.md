# Changelog

## 20.0.1.0.0
- Port to Odoo 20.0: `res.groups.privilege` replaces the module category, `group_ids` replaces `groups_id`, `models.Constraint` replaces `_sql_constraints`, mail hook signatures (`_message_post_after_hook(message)`, `_notify_get_recipients(message, **kwargs)`), `type="jsonrpc"` routes, `Domain` replaces `odoo.osv.expression`.
- `ir.access` (`security/ir.access.csv`) replaces `ir.model.access` and `ir.rule`; the record rules were folded into per-group accesses with domains.
- `ir.config_parameter`: `get_param`/`set_param` replaced by `get_str`/`set_str`.
- Discuss: persona search hooks `res.partner._search_for_channel_invite` (`im_search` is gone); `discuss.channel._get_or_create_chat` and `_add_members(partners=...)` replace `channel_get` / `add_members`.
- `_auth_method_ai_bridge(cls, routing)` takes the routing dict.
- Artifact route: share-token check via `_can_return_content`, `att.raw.content` for the lazy binary value.
