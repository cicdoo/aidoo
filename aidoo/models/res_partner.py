# -*- coding: utf-8 -*-
# Copyright 2026 CICDoo (https://cicdoo.com)
# SPDX-License-Identifier: LGPL-3.0-or-later OR LicenseRef-Aidoo-Commercial
# Dual-licensed: open source (LGPL-3, see LICENSE) or commercial (see COMMERCIAL_LICENSE.md).
"""Discuss integration for Aidoo persona bots.

Persona partners are backed by non-login **portal** service users (no internal
seat), which core's Discuss hides in two ways this file undoes:

* ``_compute_im_status`` would show them offline — mirror OdooBot
  (``base.partner_root``, hardcoded to ``'bot'``) so every persona shows the bot
  indicator in the Discuss members list instead of an offline dot.
* The "New message" (the ``/discuss/search`` route) and "Invite people"
  (``search_for_channel_invite``) searches both funnel through
  ``res.partner._search_for_channel_invite``, whose domain only returns internal
  users (``user_ids.share = False``), so a persona could never be DM'd or invited
  from the UI. That one internal method is extended to surface persona partners;
  the DM itself needs no mention (``aidoo.session._maybe_handle_discuss`` answers
  every message in a 1:1 chat with a persona member)."""
from odoo import api, models


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

    # ------------------------------------------------------------------
    # Persona discoverability in Discuss searches
    # ------------------------------------------------------------------
    def _aidoo_persona_partners_matching(self, name, excluded_ids=None):
        """Active persona partners whose name matches ``name`` (empty matches
        all), excluding ``excluded_ids``. Personas are only offered to users
        who may actually talk to them (the AI-user group)."""
        try:
            if not self.env.user.has_group("aidoo.group_ai_user"):
                return self.env["res.partner"]
            bots = self.env["aidoo.persona"]._active_personas().partner_id
        except Exception:  # noqa: BLE001 — never break a core search (pre-install)
            return self.env["res.partner"]
        needle = (name or "").strip().lower()
        excluded = set(excluded_ids or [])
        return bots.filtered(
            lambda p: p.id not in excluded
            and (not needle or needle in (p.name or "").lower()))

    @api.model
    def _search_for_channel_invite(self, store, search_term, channel_id=None, limit=30):
        # Odoo 19 routes BOTH "New message" (the /discuss/search route) and
        # "Invite people" (the public search_for_channel_invite) through this one
        # internal helper, which fills ``store`` and returns {count, partner_ids}.
        # Core's own domain here requires ``user_ids.share = False``, so it never
        # surfaces the personas' portal service users — we add them explicitly,
        # gated to AI users. (18 hooked the removed ``im_search`` + the public
        # method separately; one internal override now covers both surfaces.)
        res = super()._search_for_channel_invite(
            store, search_term, channel_id=channel_id, limit=limit)
        bots = self._aidoo_persona_partners_matching(search_term)
        channel = self.env["discuss.channel"].sudo().browse(
            int(channel_id)) if channel_id else self.env["discuss.channel"]
        if channel:
            bots -= channel.channel_partner_ids
        if bots:
            bots.sudo()._search_for_channel_invite_to_store(store, channel)
            # Odoo 19's "New message" command palette filters the store
            # client-side to internal users (main_user_id.share === false), which
            # drops the personas' portal service users. Tag them with a synthetic
            # store field so aidoo's command-palette patch can re-add them.
            store.add(bots.sudo(), {"is_aidoo_persona": True})
            res["partner_ids"] = list(res.get("partner_ids") or []) + bots.ids
            res["count"] += len(bots)
        return res
