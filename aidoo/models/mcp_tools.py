# -*- coding: utf-8 -*-
# Copyright 2026 CICDoo (https://cicdoo.com)
# SPDX-License-Identifier: LGPL-3.0-or-later OR LicenseRef-Aidoo-Commercial
# Dual-licensed: open source (LGPL-3, see LICENSE) or commercial (see COMMERCIAL_LICENSE.md).
"""MCP tool definitions served by the remote endpoint (``/aidoo/mcp``).

``bridge/mcp_server.py`` is a standalone script that must never import Odoo, so
it keeps its own copy of these definitions; ``tests/test_mcp_server.py`` asserts
the two stay identical.
"""

MCP_TOOL_DEFS = [
    {
        "name": "model_introspect",
        "description": "Inspect an Odoo model: its fields and your access rights "
                       "(read/write/create/unlink). Call this before querying an "
                       "unfamiliar model.",
        "inputSchema": {
            "type": "object",
            "properties": {"model": {"type": "string"}},
            "required": ["model"],
        },
    },
    {
        "name": "orm_search_read",
        "description": "Search and read records of an Odoo model. Honors the user's "
                       "access rights and record rules. domain is an Odoo domain "
                       "(list of triples).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "model": {"type": "string"},
                "domain": {"type": "array", "default": []},
                "fields": {"type": "array", "items": {"type": "string"},
                           "default": []},
                "limit": {"type": "integer", "default": 80},
                "offset": {"type": "integer", "default": 0},
                "order": {"type": "string", "default": ""},
            },
            "required": ["model"],
        },
    },
    {
        "name": "orm_read",
        "description": "Read specific records by id from an Odoo model.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "model": {"type": "string"},
                "ids": {"type": "array", "items": {"type": "integer"}},
                "fields": {"type": "array", "items": {"type": "string"},
                           "default": []},
            },
            "required": ["model", "ids"],
        },
    },
    {
        "name": "orm_call",
        "description": "Call a read-only model method (allowlisted: name_search, "
                       "read_group, search_count, fields_get, default_get).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "model": {"type": "string"},
                "method": {"type": "string"},
                "args": {"type": "array", "default": []},
                "kwargs": {"type": "object", "default": {}},
            },
            "required": ["model", "method"],
        },
    },
    {
        "name": "sql_select",
        "description": "Run a READ-ONLY SQL SELECT for reporting. Only SELECT/WITH "
                       "queries are allowed; any write is rejected. Available only to "
                       "members of the AI SQL Analyst group.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "params": {"type": "array", "default": []},
                "max_rows": {"type": "integer", "default": 1000},
            },
            "required": ["query"],
        },
    },
    {
        "name": "orm_create",
        "description": "Create a record on an Odoo model. Honors the user's access "
                       "rights. values is a dict of field -> value. Only available "
                       "when granted to the user.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "model": {"type": "string"},
                "values": {"type": "object"},
            },
            "required": ["model", "values"],
        },
    },
    {
        "name": "orm_write",
        "description": "Update existing records on an Odoo model. Honors the user's "
                       "access rights and record rules. Only available when granted "
                       "to the user.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "model": {"type": "string"},
                "ids": {"type": "array", "items": {"type": "integer"}},
                "values": {"type": "object"},
            },
            "required": ["model", "ids", "values"],
        },
    },
    {
        "name": "orm_unlink",
        "description": "Delete records of an Odoo model by id. Honors the user's "
                       "access rights and record rules. Only available when granted "
                       "to the user.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "model": {"type": "string"},
                "ids": {"type": "array", "items": {"type": "integer"}},
            },
            "required": ["model", "ids"],
        },
    },
    {
        "name": "orm_action",
        "description": "Call an allowlisted business/action method (e.g. "
                       "action_confirm, action_post, button_validate) on a "
                       "recordset. Omit ids for a model-level call. Honors the "
                       "user's access rights. Only available when granted.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "model": {"type": "string"},
                "ids": {"type": "array", "items": {"type": "integer"},
                        "default": []},
                "method": {"type": "string"},
                "args": {"type": "array", "default": []},
                "kwargs": {"type": "object", "default": {}},
            },
            "required": ["model", "method"],
        },
    },
    {
        "name": "run_wizard",
        "description": "Create a wizard (transient model) with values and run its "
                       "allowlisted button method in one step. Honors the user's "
                       "access rights. Only available when granted.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "model": {"type": "string"},
                "values": {"type": "object", "default": {}},
                "method": {"type": "string"},
                "args": {"type": "array", "default": []},
                "kwargs": {"type": "object", "default": {}},
            },
            "required": ["model", "method"],
        },
    },
    {
        "name": "run_server_action",
        "description": "Run an admin-allowlisted Odoo server action "
                       "(ir.actions.server) against record ids. action is the "
                       "server action's id or xmlid. Only available when granted.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {"type": ["integer", "string"]},
                "ids": {"type": "array", "items": {"type": "integer"},
                        "default": []},
            },
            "required": ["action"],
        },
    },
]


def visible_tools(names):
    """Definitions of the tools whose name is in ``names``."""
    return [t for t in MCP_TOOL_DEFS if t["name"] in names]
