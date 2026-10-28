# -*- coding: utf-8 -*-
# Copyright 2026 CICDoo (https://cicdoo.com)
# SPDX-License-Identifier: LGPL-3.0-or-later OR LicenseRef-Aidoo-Commercial
# Dual-licensed: open source (LGPL-3, see LICENSE) or commercial (see COMMERCIAL_LICENSE.md).
"""Shared, transport-neutral tool executor.

The Odoo tool surface (orm_read/search/call, sql_select, the write/action tools)
is enforced and audited in exactly ONE place here, so every caller behaves
identically:

* the loopback HTTP bridge (``controllers/tools.py``) — used by the CLI-driven
  providers (Claude/Codex/Grok) over the stdio MCP server, and
* the native in-process providers (Gemini) — which call ``_dispatch`` directly
  from the run worker, with no subprocess and no loopback HTTP.

Invariants preserved for every caller:

* runs as the acting Odoo user, never superuser (``_guard``);
* per-user tool grant + global model denylist are authoritative (advisory
  allowlists elsewhere are not trusted);
* every call — success, denial, validation rejection, error — is written to the
  immutable ``aidoo.tool_log`` on a committed cursor via ``_run``/``_audit``.
"""
import fnmatch
import json
import logging
import re
import time

from odoo import models
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.osv import expression
from odoo.tools import str2bool

_logger = logging.getLogger(__name__)

# Hard caps on rows returned to the model (oversized results bloat the model's
# context and were a primary driver of out-of-memory crashes for CLI providers).
MAX_LIMIT = 50
SQL_MAX_ROWS = 1000

# Hard cap on the SIZE of a result, which a row cap does not bound: a single
# row can be arbitrarily large. A mail loop on cicdoo.mail produced individual
# mail.message bodies of 3.6 MB, and the inbox sweep reads message bodies, so
# one row was enough to blow past any sane context budget. Rows and bytes are
# independent limits and both are needed.
MAX_RESULT_BYTES = 256 * 1024

# Read-only model methods callable via orm_call.
SAFE_METHODS = {
    "name_search", "search_count", "read_group", "fields_get",
    "default_get", "search_read", "read", "get_views",
    "web_search_read", "web_read_group",
}

# --- SQL read-only validation -------------------------------------------------
_DENY_KEYWORDS = re.compile(
    r"\b(insert|update|delete|merge|truncate|drop|alter|create|grant|revoke|"
    r"comment|reindex|vacuum|analyze|cluster|copy|do|call|execute|prepare|"
    r"deallocate|listen|notify|set|reset|lock|refresh|import|into)\b",
    re.IGNORECASE)
_DENY_FUNCS = re.compile(
    r"\b(pg_read_file|pg_read_binary_file|pg_ls_dir|pg_stat_file|lo_import|"
    r"lo_export|lo_get|lo_put|dblink|dblink_exec|pg_sleep|pg_terminate_backend|"
    r"pg_cancel_backend|pg_reload_conf|set_config|nextval|setval|"
    r"pg_logical_emit_message)\b",
    re.IGNORECASE)
_FOR_LOCK = re.compile(r"\bfor\s+(update|share|no\s+key\s+update|key\s+share)\b",
                       re.IGNORECASE)


def _strip_sql_literals(sql):
    """Remove comments and string/dollar-quoted literals so keyword checks aren't
    fooled by their contents."""
    sql = re.sub(r"--[^\n]*", " ", sql)
    sql = re.sub(r"/\*.*?\*/", " ", sql, flags=re.DOTALL)
    sql = re.sub(r"\$(\w*)\$.*?\$\1\$", " ", sql, flags=re.DOTALL)  # dollar-quoted
    sql = re.sub(r"'(?:[^']|'')*'", " ", sql)  # single-quoted strings
    sql = re.sub(r'"(?:[^"]|"")*"', " ", sql)  # quoted identifiers
    return sql


