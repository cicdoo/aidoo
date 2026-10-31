# -*- coding: utf-8 -*-
# Copyright 2026 CICDoo (https://cicdoo.com)
# SPDX-License-Identifier: LGPL-3.0-or-later OR LicenseRef-Aidoo-Commercial
# Dual-licensed: open source (LGPL-3, see LICENSE) or commercial (see COMMERCIAL_LICENSE.md).
"""Tell a tool call from a person.

``aidoo.tool.exec._dispatch`` runs every tool call (chat, scheduled agents,
native providers, remote MCP) with ``aidoo_tool_call=True`` in the context. A
model cannot set or clear it: tool arguments become method arguments, never
context. Methods that record a human decision (approve, reject, confirm) call
``require_human`` so the agent can never take that decision itself, even when
it is acting as the very person whose decision it is.
"""
from odoo.exceptions import AccessError

CTX_TOOL_CALL = "aidoo_tool_call"
CTX_SESSION_ID = "aidoo_session_id"


def is_tool_call(env):
    return bool(env.context.get(CTX_TOOL_CALL))


def tool_session(env):
    """The chat session behind this tool call, or an empty recordset (no tool
    call, or a remote MCP call, which has no session)."""
    sid = env.context.get(CTX_SESSION_ID) if is_tool_call(env) else False
    return env["aidoo.session"].sudo().browse(sid or []).exists()


def require_human(env, what="This decision"):
    if is_tool_call(env):
        raise AccessError(
            "%s must be made by a person in Odoo, not through an AI tool." % what)
