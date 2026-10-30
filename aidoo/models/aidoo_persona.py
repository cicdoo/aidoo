# -*- coding: utf-8 -*-
# Copyright 2026 CICDoo (https://cicdoo.com)
# SPDX-License-Identifier: LGPL-3.0-or-later OR LicenseRef-Aidoo-Commercial
# Dual-licensed: open source (LGPL-3, see LICENSE) or commercial (see COMMERCIAL_LICENSE.md).
"""AI Personas — named Discuss bots, each with its own provider account + skills.

A persona is a reusable AI identity an admin publishes to Discuss. Unlike a plain
chat (which runs entirely as the invoking user with that user's own credentials),
a persona splits the two identities:

* **Credential user** — a dedicated, non-login service ``res.users`` created per
  persona (``bot_user_id``). It owns the persona's provider credentials on disk
  (keyed by its user id, exactly like any human user) and carries the partner used
  to @mention the persona in Discuss.
* **Acting user** — still the human who invoked the persona. Every ORM/SQL tool
  runs with *their* Odoo rights (see ``aidoo.session._credential_user`` vs the
  worker ``uid``), so a persona never escalates privilege.

The persona supplies the AI account, the model, a personality (``instructions``)
and a bundle of ``skill_ids`` whose text rides every turn's prompt.
"""
import logging
import re

from odoo import api, fields, models, _
from odoo.exceptions import AccessError, UserError, ValidationError

from .aidoo_session import PROVIDERS

_logger = logging.getLogger(__name__)