def _validate_select(query):
    if not query or len(query) > 20000:
        raise ValidationError("Query missing or too long.")
    bare = _strip_sql_literals(query).strip().rstrip(";")
    if ";" in bare:
        raise ValidationError("Multiple statements are not allowed.")
    if not re.match(r"^\s*(select|with)\b", bare, re.IGNORECASE):
        raise ValidationError("Only SELECT/WITH queries are allowed.")
    if _DENY_KEYWORDS.search(bare):
        raise ValidationError("Query contains a disallowed (write) keyword.")
    if _DENY_FUNCS.search(bare):
        raise ValidationError("Query references a disallowed function.")
    if _FOR_LOCK.search(bare):
        raise ValidationError("Row-locking clauses are not allowed.")
    return True


# Clause keywords that end a FROM item list (so we stop collecting table names).
_SQL_FROM_STOP = {
    "where", "group", "having", "order", "limit", "offset", "union", "intersect",
    "except", "window", "fetch", "for", "on", "using", "join", "inner", "left",
    "right", "full", "cross", "natural", "lateral", "returning",
}
_SQL_IDENT_RE = re.compile(r'^("[^"]*"|[A-Za-z_][A-Za-z0-9_$]*)$')


def _sql_strip_comments_strings(sql):
    """Like _strip_sql_literals but KEEPS identifiers (quoted or not) so we can
    still see table names — used only by the table denylist."""
    sql = re.sub(r"--[^\n]*", " ", sql)
    sql = re.sub(r"/\*.*?\*/", " ", sql, flags=re.DOTALL)
    sql = re.sub(r"\$(\w*)\$.*?\$\1\$", " ", sql, flags=re.DOTALL)
    sql = re.sub(r"'(?:[^']|'')*'", " ", sql)  # string literals only
    return sql


def _sql_table_refs(query):
    """Best-effort set of real table names referenced after FROM/JOIN, lowercased
    with schema and quotes stripped. Only table-position identifiers are returned,
    so a column named like a denied table (e.g. account_id) is NOT flagged."""
    bare = _sql_strip_comments_strings(query)
    tokens = re.findall(r'"[^"]*"|[A-Za-z_][A-Za-z0-9_$]*|[(),.]', bare)

    def unq(t):
        return t[1:-1] if t.startswith('"') else t
    refs, i, n = set(), 0, len(tokens)
    while i < n:
        low = tokens[i].lower()
        if low in ("from", "join"):
            after_join = low == "join"
            i += 1
            expect = True
            while i < n:
                tok = tokens[i]
                tl = tok.lower()
                if expect:
                    if tl in ("only", "lateral"):
                        i += 1
                        continue
                    if tok == "(":          # subquery — its inner FROM is scanned later
                        break
                    if _SQL_IDENT_RE.match(tok):
                        name = unq(tok)
                        if (i + 2 < n and tokens[i + 1] == "."
                                and _SQL_IDENT_RE.match(tokens[i + 2])):
                            name = unq(tokens[i + 2])  # schema.table -> table
                            i += 2
                        refs.add(name.lower())
                        expect = False
                        i += 1
                        if after_join:       # a JOIN names exactly one table
                            break
                        continue
                    break
                else:
                    if tok == ",":
                        expect = True
                        i += 1
                        continue
                    if tl in _SQL_FROM_STOP or tok == ")":
                        break
                    i += 1                   # alias / AS / etc.
            continue
        i += 1
    return refs


