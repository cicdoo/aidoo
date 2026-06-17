# -*- coding: utf-8 -*-
# Copyright 2026 CICDoo (https://cicdoo.com)
# SPDX-License-Identifier: LGPL-3.0-or-later OR LicenseRef-Aidoo-Commercial
# Dual-licensed: open source (LGPL-3, see LICENSE) or commercial (see COMMERCIAL_LICENSE.md).
from odoo import fields, models


class AiAssistantTool(models.Model):
    """Catalog of the tools the assistant can expose to the model.

    One record per tool (seeded from data/aidoo_tool_data.xml and kept in
    sync with the READ_TOOLS/WRITE_TOOLS code constants in aidoo_session).
    It is the join target for the per-user `res.users.aidoo_tool_ids`
    Many2many that decides which tools each user may invoke.
    """
    _name = "aidoo.tool"
    _description = "Aidoo AI Assistant Tool"
    _order = "readonly desc, name"
    _rec_name = "label"

    name = fields.Char(
        required=True, index=True, copy=False,
        help="Technical tool name (matches the MCP/controller endpoint), "
             "e.g. orm_write.")
    label = fields.Char(required=True, help="Human-readable label.")
    readonly = fields.Boolean(
        default=False,
        help="Read-only tools stay available when zero-trust mode is on.")
    active = fields.Boolean(default=True)

    # Odoo 19: SQL constraints are declared as models.Constraint attributes.
    _name_uniq = models.Constraint("unique(name)", "Tool name must be unique.")
