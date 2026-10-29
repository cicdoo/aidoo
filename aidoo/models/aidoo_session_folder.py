# -*- coding: utf-8 -*-
# Copyright 2026 CICDoo (https://cicdoo.com)
# SPDX-License-Identifier: LGPL-3.0-or-later OR LicenseRef-Aidoo-Commercial
# Dual-licensed: open source (LGPL-3, see LICENSE) or commercial (see COMMERCIAL_LICENSE.md).
from odoo import fields, models


class AiAssistantSessionFolder(models.Model):
    _name = "aidoo.session.folder"
    _description = "Aidoo AI Assistant Conversation Folder"
    _order = "sequence, id"

    name = fields.Char(required=True)
    user_id = fields.Many2one(
        "res.users", required=True, ondelete="cascade", index=True,
        default=lambda self: self.env.user)
    sequence = fields.Integer(default=10)
    collapsed = fields.Boolean(default=False)

    def _to_frontend(self):
        return [{
            "id": f.id,
            "name": f.name,
            "collapsed": f.collapsed,
            "sequence": f.sequence,
        } for f in self]