class AidooToolExec(models.AbstractModel):
    _name = "aidoo.tool.exec"
    _description = "Aidoo Tool Executor (shared enforcement + audit)"

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------
    def _dispatch(self, session, tool, arguments, client_ip=None):
        """Run one tool call as the current (non-super) user, audited.

        Returns a JSON-safe result dict (the tool's payload, or ``{"error": ...}``
        for any denial/validation/error — never raises)."""
        self._guard()
        arguments = self._coerce_json_args(arguments)
        handler = getattr(self, "_t_%s" % tool, None)
        base_vals = dict(self._base_vals(tool, arguments), )
        if handler is None:
            # Audit the unknown-tool attempt the same way as a denial.
            return self._run(session, tool, lambda: self._unknown(tool),
                             base_vals, client_ip)
        args = arguments or {}
        return self._run(
            session, tool, lambda: handler(session, **args), base_vals, client_ip)

    @staticmethod
    def _unknown(tool):
        raise ValidationError("Unknown tool: %s" % tool)

    # Freeform array/object params that some models (Gemini flash) must send as
    # a JSON string (see gemini.py _sanitize_schema). Parse them back when a
    # string is received; real list/dict args (the MCP bridge path) pass through.
    _JSON_ARG_KEYS = ("domain", "args", "params", "values", "kwargs",
                      "ids", "fields")

    @classmethod
    def _coerce_json_args(cls, arguments):
        if not isinstance(arguments, dict):
            return arguments
        out = dict(arguments)
        for k in cls._JSON_ARG_KEYS:
            v = out.get(k)
            if isinstance(v, str) and v[:1] in "[{":
                try:
                    out[k] = json.loads(v)
                except ValueError:
                    pass
        return out

    # ------------------------------------------------------------------
    # Guards / checks (operate on self.env + the passed session)
    # ------------------------------------------------------------------
    def _guard(self):
        """Hard invariant: tool execution must NEVER run as superuser."""
        if self.env.su:
            raise AccessError("Tool execution running as superuser — aborting.")

    def _effective_tools(self, session):
        """Tools the caller may use: the session's user, or (remote MCP calls,
        which have no ``aidoo.session``) the authenticated request user."""
        if session:
            return session._effective_tools()
        return self.env.user.sudo()._ai_effective_tools()

    def _check_tool(self, session, tool):
        if tool not in self._effective_tools(session):
            raise AccessError("Tool '%s' is not enabled for you." % tool)

    def _check_model(self, model):
        if model not in self.env:
            raise UserError("Unknown model: %s" % model)
        if model in self.env.user._ai_excluded_models():
            raise AccessError(
                "Model '%s' is not accessible to the AI Assistant." % model)

    def _check_action_method(self, method):
        if not method or method.startswith("_"):
            raise ValidationError("Private/dunder methods are not callable.")
        patterns = self.env["aidoo.session"]._ai_action_method_patterns()
        if not any(fnmatch.fnmatchcase(method, p) for p in patterns):
            raise ValidationError(
                "Method '%s' is not in the AI action allowlist." % method)

    def _sql_table_denylist(self):
        """Admin-configured fnmatch patterns for tables the SQL tool may not read."""
        raw = self.env["ir.config_parameter"].sudo().get_param(
            "aidoo.sql_table_denylist", "") or ""
        return [p.strip().lower() for p in re.split(r"[,\s]+", raw) if p.strip()]

    def _check_sql_access(self, query):
        """Authorize a validated SELECT against the user's SQL access scope.

        - AI SQL Analyst members may read everything EXCEPT the global table
          denylist (raw SQL bypasses record rules, so the denylist is the lever
          to fence off sensitive tables for them).
        - Other users get NO SQL access by default, unless they have a per-user
          table allowlist — then they may read ONLY tables matching it. This lets
          you grant, say, accounting reads to one user without the full-database
          AI SQL Analyst role.
        """
        if not str2bool(self.env["ir.config_parameter"].sudo().get_param(
                "aidoo.sql_enabled", "True"), True):
            raise AccessError("SQL reporting is disabled on this instance.")
        user = self.env.user
        tables = _sql_table_refs(query)
        if user.has_group("aidoo.group_ai_sql_analyst"):
            denied = sorted({t for t in tables
                             for p in self._sql_table_denylist()
                             if fnmatch.fnmatchcase(t, p)})
            if denied:
                raise AccessError(
                    "SQL reporting is not allowed on: %s (blocked by the AI SQL "
                    "table denylist)." % ", ".join(denied))
            return
        # Not an analyst: default-deny, gated by the per-user allowlist.
        allow = user._ai_sql_allow_patterns()
        if not allow:
            raise AccessError(
                "You do not have SQL reporting access. Ask an administrator to "
                "add you to the AI SQL Analyst group or grant specific tables.")
        not_allowed = sorted({t for t in tables
                              if not any(fnmatch.fnmatchcase(t, p) for p in allow)})
        if not_allowed:
            raise AccessError(
                "SQL access to %s is not in your allowed tables." %
                ", ".join(not_allowed))

    def _resolve_server_action(self, action):
        env = self.env
        if isinstance(action, bool):
            raise ValidationError("action must be a server-action id or xmlid.")
        if isinstance(action, int) or (isinstance(action, str) and action.isdigit()):
            sa = env["ir.actions.server"].browse(int(action))
        elif isinstance(action, str) and "." in action:
            try:
                sa = env.ref(action)
            except ValueError:
                raise UserError("Unknown server action xmlid: %s" % action)
        else:
            raise ValidationError("action must be a server-action id or xmlid.")
        if not sa or sa._name != "ir.actions.server" or not sa.exists():
            raise UserError("Server action not found: %s" % action)
        return sa

    @staticmethod
    def _serialize_result(result):
        if hasattr(result, "_name") and hasattr(result, "ids"):
            return {"result_type": "recordset",
                    "model": result._name, "ids": result.ids}
        if isinstance(result, dict):
            safe = json.loads(json.dumps(result, default=str))
            return {"result_type": "action", "action": safe}
        return {"result_type": "value", "result": result}

    # ------------------------------------------------------------------
    # Audit wrapper
    # ------------------------------------------------------------------
    @staticmethod
    def _base_vals(tool, arguments):
        """Audit metadata derived from the call arguments (model/ids/method)."""
        a = arguments or {}
        vals = {}
        if a.get("model"):
            vals["model_name"] = a["model"]
        if a.get("method"):
            vals["method"] = a["method"]
        if a.get("ids") is not None:
            vals["record_ids"] = str(a.get("ids"))
        if a.get("query"):
            vals["sql_text"] = a["query"]
        # Stash the remaining args (sans bulky payloads handled above).
        extra = {k: v for k, v in a.items()
                 if k not in ("model", "method", "query")}
        if extra:
            vals["arguments"] = extra
        return vals

    def _audit(self, session, started, client_ip, vals):
        vals["duration_ms"] = int((time.time() - started) * 1000)
        if client_ip:
            vals["client_ip"] = client_ip
        if session:
            vals["session_id"] = session.id
        vals["user_id"] = self.env.uid
        self.env["aidoo.tool_log"].sudo()._record(vals)

    def _run(self, session, tool, fn, base_vals, client_ip=None):
        started = time.time()
        try:
            result = self._cap_result(fn())
            self._audit(session, started, client_ip,
                        dict(base_vals, tool=tool, outcome="success",
                             result_count=self._count(result)))
            return result
        except AccessError as e:
            self._audit(session, started, client_ip,
                        dict(base_vals, tool=tool, outcome="access_denied",
                             error_message=str(e)))
            return {"error": "AccessError: %s" % e}
        except (ValidationError, UserError) as e:
            self._audit(session, started, client_ip,
                        dict(base_vals, tool=tool, outcome="validation_rejected",
                             error_message=str(e)))
            return {"error": str(e)}
        except Exception as e:  # noqa: BLE001
            _logger.exception("AI tool %s failed", tool)
            self._audit(session, started, client_ip,
                        dict(base_vals, tool=tool, outcome="error",
                             error_message=str(e)))
            return {"error": "%s: %s" % (type(e).__name__, e)}

    @staticmethod
    def _result_bytes(result):
        try:
            return len(json.dumps(result, default=str).encode("utf-8", "ignore"))
        except Exception:  # noqa: BLE001
            return 0

    @staticmethod
    def _clip_row(row, limit):
        """Clip oversized string values in one row. Returns (row, clipped)."""
        if not isinstance(row, dict):
            return row, []
        clipped, out = [], {}
        for key, value in row.items():
            if isinstance(value, str) and len(value) > limit:
                out[key] = "%s... [clipped, %s of %s chars omitted]" % (
                    value[:limit], len(value) - limit, len(value))
                clipped.append(key)
            else:
                out[key] = value
        return out, clipped

    @classmethod
    def _cap_result(cls, result):
        """Keep a tool result inside MAX_RESULT_BYTES.

        MAX_LIMIT bounds how many rows come back but says nothing about how big
        one row can be, and the two failure modes are unrelated: a single
        mail.message body from the cicdoo.mail loop reached 3.6 MB, so an
        inbox sweep reading one row could exceed any context budget on its own.

        Clips the long string fields first and drops rows only if that is not
        enough, because a caller asking for 40 records usually wants to know
        what is there rather than to read any one of them in full. Always says
        plainly what it did: silent truncation would be worse than the size,
        since the model would draw conclusions from a result it believes to be
        complete.
        """
        if cls._result_bytes(result) <= MAX_RESULT_BYTES:
            return result

        if not isinstance(result, dict):
            return {"truncated": True,
                    "error": "Result exceeded %s bytes and was discarded. "
                             "Narrow the request (fewer fields, a tighter "
                             "domain) and try again." % MAX_RESULT_BYTES}

        key = next((k for k in ("records", "rows")
                    if isinstance(result.get(k), list)), None)
        if not key:
            return {"truncated": True,
                    "error": "Result exceeded %s bytes and was discarded. "
                             "Narrow the request and try again."
                             % MAX_RESULT_BYTES}

        clip = 8 * 1024
        clipped_fields, rows = set(), []
        for row in result[key]:
            new_row, clipped = cls._clip_row(row, clip)
            rows.append(new_row)
            clipped_fields.update(clipped)

        capped = dict(result)
        capped[key] = rows
        dropped = 0
        while len(rows) > 1 and cls._result_bytes(capped) > MAX_RESULT_BYTES:
            keep = max(1, len(rows) * 3 // 4)
            dropped += len(rows) - keep
            rows = rows[:keep]
            capped[key] = rows

        note = ["Result exceeded %s bytes." % MAX_RESULT_BYTES]
        if clipped_fields:
            note.append("Long text fields were clipped to %s chars: %s."
                        % (clip, ", ".join(sorted(clipped_fields))))
        if dropped:
            note.append("%s row(s) were dropped; %s of %s returned."
                        % (dropped, len(rows), len(result[key])))
        note.append("Request fewer fields or a tighter domain for the full "
                    "value.")
        capped["truncated"] = " ".join(note)
        return capped

    @staticmethod
    def _count(result):
        if isinstance(result, dict) and "records" in result:
            return len(result["records"])
        if isinstance(result, dict) and "rows" in result:
            return result.get("row_count", 0)
        if isinstance(result, dict) and result.get("result_type") == "recordset":
            return len(result.get("ids") or [])
        if isinstance(result, list):
            return len(result)
        return 0

    # ------------------------------------------------------------------
    # Read tools
    # ------------------------------------------------------------------
    def _t_model_introspect(self, session, model=None, **kw):
        self._check_tool(session, "model_introspect")
        self._check_model(model)
        Model = self.env[model]
        Model.check_access("read")
        fg = Model.fields_get(attributes=[
            "string", "type", "required", "relation", "readonly",
            "selection", "help"])
        access = {op: Model.has_access(op)
                  for op in ("read", "write", "create", "unlink")}
        eff = self._effective_tools(session)
        tool_for = {"read": "orm_read", "write": "orm_write",
                    "create": "orm_create", "unlink": "orm_unlink"}
        effective_access = {op: bool(access[op] and tool_for[op] in eff)
                            for op in access}
        return {"model": model, "access": access,
                "effective_access": effective_access,
                "ai_tools_allowed": sorted(eff), "fields": fg}

    def _t_orm_search_read(self, session, model=None, domain=None, fields=None,
                           limit=80, offset=0, order="", **kw):
        self._check_tool(session, "orm_search_read")
        self._check_model(model)
        dom = domain or []
        if not isinstance(dom, list):
            raise ValidationError("domain must be a list.")
        lim = min(int(limit or 80), MAX_LIMIT)
        Model = self.env[model]
        Model.check_access("read")
        records = Model.search(dom, offset=int(offset or 0), limit=lim,
                               order=order or None)
        data = records.read(fields or [])
        total = Model.search_count(dom)
        return {"records": data, "returned": len(data), "total": total}

    def _t_orm_read(self, session, model=None, ids=None, fields=None, **kw):
        self._check_tool(session, "orm_read")
        self._check_model(model)
        recs = self.env[model].browse(ids or [])
        recs.check_access("read")
        return {"records": recs.read(fields or [])}

    def _t_orm_call(self, session, model=None, method=None, args=None,
                    kwargs=None, **kw):
        self._check_tool(session, "orm_call")
        self._check_model(model)
        if method not in SAFE_METHODS:
            raise ValidationError(
                "Method '%s' is not in the read-only allowlist." % method)
        if method.startswith("_"):
            raise ValidationError("Private methods are not callable.")
        Model = self.env[model]
        Model.check_access("read")
        result = getattr(Model, method)(*(args or []), **(kwargs or {}))
        if hasattr(result, "_name") and hasattr(result, "ids"):
            return {"ids": result.ids}
        return {"result": result}

    def _t_sql_select(self, session, query=None, params=None, max_rows=1000, **kw):
        self._check_tool(session, "sql_select")
        _validate_select(query)
        self._check_sql_access(query)
        cap = min(int(max_rows or 1000), SQL_MAX_ROWS)
        try:
            timeout_ms = max(1000, int(self.env["ir.config_parameter"].sudo()
                                       .get_param("aidoo.sql_statement_timeout_ms",
                                                  10000)))
        except (TypeError, ValueError):
            timeout_ms = 10000
        cr = self.env.cr
        with cr.savepoint():
            cr.execute("SET TRANSACTION READ ONLY")
            cr.execute("SET LOCAL statement_timeout = %s", (timeout_ms,))
            cr.execute(query, tuple(params or ()))
            rows = cr.fetchmany(cap + 1)
            truncated = len(rows) > cap
            rows = rows[:cap]
            cols = [d.name for d in cr.description] if cr.description else []
        return {"columns": cols, "rows": [list(r) for r in rows],
                "row_count": len(rows), "truncated": truncated}

    # ------------------------------------------------------------------
    # Write tools
    # ------------------------------------------------------------------
    def _validate_m2o_domains(self, Model, values):
        """Reject many2one values that violate the field's static list domain.

        Field domains are only enforced by the web client, so an AI-guessed id
        can land a record the UI could never produce (e.g. a mailing.mailing
        whose mailing_model_id points at the abstract 'base' model, which then
        crashes the form's DomainSelector). Only plain list domains are checked;
        string/callable domains stay UI-only, and a failing search (e.g. a
        non-searchable compute in the domain) skips the check rather than
        blocking the write.
        """
        for fname, val in values.items():
            field = Model._fields.get(fname)
            if field is None or field.type != "many2one":
                continue
            if not isinstance(val, int) or isinstance(val, bool) or val <= 0:
                continue
            domain = field.domain
            if not isinstance(domain, (list, tuple)) or not domain:
                continue
            try:
                ok = self.env[field.comodel_name].search_count(
                    expression.AND([[("id", "=", val)], list(domain)])) == 1
            except Exception:
                _logger.debug(
                    "aidoo: could not enforce domain %s on %s.%s",
                    domain, Model._name, fname, exc_info=True)
                continue
            if not ok:
                raise ValidationError(
                    "Value %s for '%s' does not satisfy the field domain %s "
                    "on %s. Search %s with that domain to find a valid id "
                    "instead of guessing." % (
                        val, fname, list(domain), field.comodel_name,
                        field.comodel_name))

    def _t_orm_create(self, session, model=None, values=None, **kw):
        self._check_tool(session, "orm_create")
        self._check_model(model)
        if not isinstance(values, dict):
            raise ValidationError("values must be an object.")
        Model = self.env[model]
        Model.check_access("create")
        self._validate_m2o_domains(Model, values)
        rec = Model.create(values)
        return {"ids": rec.ids}

    def _t_orm_write(self, session, model=None, ids=None, values=None, **kw):
        self._check_tool(session, "orm_write")
        self._check_model(model)
        if not isinstance(values, dict):
            raise ValidationError("values must be an object.")
        recs = self.env[model].browse(ids or [])
        recs.check_access("write")
        self._validate_m2o_domains(recs, values)
        recs.write(values)
        return {"written": len(recs), "ids": recs.ids}

    def _t_orm_unlink(self, session, model=None, ids=None, **kw):
        self._check_tool(session, "orm_unlink")
        self._check_model(model)
        recs = self.env[model].browse(ids or [])
        recs.check_access("unlink")
        count = len(recs)
        recs.unlink()
        return {"unlinked": count}

    # ------------------------------------------------------------------
    # Action tools
    # ------------------------------------------------------------------
    def _t_orm_action(self, session, model=None, ids=None, method=None,
                      args=None, kwargs=None, **kw):
        self._check_tool(session, "orm_action")
        self._check_model(model)
        self._check_action_method(method)
        if args is not None and not isinstance(args, list):
            raise ValidationError("args must be a list.")
        if kwargs is not None and not isinstance(kwargs, dict):
            raise ValidationError("kwargs must be an object.")
        Model = self.env[model]
        target = Model.browse(ids) if ids else Model
        target.check_access("write")
        # Tell the action which chat session called it, so it can hand files
        # back into that session (e.g. an image preview in uploads/). The model
        # cannot set this: its kwargs are method arguments, not context.
        target = target.with_context(aidoo_session_id=session.id)
        result = getattr(target, method)(*(args or []), **(kwargs or {}))
        return self._serialize_result(result)

    def _t_run_wizard(self, session, model=None, values=None, method=None,
                      args=None, kwargs=None, **kw):
        self._check_tool(session, "run_wizard")
        self._check_model(model)
        self._check_action_method(method)
        if values is not None and not isinstance(values, dict):
            raise ValidationError("values must be an object.")
        if args is not None and not isinstance(args, list):
            raise ValidationError("args must be a list.")
        if kwargs is not None and not isinstance(kwargs, dict):
            raise ValidationError("kwargs must be an object.")
        Model = self.env[model]
        if not Model._transient:
            raise ValidationError(
                "run_wizard only operates on transient (wizard) models.")
        Model.check_access("create")
        wizard = Model.create(values or {})
        result = getattr(wizard, method)(*(args or []), **(kwargs or {}))
        return self._serialize_result(result)

    def _t_run_server_action(self, session, action=None, ids=None, **kw):
        self._check_tool(session, "run_server_action")
        sa = self._resolve_server_action(action)
        allowed = self.env["aidoo.session"]._ai_allowed_server_action_ids()
        if not allowed or sa.id not in allowed:
            raise AccessError(
                "Server action %s is not allowlisted for the AI Assistant." % action)
        target_model = sa.model_id.model
        self._check_model(target_model)
        Model = self.env[target_model]
        recs = Model.browse(ids or [])
        (recs if ids else Model).check_access("write")
        ctx = dict(self.env.context, active_model=target_model,
                   active_ids=recs.ids, active_id=recs.ids[0] if recs.ids else False)
        result = sa.with_context(**ctx).run()
        return self._serialize_result(result)