class AidooPersona(models.Model):
    _name = "aidoo.persona"
    _description = "Aidoo AI Persona (named Discuss bot)"
    _order = "is_default desc, name"

    name = fields.Char(
        required=True, index=True,
        help="Display name and @mention handle in Discuss, e.g. 'SalesBot'.")
    active = fields.Boolean(default=True)
    is_default = fields.Boolean(
        string="Default persona", copy=False,
        help="The built-in Aidoo persona. There is exactly one; it cannot be "
             "deleted.")
    provider = fields.Selection(
        PROVIDERS, string="AI Provider", required=True,
        default=lambda self: self.env["aidoo.session"]._default_provider(),
        help="Which provider account drives this persona.")
    model = fields.Char(
        string="Model",
        help="Optional model override (e.g. 'glm-4.6'). Leave empty for the "
             "provider's configured default.")
    bot_user_id = fields.Many2one(
        "res.users", string="Service Account", copy=False,
        ondelete="cascade", index=True,
        help="Dedicated non-login user that holds this persona's provider "
             "credentials and its Discuss identity. Created automatically on "
             "save.")
    partner_id = fields.Many2one(
        "res.partner", string="Discuss Identity",
        related="bot_user_id.partner_id", store=True, index=True,
        help="Partner used to @mention the persona and post its replies.")
    email = fields.Char(
        compute="_compute_email", inverse="_inverse_email",
        string="Email Identity",
        help="Optional email address this persona sends from (e.g. a "
             "scheduled agent's email delivery). Leave empty to send with "
             "the system default sender.")
    instructions = fields.Text(
        string="Personality / System Prompt",
        help="Free-form instructions describing who this persona is and how it "
             "should behave. Injected into every turn's prompt.")
    skill_ids = fields.Many2many(
        "aidoo.skill", "aidoo_persona_skill_rel", "persona_id", "skill_id",
        string="Skills",
        help="Skills whose instructions ride every turn for this persona.")
    principal_user_ids = fields.Many2many(
        "res.users", "aidoo_persona_principal_rel", "persona_id", "user_id",
        string="Principals",
        help="The people this persona works for. Only they can approve its "
             "change requests, and the persona is told, from the server, whether "
             "the person talking to it is one of them. Empty = no principal, so "
             "nobody can approve its change requests.")
    authenticated = fields.Boolean(
        string="Account Connected", compute="_compute_authenticated",
        help="Whether this persona's provider account has usable credentials.")

    # Inputs for the backend connect flow. Secret inputs are cleared right after
    # use (in the same request) so keys/credentials do not linger in the database.
    api_key_input = fields.Char(string="API Key", copy=False)
    creds_input = fields.Text(string="Credentials JSON", copy=False)
    code_input = fields.Char(string="Login Code", copy=False)
    auth_hint = fields.Char(
        string="Login Instructions", readonly=True, copy=False,
        help="URL / device code returned by the provider's login start step.")

    _bot_user_uniq = models.Constraint(
        "unique(bot_user_id)",
        "Each persona must have its own dedicated service account.")

    @api.constrains("name", "active")
    def _check_unique_name(self):
        """Handles must be unique (case-insensitive) so @mentions are
        unambiguous. Only active personas are compared."""
        for persona in self:
            if not persona.name or not persona.active:
                continue
            dup = self.sudo().search_count([
                ("id", "!=", persona.id),
                ("active", "=", True),
                ("name", "=ilike", persona.name),
            ])
            if dup:
                raise ValidationError(_(
                    "A persona named '%s' already exists. Persona handles must "
                    "be unique.") % persona.name)

    # ------------------------------------------------------------------
    # Provider adapter + auth state
    # ------------------------------------------------------------------
    def _adapter(self):
        self.ensure_one()
        return self.env["aidoo.provider.%s" % self.provider]

    def _credential_user(self):
        """The user whose on-disk credentials this persona runs under."""
        self.ensure_one()
        return self.bot_user_id

    def action_open_chat(self):
        """Open (creating if needed) the caller's 1:1 Discuss chat with this
        persona. Same URL pattern core uses to open a channel."""
        self.ensure_one()
        channel = self.env["discuss.channel"]._get_or_create_chat([self.partner_id.id])
        return {
            "type": "ir.actions.act_url",
            "url": "/odoo/action-mail.action_discuss?active_id=%s" % channel.id,
            "target": "self",
        }

    @api.depends("bot_user_id.email")
    def _compute_email(self):
        for persona in self:
            persona.email = persona.bot_user_id.email

    def _inverse_email(self):
        # The service account is a system-managed record; managers edit the
        # persona, not res.users — write through with sudo (same pattern as
        # the auth actions below).
        for persona in self:
            if persona.bot_user_id:
                persona.bot_user_id.sudo().email = persona.email

    @api.depends("provider", "bot_user_id")
    def _compute_authenticated(self):
        for persona in self:
            try:
                persona.authenticated = bool(
                    persona.bot_user_id
                    and persona._adapter()._is_authenticated(
                        persona.bot_user_id.sudo()))
            except Exception:  # noqa: BLE001
                persona.authenticated = False

    # ------------------------------------------------------------------
    # Service-account provisioning
    # ------------------------------------------------------------------
    @api.model
    def _unique_login(self, name):
        base = "aidoo_persona_%s" % (
            re.sub(r"[^a-z0-9]+", "_", (name or "persona").lower()).strip("_")
            or "persona")
        login, n = base, 1
        Users = self.env["res.users"].sudo().with_context(active_test=False)
        while Users.search_count([("login", "=", login)]):
            n += 1
            login = "%s_%s" % (base, n)
        return login

    @api.model
    def _provision_bot_user(self, name, partner=None):
        """Create the dedicated non-login **portal** service user for a persona.

        It is a portal (share) user, not internal: it never logs in and never acts
        (tools run as the invoking user), so it consumes no internal seat and only
        holds provider credentials + the Discuss identity. When ``partner`` is
        given, the user adopts that existing partner (used by the default persona
        to keep the historical ``@aidoo`` identity)."""
        # Assign only the portal group so ``share`` computes to True; omit
        # ``password`` (a user without one cannot log in, and password=False would
        # trip res.users' password-hash inverse). Portal users must notify by
        # email (mail DB constraint).
        vals = {
            "name": name or _("Persona"),
            "login": self._unique_login(name),
            "active": True,
            "group_ids": [(6, 0, self.env.ref("base.group_portal").ids)],
            "notification_type": "email",
        }
        if partner:
            vals["partner_id"] = partner.id
        return self.env["res.users"].sudo().with_context(
            no_reset_password=True).create(vals)

    @api.model
    def _demote_bot_users_to_portal(self):
        """Convert any persona service user still carrying the internal-user group
        into a portal user. Idempotent; run from the post-init hook so existing
        installs (whose bot users were created as internal) are migrated."""
        portal = self.env.ref("base.group_portal")
        internal = self.env.ref("base.group_user")
        bots = self.sudo().search([]).bot_user_id.filtered(
            lambda u: u.has_group("base.group_user"))
        if bots:
            bots.sudo().write({"group_ids": [(3, internal.id), (4, portal.id)]})

    def _check_manager(self):
        if not (self.env.su or self.env.user.has_group("aidoo.group_ai_manager")):
            raise AccessError(_(
                "Only an AI Assistant Manager can manage personas."))

    @api.model_create_multi
    def create(self, vals_list):
        # Create/write/unlink are already restricted to AI managers by the ACL
        # (ir.model.access); this override only auto-provisions the service user.
        for vals in vals_list:
            if not vals.get("bot_user_id"):
                vals["bot_user_id"] = self._provision_bot_user(
                    vals.get("name")).id
        return super().create(vals_list)

    def unlink(self):
        if any(p.is_default for p in self):
            raise UserError(_("The default persona cannot be deleted."))
        bots = self.bot_user_id
        res = super().unlink()
        bots.sudo().unlink()  # cascade cleans the service accounts
        return res

    # ------------------------------------------------------------------
    # Backend connect flow — reuse the provider adapter _auth_* methods,
    # scoped to this persona's service account instead of the current user.
    # ------------------------------------------------------------------
    def _notify(self, message, title=None, sticky=False, kind="info"):
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": title or _("Aidoo"),
                "message": message,
                "type": kind,
                "sticky": sticky,
            },
        }

    def action_auth_set_key(self):
        self.ensure_one()
        self._check_manager()
        key = (self.api_key_input or "").strip()
        if not key:
            raise UserError(_("Enter an API key first."))
        self._adapter()._auth_set_key(self.bot_user_id.sudo(), key)
        self.api_key_input = False  # never persist the secret
        self._compute_authenticated()
        return self._notify(
            _("%s is now connected.") % self.name,
            title=self.name, kind="success")

    def action_auth_import(self):
        self.ensure_one()
        self._check_manager()
        blob = (self.creds_input or "").strip()
        if not blob:
            raise UserError(_("Paste the credentials JSON first."))
        self._adapter()._auth_import(self.bot_user_id.sudo(), blob)
        self.creds_input = False
        self._compute_authenticated()
        return self._notify(
            _("%s is now connected.") % self.name,
            title=self.name, kind="success")

    def action_auth_start(self):
        self.ensure_one()
        self._check_manager()
        res = self._adapter()._auth_start(self.bot_user_id.sudo()) or {}
        url = res.get("url") or ""
        code = res.get("code")
        hint = url
        if code:
            hint = _("Open %(url)s and enter code %(code)s") % {
                "url": url, "code": code}
        self.auth_hint = hint
        return self._notify(
            hint or _("Follow the provider login, then paste the code below."),
            title=_("Complete login"), sticky=True)

    def action_auth_complete(self):
        self.ensure_one()
        self._check_manager()
        code = (self.code_input or "").strip()
        self._adapter()._auth_complete(self.bot_user_id.sudo(), code or None)
        self.code_input = False
        self.auth_hint = False
        self._compute_authenticated()
        return self._notify(
            _("%s is now connected.") % self.name,
            title=self.name, kind="success")

    def action_auth_logout(self):
        self.ensure_one()
        self._check_manager()
        self._adapter()._auth_logout(self.bot_user_id.sudo())
        self._compute_authenticated()
        return self._notify(
            _("%s has been disconnected.") % self.name,
            title=self.name, kind="warning")

    # ------------------------------------------------------------------
    # Default persona (the built-in "Aidoo", adopts the legacy bot partner)
    # ------------------------------------------------------------------
    @api.model
    def _ensure_default_persona(self):
        """Return the built-in Aidoo persona, creating it (and adopting the
        historical ``partner_aidoo_bot`` identity) on first use.

        Called from the post-init hook (install/upgrade), never from a hot path.
        Resolves the xmlid via a direct ``ir.model.data`` search rather than
        ``env.ref``: a raw ``ir.model.data`` create does not invalidate the ormcache
        behind ``env.ref``, so ``env.ref`` could keep reporting the record missing
        and trigger a duplicate-xmlid insert. ``_update_xmlids`` registers the xmlid
        cache-safely."""
        IMD = self.env["ir.model.data"].sudo()
        data = IMD.search(
            [("module", "=", "aidoo"), ("name", "=", "persona_default")], limit=1)
        if data:
            persona = self.sudo().browse(data.res_id).exists()
            if persona:
                return persona
            data.unlink()  # dangling xmlid (record was deleted) — recreate below
        Session = self.env["aidoo.session"].sudo()
        partner = Session._aidoo_bot_partner()
        # Reuse (or adopt) a service user bound to the legacy bot partner so
        # existing @aidoo mentions / DMs keep working.
        bot_user = self.env["res.users"].sudo().with_context(
            active_test=False).search([("partner_id", "=", partner.id)], limit=1)
        if not bot_user:
            bot_user = self._provision_bot_user("Aidoo", partner=partner)
        persona = self.sudo().create({
            "name": partner.name or "Aidoo",
            "is_default": True,
            "provider": Session._default_provider(),
            "bot_user_id": bot_user.id,
        })
        IMD._update_xmlids([{
            "xml_id": "aidoo.persona_default",
            "record": persona,
            "noupdate": True,
        }])
        return persona

    @api.model
    def _active_personas(self):
        """All active personas that have a Discuss partner. Read-only: the default
        persona is seeded by the post-init hook, never created here (this runs on
        every inbound Discuss message)."""
        return self.sudo().search([("partner_id", "!=", False)])
