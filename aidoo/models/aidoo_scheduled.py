# -*- coding: utf-8 -*-
# Copyright 2026 CICDoo (https://cicdoo.com)
# SPDX-License-Identifier: LGPL-3.0-or-later OR LicenseRef-Aidoo-Commercial
# Dual-licensed: open source (LGPL-3, see LICENSE) or commercial (see COMMERCIAL_LICENSE.md).
"""Scheduled AI agents.

A saved prompt + provider + schedule that the master cron runs autonomously. Each
run spins up a normal ``aidoo.session`` owned by the agent's user (so it executes
with that user's credentials, tools and permissions, exactly like a chat turn) and
calls ``send_message``. When the turn finishes, the runner's terminal hook calls
``aidoo.session._deliver_result`` which dispatches back here to deliver the answer.
"""
import logging
import uuid

from dateutil.relativedelta import relativedelta

from odoo import api, fields, models, _
from odoo.tools import plaintext2html

from .aidoo_session import PROVIDERS

_logger = logging.getLogger(__name__)


class AidooScheduled(models.Model):
    _name = "aidoo.scheduled"
    _description = "Aidoo Scheduled Agent"
    _order = "name"

    name = fields.Char(required=True)
    active = fields.Boolean(default=True)
    user_id = fields.Many2one(
        "res.users", string="Run As", required=True, ondelete="cascade",
        default=lambda self: self.env.user,
        help="The agent runs with this user's AI credentials, tool grant and "
             "Odoo permissions — exactly as if they had typed the prompt.")
    provider = fields.Selection(
        PROVIDERS, string="AI Provider", required=True,
        default=lambda self: self.env["aidoo.session"]._default_provider())
    prompt = fields.Text(required=True)

    interval_number = fields.Integer(default=1, required=True)
    interval_type = fields.Selection(
        [("minutes", "Minutes"), ("hours", "Hours"), ("days", "Days"),
         ("weeks", "Weeks"), ("months", "Months")],
        string="Interval Unit", default="days", required=True)
    next_run = fields.Datetime(
        string="Next Run", default=lambda self: fields.Datetime.now(), copy=False)
    last_run = fields.Datetime(string="Last Run", readonly=True, copy=False)

    context_mode = fields.Selection(
        [("none", "No prior context (isolated)"),
         ("messages", "Last N messages"),
         ("runs", "Last N runs"),
         ("full", "Full history")],
        string="Carry Context", default="none", required=True,
        help="How much of the previous runs' conversation to feed into each run. "
             "'No prior context' runs each turn isolated (the default). The bounded "
             "modes keep token cost in check; 'Full history' grows over time.")
    context_amount = fields.Integer(
        string="Context Size (N)", default=3,
        help="How many messages (or runs) of prior context to include — used by "
             "the 'Last N messages' and 'Last N runs' modes.")

    delivery = fields.Selection(
        [("chat", "Chat only"), ("email", "Email"), ("chatter", "Record chatter")],
        default="chat", required=True,
        help="Where to deliver the agent's answer. 'Chat only' just leaves it as a "
             "conversation in the Aidoo app.")
    email_to = fields.Char(
        string="Email To",
        help="Recipient for 'Email' delivery. Defaults to the run-as user's email.")
    res_model = fields.Char(string="Target Model")
    res_id = fields.Integer(string="Target Record ID")

    session_ids = fields.One2many(
        "aidoo.session", "scheduled_id", string="Runs", readonly=True)
    run_count = fields.Integer(compute="_compute_run_count")

    def _compute_run_count(self):
        for rec in self:
            rec.run_count = len(rec.session_ids)

    # ------------------------------------------------------------------
    # Scheduling
    # ------------------------------------------------------------------
    def _interval_delta(self):
        self.ensure_one()
        return relativedelta(**{self.interval_type: max(self.interval_number, 1)})

    @api.model
    def _cron_run_due(self):
        """Launch every active agent whose ``next_run`` is due, respecting the
        global concurrency cap (leaving the rest for the next cron tick)."""
        now = fields.Datetime.now()
        due = self.search([("next_run", "<=", now)])
        max_runs = int(self.env["aidoo.session"]._config("max_concurrent_runs", 0) or 0)
        Session = self.env["aidoo.session"].sudo()
        for agent in due:
            if max_runs > 0 and Session.search_count(
                    [("state", "=", "running")]) >= max_runs:
                break  # busy — try again next tick, leave next_run untouched
            try:
                agent._run_once()
            except Exception:  # noqa: BLE001
                _logger.exception(
                    "aidoo: scheduled agent %s failed to launch", agent.id)
                # Reschedule anyway so one bad run doesn't wedge the agent.
                agent.write({"next_run": now + agent._interval_delta()})
        return True

    def _run_context(self, session):
        """A bounded, labeled transcript of the agent's PRIOR runs to feed into
        this run, sized by ``context_mode`` / ``context_amount``. Returns "" when
        there is nothing to carry (or the agent is in isolated mode).

        Read fresh from ``message_ids`` every run (never from a stored prefix), so
        carried context can never compound across runs. Call this BEFORE writing
        the current run's divider, so it spans only previous runs."""
        self.ensure_one()
        if self.context_mode == "none":
            return ""
        msgs = session.message_ids.filtered(
            lambda m: m.role in ("user", "assistant")).sorted("id")
        if self.context_mode == "messages":
            msgs = msgs[-max(1, self.context_amount):]
        elif self.context_mode == "runs":
            # Runs are delimited by 'divider' messages (one is written at the
            # start of every run after the first). "Last N runs" = messages
            # at/after the Nth-from-last divider.
            divs = session.message_ids.filtered(
                lambda m: m.role == "divider").sorted("id")
            if divs:
                boundary = divs[-min(max(1, self.context_amount), len(divs))].id
                msgs = msgs.filtered(lambda m: m.id >= boundary)
        # "full" -> all user/assistant messages (intentionally unbounded).
        if not msgs:
            return ""
        body = "\n\n".join(
            "%s: %s" % ("User" if m.role == "user" else "Assistant", m.body or "")
            for m in msgs)
        return ("--- CONTEXT FROM PREVIOUS SCHEDULED RUNS ---\n%s\n"
                "--- END CONTEXT (the task below is the current run) ---" % body)

    def _run_once(self):
        """Run one scheduled turn in the agent's single reused conversation.

        The CLI conversation is kept isolated per run (fresh conversation id,
        reset turn counter) so resume-based providers never replay an ever-growing
        transcript; any prior-run context the agent is configured to carry
        (``context_mode``) is fed in as a bounded, non-persisted prompt prefix.
        Reusing one session keeps the chat list tidy instead of spawning a new
        conversation per run."""
        self.ensure_one()
        now = fields.Datetime.now()
        session = self.env["aidoo.session"].sudo().search(
            [("scheduled_id", "=", self.id)], order="id asc", limit=1)
        if not session:
            session = self.env["aidoo.session"].with_user(self.user_id).create({
                "user_id": self.user_id.id,
                "provider": self.provider,
                "scheduled_id": self.id,
                "name": self.name,
            })
        # A previous run may still be in flight (cron tick < run time): leave it
        # alone and retry next tick rather than stacking onto a running session.
        if session.state == "running":
            self.write({"next_run": now + self._interval_delta()})
            return session
        session = session.with_user(self.user_id)
        # Build the prior-run context BEFORE writing this run's divider, so it
        # spans only previous runs (sized by the agent's context_mode). NB: do not
        # name this local ``context`` — Odoo's ``_()`` lang lookup introspects the
        # frame for a ``context`` dict, and a string here would break translation.
        run_context = self._run_context(session)
        # Visible run boundary between this run and the previous one.
        if session.message_ids:
            self.env["aidoo.message"].create({
                "session_id": session.id,
                "role": "divider",
                "body": _("Scheduled run — %s") % fields.Datetime.to_string(now),
            })
        # Per-run isolation: a new CLI conversation id and a reset turn counter so
        # the turn is treated as the first one — no prior-run transcript or
        # replayed history reaches the model. Any context the agent is configured
        # to carry is fed in below as a bounded, non-persisted prompt prefix.
        session.write({"cli_session_id": str(uuid.uuid4()), "turn_count": 0})
        session.send_message(self.prompt, context_prefix=run_context)
        self.write({"last_run": now, "next_run": now + self._interval_delta()})
        return session

    def action_run_now(self):
        """Run the agent immediately (form-view button)."""
        for agent in self:
            agent._run_once()
        return True

    # ------------------------------------------------------------------
    # Delivery (called from aidoo.session._deliver_result)
    # ------------------------------------------------------------------
    def _deliver(self, session, ok):
        self.ensure_one()
        if not ok or self.delivery == "chat":
            return
        body = session._last_assistant_text()
        if not body:
            return
        html = plaintext2html(body)
        if self.delivery == "email":
            email_to = self.email_to or self.user_id._ai_account_email()
            if not email_to:
                return
            self.env["mail.mail"].sudo().create({
                "subject": _("Aidoo: %s") % self.name,
                "body_html": html,
                "email_to": email_to,
                "auto_delete": True,
            }).send()
        elif self.delivery == "chatter" and self.res_model and self.res_id:
            if self.res_model in self.env:
                rec = self.env[self.res_model].browse(self.res_id).exists()
                if rec:
                    rec.message_post(body=html)
