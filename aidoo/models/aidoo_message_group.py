# -*- coding: utf-8 -*-
# Copyright 2026 CICDoo (https://cicdoo.com)
# SPDX-License-Identifier: LGPL-3.0-or-later OR LicenseRef-Aidoo-Commercial
# Dual-licensed: open source (LGPL-3, see LICENSE) or commercial (see COMMERCIAL_LICENSE.md).
from odoo import fields, models


class AiAssistantMessageGroup(models.Model):
    _name = "aidoo.message.group"
    _description = "Aidoo AI Assistant Message Group"
    _order = "sequence, id"

    session_id = fields.Many2one(
        "aidoo.session", required=True, ondelete="cascade", index=True)
    name = fields.Char(required=True, default="Group")
    sequence = fields.Integer(default=10)
    collapsed = fields.Boolean(default=False)

    def _to_frontend(self):
        return [{
            "id": g.id,
            "name": g.name,
            "collapsed": g.collapsed,
            "sequence": g.sequence,
        } for g in self]
