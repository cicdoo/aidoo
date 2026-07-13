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
* The "New message" (``im_search``) and "Invite people"
  (``search_for_channel_invite``) searches only return internal users
  (``share = False``), so a persona could never be DM'd or invited from the UI.
  Both are extended to surface persona partners; the DM itself needs no mention
  (``aidoo.session._maybe_handle_discuss`` answers every message in a 1:1 chat
  with a persona member)."""
from odoo import api, models
from odoo.addons.mail.tools.discuss import Store


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

    @staticmethod
    def _aidoo_merge_store_result(res, extra):
        """Merge a second ``Store().get_result()`` payload into ``res`` in
        place, skipping records already present (the web client store upserts
        by id, so plain appending is safe)."""
        for key, values in extra.items():
            if not isinstance(values, list):
                res.setdefault(key, values)
                continue
            bucket = res.setdefault(key, [])
            seen = {v.get("id") for v in bucket if isinstance(v, dict)}
            bucket.extend(
                v for v in values
                if not isinstance(v, dict) or v.get("id") not in seen)

    @api.model
    def get_mention_suggestions(self, search, limit=8):
        # Odoo 19 dropped res.partner.im_search (the 18 "New message" search);
        # partner suggestions now flow through get_mention_suggestions, so the
        # persona bots are surfaced here instead.
        res = super().get_mention_suggestions(search, limit=limit)
        bots = self._aidoo_persona_partners_matching(search)
        if bots:
            self._aidoo_merge_store_result(res, Store(bots.sudo()).get_result())
        return res

    @api.model
    def search_for_channel_invite(self, search_term, channel_id=None, limit=30):
        res = super().search_for_channel_invite(
            search_term, channel_id=channel_id, limit=limit)
        bots = self._aidoo_persona_partners_matching(search_term)
        channel = self.env["discuss.channel"].sudo().browse(
            int(channel_id)) if channel_id else self.env["discuss.channel"]
        if channel:
            bots -= channel.channel_partner_ids
        if bots:
            # Odoo 19's search_for_channel_invite returns {count, partner_ids,
            # store_data, …} — the matching partners live in ``store_data`` and
            # are referenced by ``partner_ids`` (18's flat ``data`` key is gone).
            store = Store()
            bots.sudo()._search_for_channel_invite_to_store(store, channel)
            self._aidoo_merge_store_result(res["store_data"], store.get_result())
            res["partner_ids"] = list(res.get("partner_ids") or []) + bots.ids
            res["count"] += len(bots)
        return res
