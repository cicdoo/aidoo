# -*- coding: utf-8 -*-
# Copyright 2026 CICDoo (https://cicdoo.com)
# SPDX-License-Identifier: LGPL-3.0-or-later OR LicenseRef-Aidoo-Commercial
# Dual-licensed: open source (LGPL-3, see LICENSE) or commercial (see COMMERCIAL_LICENSE.md).
"""Change requests: how a persona asks for a change instead of making it.

A persona proposes (``action_propose`` through the ordinary ``orm_action``
tool); one of its principals approves or rejects it in Odoo. Approval applies
the change as the approving person, so their access rights apply, and refuses
when the target moved since the proposal. The agent can never approve: the
decision methods call ``require_human``, and rows are only created or changed
through the methods here (``create``/``write`` refuse anything else).
"""
import json
from datetime import timedelta

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError

from .aidoo_guard import is_tool_call, require_human, tool_session

# Context key that only this model's own methods set. Tool arguments cannot
# reach the context, so orm_create/orm_write on this model are refused.
_INTERNAL = "aidoo_change_request_internal"
EXPIRY_HOURS = 72


def _dump(value):
    return json.dumps(value, default=str, sort_keys=True)


class AidooChangeRequest(models.Model):
    _name = "aidoo.change.request"
    _description = "AI Change Request"
    _order = "id desc"
    _rec_name = "summary"

    summary = fields.Char(required=True, readonly=True)
    reason = fields.Text(readonly=True)
    persona_id = fields.Many2one(
        "aidoo.persona", readonly=True, index=True, ondelete="set null")
    session_id = fields.Many2one(
        "aidoo.session", readonly=True, index=True, ondelete="set null")
    proposer_id = fields.Many2one(
        "res.users", readonly=True, index=True, default=lambda s: s.env.user)
    model = fields.Char(string="Model", required=True, readonly=True)
    operation = fields.Selection(
        [("write", "Update"), ("create", "Create"),
         ("unlink", "Delete"), ("action", "Run action")],
        required=True, readonly=True)
    res_ids = fields.Char(string="Record IDs", readonly=True, default="[]")
    values = fields.Text(readonly=True, default="{}",
                         help="Field values (update/create) or keyword "
                              "arguments (action), as JSON.")
    method = fields.Char(readonly=True)
    before = fields.Text(readonly=True,
                         help="The targeted fields as they were when proposed.")
    state = fields.Selection(
        [("draft", "Waiting for approval"), ("applied", "Applied"),
         ("rejected", "Rejected"), ("stale", "Stale"),
         ("failed", "Failed"), ("expired", "Expired")],
        default="draft", required=True, readonly=True, index=True)
    approved_by_id = fields.Many2one("res.users", string="Decided by",
                                     readonly=True)
    approved_at = fields.Datetime(string="Decided at", readonly=True)
    expires_at = fields.Datetime(readonly=True)
    result = fields.Text(readonly=True)

    # ------------------------------------------------------------------
    # Only this model's own methods create or change rows
    # ------------------------------------------------------------------
    @api.model_create_multi
    def create(self, vals_list):
        if not self.env.context.get(_INTERNAL):
            raise AccessError(_("Change requests are created with action_propose."))
        return super().create(vals_list)

    def write(self, vals):
        if not self.env.context.get(_INTERNAL):
            raise AccessError(_("Change requests change only through approve/reject."))
        return super().write(vals)

    # ------------------------------------------------------------------
    # Propose (the agent's side)
    # ------------------------------------------------------------------
    @api.model
    def action_propose(self, model=None, operation=None, res_ids=None,
                       values=None, method=None, summary=None, reason=None):
        """Record a change for a principal to approve. Applies nothing.

        operation: write | create | unlink | action. ``values`` are the field
        values (write/create) or the action's keyword arguments."""
        if not model or model not in self.env:
            raise ValidationError(_("Unknown model: %s", model))
        if operation not in ("write", "create", "unlink", "action"):
            raise ValidationError(_("operation must be write, create, unlink or action."))
        if not (summary or "").strip():
            raise ValidationError(_("A one-line summary is required."))
        values = values or {}
        if not isinstance(values, dict):
            raise ValidationError(_("values must be an object."))
        ids = [int(i) for i in (res_ids or [])]
        if operation != "create" and not ids:
            raise ValidationError(_("res_ids are required for %s.", operation))
        if operation in ("write", "create") and not values:
            raise ValidationError(_("values are required for %s.", operation))
        if operation == "action":
            self.env["aidoo.tool.exec"]._check_action_method(method, allow_denied=True)
        if is_tool_call(self.env):
            self.env["aidoo.tool.exec"]._check_model(model)

        records = self.env[model].browse(ids)
        before = False
        if ids:
            # The proposer must be able to see what they propose to change.
            records.check_access("read")
            if len(records.exists()) != len(ids):
                raise ValidationError(_("Some of res_ids do not exist."))
            if operation == "write":
                before = _dump(records.read(list(values)))

        session = tool_session(self.env)
        req = self.with_context(**{_INTERNAL: True}).create({
            "summary": summary.strip()[:200],
            "reason": reason or False,
            "persona_id": session.persona_id.id or False,
            "session_id": session.id or False,
            "model": model,
            "operation": operation,
            "res_ids": json.dumps(ids),
            "values": _dump(values),
            "method": method or False,
            "before": before,
            "expires_at": fields.Datetime.now() + timedelta(hours=EXPIRY_HOURS),
        })
        base = (self.env["ir.config_parameter"].sudo()
                .get_param("web.base.url", "") or "").rstrip("/")
        return {
            "id": req.id,
            "state": req.state,
            "url": "%s/odoo/aidoo.change.request/%s" % (base, req.id),
            "note": "Nothing has changed yet. A principal must approve this "
                    "request in Odoo; you cannot approve it.",
        }

    # ------------------------------------------------------------------
    # Decide (the principal's side, in Odoo only)
    # ------------------------------------------------------------------
    def _check_decider(self):
        user = self.env.user
        for req in self:
            persona = req.persona_id.sudo()
            if persona:
                if user not in persona.principal_user_ids:
                    raise AccessError(_(
                        "Only a principal of %s can decide this request.", persona.name))
            elif not user.has_group("aidoo.group_ai_manager"):
                raise AccessError(_(
                    "Only an AI manager can decide a request made without a persona."))

    def _decide(self, vals):
        self.with_context(**{_INTERNAL: True}).write(dict(
            vals, approved_by_id=self.env.uid, approved_at=fields.Datetime.now()))

    def action_approve(self):
        require_human(self.env, _("Approving a change request"))
        self._check_decider()
        for req in self:
            if req.state != "draft":
                raise UserError(_("Only a request waiting for approval can be approved."))
            if req.expires_at and req.expires_at < fields.Datetime.now():
                req._decide({"state": "expired"})
                continue
            req._apply()

    def action_reject(self):
        require_human(self.env, _("Rejecting a change request"))
        self._check_decider()
        for req in self:
            if req.state != "draft":
                raise UserError(_("Only a request waiting for approval can be rejected."))
            req._decide({"state": "rejected"})

    def _apply(self):
        """Apply as the approving person, unless the target moved."""
        self.ensure_one()
        Model = self.env[self.model]
        records = Model.browse(json.loads(self.res_ids or "[]"))
        values = json.loads(self.values or "{}")
        if self.operation != "create" and len(records.exists()) != len(records):
            return self._decide({"state": "stale",
                                 "result": "Some target records no longer exist."})
        if self.operation == "write" and self.before:
            now = _dump(records.sudo().read(list(values)))
            if now != self.before:
                return self._decide({"state": "stale", "result": (
                    "The records changed after this was proposed. Ask for a "
                    "fresh proposal.")})
        try:
            with self.env.cr.savepoint():
                if self.operation == "write":
                    records.write(values)
                    result = "Updated %s record(s)." % len(records)
                elif self.operation == "create":
                    result = "Created %s." % Model.create(values).ids
                elif self.operation == "unlink":
                    records.unlink()
                    result = "Deleted %s record(s)." % len(records)
                else:
                    target = records or Model
                    result = _dump(getattr(target, self.method)(**values))[:2000]
        except (AccessError, UserError, ValidationError) as e:
            return self._decide({"state": "failed", "result": str(e)})
        self._decide({"state": "applied", "result": result})
