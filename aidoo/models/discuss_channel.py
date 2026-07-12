# -*- coding: utf-8 -*-
# Copyright 2026 CICDoo (https://cicdoo.com)
# SPDX-License-Identifier: LGPL-3.0-or-later OR LicenseRef-Aidoo-Commercial
# Dual-licensed: open source (LGPL-3, see LICENSE) or commercial (see COMMERCIAL_LICENSE.md).
"""Discuss bot hook.

Mirrors OdooBot's pattern (``mail_bot``): when a human posts a message in a
channel the Aidoo bot belongs to, kick off an Aidoo turn as that user. The reply
is posted back into the channel by ``aidoo.session._deliver_discuss`` once the
background run finishes.
"""
import logging

from odoo import models

_logger = logging.getLogger(__name__)


class DiscussChannel(models.Model):
    _inherit = "discuss.channel"

    def _message_post_after_hook(self, message, msg_values):
        res = super()._message_post_after_hook(message, msg_values)
        try:
            self.env["aidoo.session"]._maybe_handle_discuss(self, msg_values, message=message)
        except Exception:  # noqa: BLE001
            # Never let assistant handling break a normal message post.
            _logger.exception("aidoo: discuss bot handling failed")
        return res

    def _notify_get_recipients(self, message, msg_vals=False, **kwargs):
        """Never notify persona bots. Their service users are portal (email-only)
        with no real inbox, so @mentioning one would try to email a bot address
        and fail — leaving the sender's message greyed out with an error. Personas
        pick up the message through ``_maybe_handle_discuss`` instead, so they need
        no mail notification at all."""
        recipients = super()._notify_get_recipients(message, msg_vals=msg_vals, **kwargs)
        if not recipients:
            return recipients
        bot_ids = set(self.env["aidoo.persona"].sudo().search([]).partner_id.ids)
        if not bot_ids:
            return recipients
        return [r for r in recipients if r.get("id") not in bot_ids]
