# -*- coding: utf-8 -*-
# Copyright 2026 CICDoo (https://cicdoo.com)
# SPDX-License-Identifier: LGPL-3.0-or-later OR LicenseRef-Aidoo-Commercial
# Dual-licensed: open source (LGPL-3, see LICENSE) or commercial (see COMMERCIAL_LICENSE.md).
"""Presence for Aidoo persona bots.

Persona partners are backed by non-login service users, so core's
``_compute_im_status`` would show them offline. Mirror OdooBot (``base.partner_root``,
which core hardcodes to ``'bot'``) so every persona shows the bot indicator in the
Discuss members list instead of an offline dot."""
from odoo import models


class ResPartner(models.Model):
    _inherit = "res.partner"

    def _compute_im_status(self):
        super()._compute_im_status()
        try:
            bots = self.env["aidoo.persona"].sudo().search([]).mapped("partner_id")
        except Exception:  # noqa: BLE001 — never let presence break (e.g. pre-install)
            bots = self.env["res.partner"]
        legacy = self.env.ref("aidoo.partner_aidoo_bot", raise_if_not_found=False)
        if legacy:
            bots |= legacy
        for partner in self & bots:
            partner.im_status = "bot"
