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
* The "New message" (``/discuss/search``) and "Invite people"
  (``search_for_channel_invite``) searches (both built on
  ``_search_for_channel_invite``) only return internal users
  (``share = False``), so a persona could never be DM'd or invited from the UI.
  Both are extended to surface persona partners; the DM itself needs no mention
  (``aidoo.session._maybe_handle_discuss`` answers every message in a 1:1 chat
  with a persona member)."""
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

    @api.readonly
    @api.model
    def _search_for_channel_invite(
        self, store, search_term, channel_id=None, limit=30,
        with_portal_users=False,
    ):
        """Shared by Discuss's "New message" search (``/discuss/search``) and
        the "Invite people" panel: append the persona partners to the
        internal-user results."""
        partner_ids = super()._search_for_channel_invite(
            store, search_term, channel_id=channel_id, limit=limit,
            with_portal_users=with_portal_users)
        bots = self._aidoo_persona_partners_matching(search_term)
        channel = self.env["discuss.channel"]
        if channel_id:
            channel = channel.sudo().browse(int(channel_id)).exists()
        if channel:
            bots -= channel.channel_partner_ids
        if bots:
            store.add(
                bots.sudo(), "_store_channel_invite_fields",
                fields_params={"channel": channel})
            partner_ids = list(partner_ids) + [
                pid for pid in bots.ids if pid not in partner_ids]
        return partner_ids
