# -*- coding: utf-8 -*-
# Copyright 2026 CICDoo (https://cicdoo.com)
# SPDX-License-Identifier: LGPL-3.0-or-later OR LicenseRef-Aidoo-Commercial
# Dual-licensed: open source (LGPL-3, see LICENSE) or commercial (see COMMERCIAL_LICENSE.md).
import base64
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import time
import uuid

from markupsafe import Markup, escape

from odoo import api, fields, models, _
from odoo.exceptions import AccessError, MissingError, UserError
from odoo.tools import html2plaintext

_logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Minimal, safe Markdown -> HTML rendering for Discuss replies.
#
# The chat UI renders model markdown client-side; a Discuss channel needs HTML,
# so the assistant's reply (markdown) must be converted before message_post.
# Everything is HTML-escaped first and only the small set of tags we emit is
# introduced, so model output can never inject markup. No external dependency.
# ---------------------------------------------------------------------------
_MD_LINK = re.compile(r"\[([^\]]+)\]\((https?://[^\s)]+)\)")
_MD_BOLD = re.compile(r"\*\*(.+?)\*\*|__(.+?)__")
_MD_ITALIC = re.compile(
    r"(?<![\*\w])\*(?!\s)(.+?)(?<!\s)\*(?![\*\w])"
    r"|(?<![_\w])_(?!\s)(.+?)(?<!\s)_(?![_\w])")
_MD_INLINE_CODE = re.compile(r"`([^`]+)`")
_MD_MARKER = re.compile(r"^\x00(\d+)\x00$")
_MD_TABLE_SEP = re.compile(r"^\s*\|?\s*:?-{1,}:?\s*(\|\s*:?-{1,}:?\s*)+\|?\s*$")


def _md_row_cells(line):
    line = line.strip()
    if line.startswith("|"):
        line = line[1:]
    if line.endswith("|"):
        line = line[:-1]
    return [c.strip() for c in line.split("|")]


def _md_emphasis(s):
    s = _MD_LINK.sub(
        lambda m: '<a href="%s" target="_blank" rel="noreferrer noopener">%s</a>'
        % (m.group(2), m.group(1)), s)
    s = _MD_BOLD.sub(lambda m: "<strong>%s</strong>" % (m.group(1) or m.group(2)), s)
    s = _MD_ITALIC.sub(lambda m: "<em>%s</em>" % (m.group(1) or m.group(2)), s)
    return s


def _md_inline(s):
    parts, last = [], 0
    for m in _MD_INLINE_CODE.finditer(s):
        parts.append(_md_emphasis(s[last:m.start()]))
        parts.append("<code>%s</code>" % m.group(1))
        last = m.end()
    parts.append(_md_emphasis(s[last:]))
    return "".join(parts)


def aidoo_md_to_html(text):
    """Render a subset of Markdown (headings, bold/italic, links, inline and
    fenced code, ordered/unordered lists, GFM tables) to sanitized HTML for
    Discuss. Returns ``Markup`` so ``message_post`` renders it (a plain str would
    be re-escaped and shown as literal tags)."""
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    blocks = []

    def _stash(m):
        blocks.append("<pre><code>%s</code></pre>" % escape(m.group(1)))
        return "\n\x00%d\x00\n" % (len(blocks) - 1)

    text = re.sub(r"```[ \t]*[\w+.-]*\n(.*?)```", _stash, text, flags=re.DOTALL)
    esc = str(escape(text))

    out, para, list_stack = [], [], []

    def close_lists():
        while list_stack:
            out.append("</%s>" % list_stack.pop())

    def flush_para():
        if para:
            out.append("<p>%s</p>" % "<br>".join(_md_inline(x) for x in para))
            para.clear()

    lines = esc.split("\n")
    i, n = 0, len(lines)
    while i < n:
        line = lines[i]
        stripped = line.strip()
        mk = _MD_MARKER.match(stripped)
        if mk:
            flush_para()
            close_lists()
            out.append(blocks[int(mk.group(1))])
            i += 1
            continue
        # GFM table: a header row of pipes followed by a |---|---| separator row.
        if "|" in stripped and i + 1 < n and _MD_TABLE_SEP.match(lines[i + 1]):
            flush_para()
            close_lists()
            header = _md_row_cells(stripped)
            out.append("<table><thead><tr>")
            out.extend("<th>%s</th>" % _md_inline(c) for c in header)
            out.append("</tr></thead><tbody>")
            i += 2
            while i < n and lines[i].strip() and "|" in lines[i]:
                cells = _md_row_cells(lines[i])
                cells += [""] * (len(header) - len(cells))
                out.append("<tr>%s</tr>" % "".join(
                    "<td>%s</td>" % _md_inline(c) for c in cells[:len(header)]))
                i += 1
            out.append("</tbody></table>")
            continue
        if not stripped:
            flush_para()
            close_lists()
            i += 1
            continue
        h = re.match(r"(#{1,6})\s+(.*)$", line)
        if h:
            flush_para()
            close_lists()
            lvl = min(6, max(3, len(h.group(1)) + 2))  # cap: '# ' -> h3
            out.append("<h%d>%s</h%d>" % (lvl, _md_inline(h.group(2)), lvl))
            i += 1
            continue
        ul = re.match(r"[ \t]*[-*+]\s+(.*)$", line)
        ol = re.match(r"[ \t]*\d+[.)]\s+(.*)$", line)
        if ul or ol:
            flush_para()
            want = "ul" if ul else "ol"
            if not list_stack or list_stack[-1] != want:
                close_lists()
                out.append("<%s>" % want)
                list_stack.append(want)
            out.append("<li>%s</li>" % _md_inline((ul or ol).group(1)))
            i += 1
            continue
        close_lists()
        para.append(stripped)
        i += 1
    flush_para()
    close_lists()
    return Markup("".join(out) or "<p></p>")


# Chat history paging: the OWL client loads the most recent AIDOO_MSG_PAGE
# messages first and fetches older ones on scroll-up (capped per request).
AIDOO_MSG_PAGE = 30
AIDOO_MSG_PAGE_MAX = 200

# Providers selectable per conversation. Each maps to an aidoo.provider.<code>
# adapter AbstractModel that owns all provider-specific behavior.
PROVIDERS = [
    ("claude", "Claude"),
    ("codex", "Codex"),
    ("gemini", "Gemini"),
    ("grok", "Grok"),
    ("zai", "Z.AI (GLM)"),
    ("antigravity", "Antigravity"),
]

# Tools the model is allowed to call (Phase 1 = read-only). Kept in sync with the
# MCP forwarder's advertised tools and the CLI --allowedTools list.
READ_TOOLS = [
    "orm_search_read",
    "orm_read",
    "orm_call",
    "model_introspect",
    "sql_select",
]

# Mutating tools (Phase 2). Each is individually grantable per user and is
# stripped entirely when zero-trust mode is on.
WRITE_TOOLS = [
    "orm_create",
    "orm_write",
    "orm_unlink",
    "orm_action",
    "run_wizard",
    "run_server_action",
]

# Built-in web tools (the CLI's native WebFetch/WebSearch / web search). Unlike
# the other tools these are NOT served by the mcp__odoo__* bridge — they are the
# CLI's own built-ins, normally hard-denied. They are opt-in per user (granted
# via res.users.aidoo_tool_ids) and deliberately kept OUT of READ_TOOLS so an
# empty selection never auto-grants web access.
WEB_TOOLS = ["web_fetch", "web_search"]
WEB_TOOL_BUILTINS = {"web_fetch": "WebFetch", "web_search": "WebSearch"}

ALL_TOOLS = READ_TOOLS + WRITE_TOOLS + WEB_TOOLS
# sql_select is read-only (SELECT-only), so it survives zero-trust mode. Web
# tools don't mutate Odoo data, so they survive zero-trust too.
READONLY_TOOL_SET = set(READ_TOOLS) | set(WEB_TOOLS)

# Default fnmatch patterns for methods orm_action/run_wizard may invoke.
DEFAULT_ACTION_METHOD_PATTERNS = ["action_*", "button_*"]

# Hard floor: every CLI built-in tool is denied so the model can ONLY act through
# our mcp__odoo__* tools (Claude enforces this via settings.json; other CLIs via
# their own allow/exclude mechanisms). Only list tool names a CLI actually knows.
DISALLOWED_BUILTINS = [
    "Bash", "Write", "Edit", "NotebookEdit", "Read",
    "Glob", "Grep", "WebFetch", "WebSearch",
    "Task", "TaskCreate", "TaskGet", "TaskList", "TaskOutput", "TaskStop",
    "TaskUpdate", "Skill", "Workflow", "ToolSearch", "AskUserQuestion",
    "EnterPlanMode", "ExitPlanMode", "EnterWorktree", "ExitWorktree",
    "Monitor", "PushNotification", "ScheduleWakeup", "RemoteTrigger",
    "CronCreate", "CronDelete", "CronList", "BashOutput", "KillShell",
    "TodoWrite", "ListMcpResources", "ReadMcpResource",
]

# --- File upload support -------------------------------------------------
UPLOAD_TEXT_EXTS = {
    ".txt", ".csv", ".tsv", ".json", ".xml", ".md", ".markdown", ".rst",
    ".log", ".yaml", ".yml", ".ini", ".conf", ".cfg", ".toml", ".env",
    ".py", ".js", ".ts", ".jsx", ".tsx", ".html", ".htm", ".css", ".scss",
    ".sql", ".sh", ".c", ".h", ".cpp", ".hpp", ".java", ".go", ".rb", ".php",
}
UPLOAD_PDF_EXTS = {".pdf"}
UPLOAD_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
ALLOWED_UPLOAD_EXTS = UPLOAD_TEXT_EXTS | UPLOAD_PDF_EXTS | UPLOAD_IMAGE_EXTS

# Subdirectory of the scratch dir that holds uploaded files. The Read tool is
# allowed ONLY under this path — never the scratch root (which holds the bridge
# token + settings).
UPLOADS_SUBDIR = "uploads"

DEFAULT_MAX_UPLOAD_MB = 15

# Bounded history replayed into the prompt for providers that cannot resume a
# server-side transcript (Gemini / Grok).
HISTORY_MAX_MESSAGES = 12
HISTORY_MAX_CHARS = 8000


class AiAssistantSession(models.Model):
    _name = "aidoo.session"
    _description = "Aidoo AI Assistant Conversation Session"
    _order = "write_date desc, id desc"

    name = fields.Char(default=lambda self: _("New conversation"), required=True)
    user_id = fields.Many2one(
        "res.users", string="User", required=True, ondelete="cascade",
        default=lambda self: self.env.user, index=True,
    )
    provider = fields.Selection(
        PROVIDERS, string="AI Provider", required=True, copy=True, index=True,
        default=lambda self: self._default_provider(),
        help="Which AI assistant drives this conversation.",
    )
    # UUID we generate, used to name the scratch dir (same for every provider).
    scratch_uuid = fields.Char(copy=False, index=True)
    # The provider's own conversation id used for resume. For Claude this is the
    # UUID we mint (--session-id/--resume); for Codex/Gemini/Grok it is captured
    # from the CLI's stream on the first turn.
    cli_session_id = fields.Char(
        string="CLI Session Id", copy=False, index=True,
        help="Provider conversation id passed to the CLI for continuity.")
    state = fields.Selection(
        [("idle", "Idle"), ("running", "Running"),
         ("done", "Done"), ("error", "Error")],
        default="idle", required=True, copy=False,
    )
    turn_count = fields.Integer(default=0, copy=False)
    is_favorite = fields.Boolean(
        string="Favorite", default=False, copy=False, index=True)
    # Optional reusable instruction preset applied to every turn (its text is
    # injected into the identity block — see _ai_identity_prompt).
    skill_id = fields.Many2one(
        "aidoo.skill", string="Skill", ondelete="set null", copy=False)
    working_dir = fields.Char(copy=False)
    last_run_pid = fields.Integer(copy=False)
    last_error = fields.Text(copy=False)

    # Optional record this conversation is focused on (set when opened from a
    # form view via "Ask Aidoo"). Used to seed the first prompt and the UI chip.
    context_model = fields.Char(copy=False)
    context_res_id = fields.Integer(copy=False)
    # For list focus: the effective search domain (JSON) the user has applied,
    # so the assistant can query exactly the records the user is looking at.
    context_domain = fields.Text(copy=False)
    # Richer view context (any multi-record view: list/kanban/pivot/graph/calendar).
    context_view_type = fields.Char(copy=False)             # e.g. "list", "kanban"
    context_groupby = fields.Text(copy=False)               # JSON list of field names
    context_filters = fields.Text(copy=False)               # human-readable facet summary
    context_selected_ids = fields.Text(copy=False)          # JSON list of ticked record ids

    # Set when the session was launched by a scheduled agent (delivery routes
    # back to it when the turn finishes).
    scheduled_id = fields.Many2one(
        "aidoo.scheduled", string="Scheduled Agent", ondelete="set null",
        copy=False, index=True)

    # Set for conversations driven from the Discuss bot — the reply is posted
    # back into this channel when the turn finishes.
    discuss_channel_id = fields.Many2one(
        "discuss.channel", string="Discuss Channel", ondelete="cascade",
        copy=False, index=True)

    # Set when this conversation is driven by an AI persona. The persona supplies
    # the provider account + personality/skills; tools still run as ``user_id``
    # (the invoking human). See ``_credential_user`` / ``_run_identity``.
    persona_id = fields.Many2one(
        "aidoo.persona", string="Persona", ondelete="set null",
        copy=False, index=True)

    message_ids = fields.One2many(
        "aidoo.message", "session_id", string="Messages")

    # ------------------------------------------------------------------
    # Message paging (newest-anchored, for the OWL chat)
    # ------------------------------------------------------------------
    def _messages_page(self, limit=None, before_id=None):
        """Serialize a page of this conversation's messages for the chat client.

        Without ``limit`` the whole conversation is returned (legacy behavior, used
        e.g. by the floating companion). With ``limit`` the page is *newest-anchored*:
        the most recent ``limit`` messages in chronological order, or — when
        ``before_id`` is given — the ``limit`` messages immediately older than that id
        (scroll-up). ``has_more`` tells the client whether still-older messages exist.
        """
        self.ensure_one()
        if not limit:
            return {"messages": self.message_ids._to_frontend(), "has_more": False}
        try:
            limit = int(limit)
        except (TypeError, ValueError):
            limit = AIDOO_MSG_PAGE
        limit = max(1, min(limit, AIDOO_MSG_PAGE_MAX))
        domain = [("session_id", "=", self.id)]
        if before_id:
            domain.append(("id", "<", int(before_id)))
        # Fetch newest-first with one extra row to detect more history, then flip
        # back to chronological order for display.
        recs = self.env["aidoo.message"].search(domain, order="id desc", limit=limit + 1)
        has_more = len(recs) > limit
        recs = recs[:limit].sorted("id")
        return {"messages": recs._to_frontend(), "has_more": has_more}

    # --- Bridge token bookkeeping (only the jti/salt are stored, never the token) ---
    bridge_jti = fields.Char(copy=False, groups="base.group_system")
    bridge_salt = fields.Char(copy=False, groups="base.group_system")
    bridge_exp = fields.Integer(copy=False, groups="base.group_system")

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    @api.model
    def _default_provider(self):
        return self._config("default_provider", "claude")

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            vals.setdefault("scratch_uuid", str(uuid.uuid4()))
            # Only providers that MINT their conversation id client-side (Claude:
            # --session-id) get a pre-generated UUID. Providers that learn their
            # id from the CLI stream (Codex/Gemini/Grok) must start with an EMPTY
            # cli_session_id: a fabricated UUID would make their --resume/-r fire
            # with an id the CLI has never stored, aborting the turn with
            # "No previous sessions found for this project."
            code = vals.get("provider") or self._default_provider()
            model_name = "aidoo.provider.%s" % code
            if model_name in self.env and self.env[model_name]._mints_session_id:
                vals.setdefault("cli_session_id", str(uuid.uuid4()))
        return super().create(vals_list)

    @api.model
    def _prune_empty(self, keep_id=None):
        """Delete the current user's empty conversations (no messages, not
        running, and not tied to a Discuss channel or scheduled agent). Used to
        clean up sessions created eagerly when a chat opens but never used."""
        domain = [
            ("user_id", "=", self.env.uid),
            ("message_ids", "=", False),
            ("state", "!=", "running"),
            ("discuss_channel_id", "=", False),
            ("scheduled_id", "=", False),
        ]
        if keep_id:
            domain.append(("id", "!=", int(keep_id)))
        self.search(domain).unlink()

    def _adapter(self):
        self.ensure_one()
        return self.env["aidoo.provider.%s" % self.provider]

    def _credential_user(self):
        """The user whose on-disk provider credentials this session runs under.

        For a persona-driven session that is the persona's dedicated service
        account (so it uses the persona's own AI provider account); otherwise the
        invoking user. Tool execution still runs as ``user_id`` — this only
        governs which provider credentials are loaded."""
        self.ensure_one()
        if self.persona_id:
            return self.persona_id.bot_user_id
        return self.user_id

    def _run_identity(self, tools):
        """The identity block injected into the prompt for a run.

        Always describes the *invoking* user (``user_id``) so the model acts
        within that user's Odoo rights. When a persona drives the session, its
        personality and skills are appended so the model adopts the persona while
        still being bound by the caller's permissions."""
        self.ensure_one()
        identity = self.user_id._ai_identity_prompt(tools, skill=self.skill_id)
        # Persona text is admin-curated (not secret); read it sudo so an invoking
        # user without record-rule access to a bundled skill still gets it.
        persona = self.persona_id.sudo()
        if not persona:
            return identity
        # Persona FIRST and marked authoritative so the model adopts its identity,
        # voice and rules instead of the generic "you are the user's assistant"
        # framing. The user context that follows is reframed as the interlocutor +
        # permission scope, not the model's own identity.
        blocks = [
            "=== YOUR PERSONA (authoritative — adopt this identity, voice and "
            "instructions as your own; they take precedence over any generic "
            "assistant framing) ===",
            'You are "%s".' % (persona.name or "Aidoo"),
        ]
        if persona.instructions:
            blocks.append(persona.instructions)
        for skill in persona.skill_ids:
            if skill.instructions:
                blocks.append("")
                blocks.append("[Skill: %s]" % (skill.name or ""))
                blocks.append(skill.instructions)
        blocks.append(
            "Stay in character as the persona above. The ODOO USER CONTEXT below "
            "is the person you are assisting and the permission scope for your "
            "tools — it is NOT your own identity.")
        blocks.append("=== END PERSONA ===")
        blocks.append("")
        blocks.append(identity)
        return "\n".join(blocks)

    @api.model
    def _cron_reap_stale(self):
        """Reset sessions stuck in 'running' past the timeout and GC scratch dirs."""
        timeout = int(self._config("timeout_s", 900))
        cutoff = fields.Datetime.now() - __import__("datetime").timedelta(
            seconds=timeout + 120)
        stale = self.search([("state", "=", "running"), ("write_date", "<", cutoff)])
        for session in stale:
            if session.last_run_pid:
                try:
                    os.kill(session.last_run_pid, 9)
                except (ProcessLookupError, PermissionError):
                    pass
            session.write({"state": "error", "last_run_pid": False,
                           "last_error": "Reaped by cron (stale run)."})
        return True

    def action_stop(self):
        """Terminate the running CLI subprocess for this session, if any."""
        self.ensure_one()
        if self.last_run_pid:
            try:
                os.kill(self.last_run_pid, 15)  # SIGTERM
            except (ProcessLookupError, PermissionError):
                pass
        self.write({"state": "idle", "last_run_pid": False})
        return True

    # ------------------------------------------------------------------
    # Scratch working directory (CLI cwd) — NOT under /opt/odoo source
    # ------------------------------------------------------------------
    def _get_scratch_dir(self):
        self.ensure_one()
        root = self._pconfig(self.provider, "scratch_root",
                             "/var/lib/odoo/aidoo_%s_scratch" % self.provider)
        path = os.path.join(root, self.scratch_uuid)
        try:
            os.makedirs(path, mode=0o700, exist_ok=True)
        except OSError as e:
            raise UserError(_("Cannot create scratch dir %s: %s") % (path, e))
        if self.working_dir != path:
            self.working_dir = path
        return path

    def _uploads_dir(self, create=True):
        """Directory (under scratch) holding this session's uploaded files."""
        self.ensure_one()
        path = os.path.join(self._get_scratch_dir(), UPLOADS_SUBDIR)
        if create:
            try:
                os.makedirs(path, mode=0o700, exist_ok=True)
            except OSError as e:
                raise UserError(_("Cannot create uploads dir %s: %s") % (path, e))
        return path

    # ------------------------------------------------------------------
    # File uploads
    # ------------------------------------------------------------------
    @api.model
    def _max_upload_bytes(self):
        return int(self._config("max_upload_mb", DEFAULT_MAX_UPLOAD_MB)) * 1024 * 1024

    @staticmethod
    def _safe_upload_name(filename):
        """A traversal-safe basename, restricted to the allowed extensions."""
        base = os.path.basename(filename or "").strip().replace("\x00", "")
        base = re.sub(r"[^A-Za-z0-9._-]+", "_", base).lstrip(".") or "file"
        ext = os.path.splitext(base)[1].lower()
        if ext not in ALLOWED_UPLOAD_EXTS:
            raise UserError(_(
                "File type '%s' is not supported. You can upload text/CSV/code, "
                "PDF and image (PNG/JPG/GIF/WebP) files.") % (ext or base))
        return base

    def _store_upload(self, filename, content):
        """Validate and persist one uploaded file as an ir.attachment + on disk."""
        self.ensure_one()
        safe = self._safe_upload_name(filename)
        if not content:
            raise UserError(_("Uploaded file '%s' is empty.") % safe)
        max_bytes = self._max_upload_bytes()
        if len(content) > max_bytes:
            raise UserError(_(
                "File '%s' is too large (limit %s MB).")
                % (safe, max_bytes // (1024 * 1024)))
        att = self.env["ir.attachment"].create({
            "name": safe,
            "raw": content,
            "res_model": self._name,
            "res_id": self.id,
        })
        self._write_attachment_to_disk(att)
        return self._attachment_descriptor(att)

    def _attachment_descriptor(self, att):
        return {"id": att.id, "name": att.name, "mimetype": att.mimetype or ""}

    def _write_attachment_to_disk(self, att):
        """Materialize one attachment into the uploads dir; return its abs path."""
        self.ensure_one()
        disk_name = "%s_%s" % (att.id, self._safe_upload_name(att.name))
        path = os.path.join(self._uploads_dir(), disk_name)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(att.raw or b"")
        return path

    def _materialize_attachments(self, attachment_ids):
        """Ensure the given (own) attachments exist on disk."""
        self.ensure_one()
        if not attachment_ids:
            return [], self.env["ir.attachment"]
        atts = self.env["ir.attachment"].browse(attachment_ids).exists().filtered(
            lambda a: a.res_model == self._name and a.res_id == self.id)
        paths = [self._write_attachment_to_disk(a) for a in atts]
        return paths, atts

    # ------------------------------------------------------------------
    # Configuration helpers
    # ------------------------------------------------------------------
    @api.model
    def _config(self, key, default=None):
        """Read a shared system parameter ``aidoo.<key>``."""
        val = self.env["ir.config_parameter"].sudo().get_param("aidoo.%s" % key)
        return val if val not in (None, False, "") else default

    @api.model
    def _pconfig(self, provider, key, default=None):
        """Read a provider-scoped parameter ``aidoo.<provider>.<key>``, falling
        back to the shared ``aidoo.<key>``, then ``default``."""
        ICP = self.env["ir.config_parameter"].sudo()
        val = ICP.get_param("aidoo.%s.%s" % (provider, key))
        if val not in (None, False, ""):
            return val
        return self._config(key, default)

    # ------------------------------------------------------------------
    # Tool / model gating — single server-side source of truth.
    # ------------------------------------------------------------------
    def _effective_tools(self):
        self.ensure_one()
        return self.user_id.sudo()._ai_effective_tools()

    @api.model
    def _ai_excluded_models(self):
        raw = self._config("excluded_models", "") or ""
        return {m.strip() for m in raw.split(",") if m.strip()}

    @api.model
    def _ai_action_method_patterns(self):
        raw = self._config("action_methods", "") or ""
        pats = [p.strip() for p in raw.split(",") if p.strip()]
        return pats or DEFAULT_ACTION_METHOD_PATTERNS

    @api.model
    def _ai_allowed_server_action_ids(self):
        raw = self._config("server_action_ids", "") or ""
        return {int(x) for x in raw.split(",") if x.strip().isdigit()}

    def _target_db(self):
        """The database the bridge should connect back to."""
        return self._config("db_name") or self.env.cr.dbname

    def _mint_routing_sid(self, db):
        """Create a short anonymous Odoo session pinned to ``db`` and return its
        sid, so the bridge's loopback calls resolve to this exact database."""
        from odoo.http import root
        try:
            store = root.session_store
            sess = store.new()
            sess.db = db
            store.save(sess)
            return sess.sid
        except Exception:  # noqa: BLE001
            _logger.exception("AI assistant: could not mint routing session")
            return ""

    @api.model
    def _delete_routing_sid(self, sid):
        if not sid:
            return
        from odoo.http import root
        try:
            store = root.session_store
            store.delete(store.get(sid))
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------------
    # Bridge token: a short-lived, session-scoped HMAC capability.
    # ------------------------------------------------------------------
    def _signing_secret(self):
        self.ensure_one()
        db_secret = self.env["ir.config_parameter"].sudo().get_param(
            "database.secret") or ""
        salt = self.sudo().bridge_salt or ""
        return (db_secret + "|" + salt).encode()

    def _mint_bridge_token(self, ttl_seconds=900):
        self.ensure_one()
        salt = secrets.token_urlsafe(24)
        jti = secrets.token_urlsafe(18)
        exp = int(time.time()) + ttl_seconds
        self.sudo().write({"bridge_salt": salt, "bridge_jti": jti, "bridge_exp": exp})
        payload = {
            "sid": self.id, "uid": self.user_id.id, "db": self._target_db(),
            "scope": "ai_bridge", "jti": jti, "exp": exp,
        }
        body = base64.urlsafe_b64encode(
            json.dumps(payload, separators=(",", ":")).encode()).rstrip(b"=")
        sig = hmac.new(self._signing_secret(), body, hashlib.sha256).digest()
        sig_b64 = base64.urlsafe_b64encode(sig).rstrip(b"=")
        return (body + b"." + sig_b64).decode()

    @api.model
    def _verify_bridge_token(self, token):
        """Validate a bridge token. Returns the (sudo) session record or None."""
        try:
            body_b64, sig_b64 = token.encode().split(b".")
            padded = body_b64 + b"=" * (-len(body_b64) % 4)
            payload = json.loads(base64.urlsafe_b64decode(padded))
        except Exception:
            return None
        if payload.get("scope") != "ai_bridge":
            return None
        if payload.get("db") != self.env.cr.dbname:
            return None
        if int(payload.get("exp", 0)) < int(time.time()):
            return None
        session = self.sudo().browse(payload.get("sid"))
        if not session.exists():
            return None
        expected = hmac.new(
            session._signing_secret(), body_b64, hashlib.sha256).digest()
        expected_b64 = base64.urlsafe_b64encode(expected).rstrip(b"=")
        if not hmac.compare_digest(sig_b64, expected_b64):
            return None
        if not session.bridge_jti or session.bridge_jti != payload.get("jti"):
            return None
        if int(session.bridge_exp or 0) < int(time.time()):
            return None
        return session

    # ------------------------------------------------------------------
    # Prompt assembly
    # ------------------------------------------------------------------
    def _context_label(self):
        """Human-readable label for the focused context, under the user's own
        rights: the record's name on a form, else the model's name on a list.
        Empty string if there is none or it is not readable."""
        self.ensure_one()
        if not self.context_model or self.context_model not in self.env:
            return ""
        model_label = self.env["ir.model"]._get(self.context_model).name \
            or self.context_model
        if not self.context_res_id:
            # list/model-level focus — show the selection count when there is one.
            n = len(self._context_selected_ids())
            return _("%(m)s · %(n)s selected") % {"m": model_label, "n": n} \
                if n else model_label
        try:
            rec = self.env[self.context_model].browse(self.context_res_id)
            if not rec.exists():
                return ""
            return rec.display_name or model_label
        except (AccessError, MissingError):
            return ""

    def _context_note(self):
        """A note telling the model which Odoo record/model the user is looking
        at. Only emitted on the first turn of a session opened with a focus
        context, and only when the user may actually read it."""
        self.ensure_one()
        if self.turn_count != 0 or not self.context_model:
            return ""
        label = self._context_label()
        if not label:
            return ""
        if self.context_res_id:
            return _(
                "The user is currently viewing the Odoo record %(model)s,%(rid)s "
                "(\"%(label)s\"). Treat it as the focus of this conversation; use "
                "the ORM tools to read it before answering."
            ) % {"model": self.context_model, "rid": self.context_res_id,
                 "label": label}
        view = (self.context_view_type or "list").strip() or "list"
        parts = [_(
            "The user is currently looking at the \"%(label)s\" %(view)s view "
            "(model %(model)s)."
        ) % {"label": label, "view": view, "model": self.context_model}]

        selected = self._context_selected_ids()
        if selected:
            parts.append(_(
                "They have selected %(n)s specific record(s): ids %(ids)s. Treat "
                "these as the focus — read them with orm_read before answering."
            ) % {"n": len(selected), "ids": selected})

        if self.context_domain:
            parts.append(_(
                "The view is filtered to the records matching this Odoo domain: "
                "%s. Use orm_search_read with that domain to see exactly those "
                "records."
            ) % self.context_domain)

        groupby = self._context_groupby()
        if groupby:
            parts.append(_("It is grouped by: %s.") % ", ".join(groupby))

        if self.context_filters:
            parts.append(_("Active search filters: %s.") % self.context_filters)

        if not selected and not self.context_domain:
            parts.append(_(
                "Treat this model as the focus of the conversation; use the ORM "
                "tools to query its records before answering."))
        return " ".join(parts)

    def _context_selected_ids(self):
        """The ticked record ids stored with this conversation (or [])."""
        self.ensure_one()
        try:
            ids = json.loads(self.context_selected_ids or "[]")
            return [int(i) for i in ids] if isinstance(ids, list) else []
        except (ValueError, TypeError):
            return []

    def _context_groupby(self):
        """The group-by field names stored with this conversation (or [])."""
        self.ensure_one()
        try:
            gb = json.loads(self.context_groupby or "[]")
            return [str(g) for g in gb] if isinstance(gb, list) else []
        except (ValueError, TypeError):
            return []

    def _compose_prompt(self, body, paths):
        """Compose the prompt text: the user's body plus, when relevant, a
        focus-record note and an attachment manifest the model can act on."""
        parts = []
        note = self._context_note()
        if note:
            parts.append(note)
        if body:
            parts.append(body)
        if paths:
            listing = "\n".join("- %s" % p for p in paths)
            parts.append(_(
                "The user attached the following file(s), saved in your working "
                "directory. Use the Read tool to open them — text/CSV/code is read "
                "verbatim, images are viewed visually, and PDFs page-by-page:\n%s"
            ) % listing)
        return "\n\n".join(parts)

    def _history_for_prompt(self):
        """Bounded transcript of this conversation, for providers that cannot
        resume a server-side session and must replay context into the prompt."""
        self.ensure_one()
        msgs = self.message_ids.filtered(lambda m: m.role in ("user", "assistant"))
        msgs = msgs.sorted("id")[-HISTORY_MAX_MESSAGES:]
        if not msgs:
            return ""
        text = "\n\n".join(
            "%s: %s" % (("User" if m.role == "user" else "Assistant"), m.body or "")
            for m in msgs)
        if len(text) > HISTORY_MAX_CHARS:
            text = "…" + text[-HISTORY_MAX_CHARS:]
        return text

    # ------------------------------------------------------------------
    # Public entry point (called from the controller)
    # ------------------------------------------------------------------
    def send_message(self, body, attachment_ids=None, context_prefix=None):
        """Persist the user message and launch a CLI turn in the background.

        ``context_prefix`` (optional) is prepended to the prompt handed to the
        model but is NOT stored in the user message — used by scheduled agents to
        carry a bounded slice of prior-run context without polluting the chat
        transcript or compounding into the next run's context."""
        self.ensure_one()
        body = (body or "").strip()
        if self.state == "running":
            raise UserError(_("The assistant is still working on the previous message."))
        max_runs = int(self._config("max_concurrent_runs", 0) or 0)
        if max_runs > 0:
            active = self.search_count([("state", "=", "running")])
            if active >= max_runs:
                raise UserError(_(
                    "The AI assistant is busy right now (%(active)s of %(max)s runs "
                    "in progress). Please wait a moment and send your message again.",
                    active=active, max=max_runs))
        adapter = self._adapter()
        if not adapter._is_authenticated(self._credential_user().sudo()):
            if self.persona_id:
                raise UserError(_(
                    "The persona '%(persona)s' has no connected %(provider)s "
                    "account. Ask an administrator to connect it.",
                    persona=self.persona_id.name,
                    provider=adapter._provider_label))
            raise UserError(_(
                "You need to connect your %s account before chatting.")
                % adapter._provider_label)

        paths, atts = self._materialize_attachments(attachment_ids)
        if not body and not atts:
            raise UserError(_("Empty message."))

        user_msg = self.env["aidoo.message"].create({
            "session_id": self.id,
            "role": "user",
            "body": body,
            "attachments": [self._attachment_descriptor(a) for a in atts] or False,
        })
        if self.turn_count == 0 and self.name in (_("New conversation"), False):
            title = body or (atts[:1].name or "")
            self.name = (title[:60] + ("…" if len(title) > 60 else "")) or self.name

        is_first = self.turn_count == 0
        # Persona-aware identity so history-fold providers (which embed identity
        # in the prompt here) also adopt the persona; Claude/zai read the same
        # block from ctx["identity"] in the runner.
        identity = self._run_identity(set(self._effective_tools()))
        prompt = adapter._build_prompt(self, body, paths, is_first, identity)
        if context_prefix:
            # Prepended to the model prompt only (never the stored message body).
            prompt = context_prefix + "\n\n" + prompt
        self.write({"state": "running"})
        token = self._mint_bridge_token()
        self.env["aidoo.runner"]._launch(self, prompt, is_first, token)
        return {"message_id": user_msg.id, "session_id": self.id}

    def _post_report(self, html):
        """Create a ``report``-role message holding raw report HTML and push it
        to the live chat."""
        self.ensure_one()
        rec = self.env["aidoo.message"].create({
            "session_id": self.id, "role": "report", "body": html or ""})
        self.env["aidoo.runner"]._emit_message(self, rec)
        return rec

    # ------------------------------------------------------------------
    # Post-run delivery hook (called by the runner at terminal state)
    # ------------------------------------------------------------------
    def _last_assistant_text(self):
        """The body of the most recent non-empty assistant message, if any."""
        self.ensure_one()
        msgs = self.message_ids.filtered(
            lambda m: m.role == "assistant" and m.body)
        return msgs.sorted("id")[-1].body if msgs else ""

    def _deliver_result(self, ok):
        """Called by the runner once a turn reaches a terminal state.

        Plain chat sessions need nothing — they already streamed to the bus.
        Non-chat origins (scheduled agents, the Discuss bot) extend this to
        deliver the result elsewhere. ``ok`` is True on a clean (rc == 0) run."""
        self.ensure_one()
        if self.scheduled_id:
            self.scheduled_id._deliver(self, ok)
        if self.discuss_channel_id:
            self._deliver_discuss(ok)

    # ------------------------------------------------------------------
    # Discuss bot
    # ------------------------------------------------------------------
    @api.model
    def _aidoo_bot_partner(self):
        """The Discuss bot partner, created on first use (ORM create applies the
        required res.partner defaults the XML data loader would miss)."""
        partner = self.env.ref("aidoo.partner_aidoo_bot", raise_if_not_found=False)
        if partner:
            return partner
        partner = self.env["res.partner"].sudo().create({
            "name": "Aidoo",
            "email": "aidoo@example.com",
        })
        self.env["ir.model.data"].sudo().create({
            "name": "partner_aidoo_bot",
            "module": "aidoo",
            "model": "res.partner",
            "res_id": partner.id,
            "noupdate": True,
        })
        return partner

    def _bot_partner(self):
        """The partner this session posts replies as: the persona's identity when
        persona-driven, else the legacy default Aidoo bot."""
        self.ensure_one()
        if self.persona_id and self.persona_id.partner_id:
            return self.persona_id.partner_id
        return self._aidoo_bot_partner()

    def _post_bot_message(self, body):
        """Post ``body`` (Markdown from the model, or a plain system notice) into
        this session's Discuss channel as the persona (or Aidoo bot), rendered to
        HTML so it is formatted. message_post fires the bus notification itself."""
        self.ensure_one()
        bot = self._bot_partner()
        if not bot or not self.discuss_channel_id:
            return
        self.discuss_channel_id.sudo().message_post(
            author_id=bot.id,
            body=aidoo_md_to_html(body or ""),
            message_type="comment",
            subtype_xmlid="mail.mt_comment",
            silent=True,
        )

    def _discuss_set_typing(self, is_typing):
        """Show/clear the persona's "is typing…" indicator in its Discuss channel
        so users get live feedback while the turn runs in the background. No-op for
        non-Discuss sessions; never raises (best-effort UI feedback)."""
        self.ensure_one()
        if not self.discuss_channel_id:
            return
        bot = self._bot_partner()
        if not bot:
            return
        try:
            member = self.env["discuss.channel.member"].sudo().search([
                ("channel_id", "=", self.discuss_channel_id.id),
                ("partner_id", "=", bot.id),
            ], limit=1)
            if member:
                member._notify_typing(bool(is_typing))
        except Exception:  # noqa: BLE001
            _logger.exception("aidoo: typing indicator failed")

    def _deliver_discuss(self, ok):
        self.ensure_one()
        self._discuss_set_typing(False)  # stop the typing indicator
        body = self._last_assistant_text()
        if not body:
            body = self.last_error if not ok else _("(no response)")
        self._post_bot_message(body)

    @staticmethod
    def _normalize_partner_ids(vals):
        """Return a set of partner ids from a ``partner_ids`` value that may be a
        plain list of ids or a list of ORM command tuples ``(cmd, id, ...)``."""
        ids = set()
        for v in vals or []:
            if isinstance(v, (list, tuple)):
                if len(v) >= 2 and isinstance(v[1], int):
                    ids.add(v[1])
            elif isinstance(v, int):
                ids.add(v)
        return ids

    def _persona_mention_names(self, persona):
        """The @handles that address ``persona``: its name, its partner name, and
        the legacy ``aidoo`` handle for the default persona."""
        names = []
        if persona.name:
            names.append(persona.name)
        pname = persona.partner_id.name
        if pname and pname not in names:
            names.append(pname)
        if persona.is_default and "aidoo" not in [n.lower() for n in names]:
            names.append("aidoo")
        return names

    def _discuss_body_mentions(self, body_html, names):
        """True when the raw message body textually contains ``@<name>`` for any
        of ``names``. Covers the auto-join path, where the persona is not yet a
        channel member so Odoo never resolved the mention into ``partner_ids``."""
        text = html2plaintext(body_html or "").lower()
        if not text:
            return False
        return any(("@" + n).lower() in text for n in names if n)

    def _strip_mentions(self, text, names):
        """Remove ``@<name>`` handles so the model receives a clean prompt."""
        for name in names:
            if name:
                text = re.sub(r"@%s\b" % re.escape(name), "", text,
                              flags=re.IGNORECASE)
        return text.strip()

    @api.model
    def _maybe_handle_discuss(self, channel, msg_values):
        """React to a human message in a Discuss channel by routing it to the
        addressed persona.

        A 1:1 DM with a persona answers every message; a group/public channel
        triggers only when a persona is @mentioned (resolved chip or literal
        ``@name`` text — the persona auto-joins on first mention). The turn runs
        as the message author (their Odoo rights) under the persona's provider
        account; ``_deliver_discuss`` posts the answer back as the persona. Bails
        (the loop guard) on any persona's own posts and on non-comment messages."""
        msg_values = msg_values or {}
        if msg_values.get("message_type") != "comment":
            return
        author_id = msg_values.get("author_id")
        if not author_id:
            return

        personas = self.env["aidoo.persona"]._active_personas()
        persona_partner_ids = set(personas.mapped("partner_id").ids)
        if not persona_partner_ids or author_id in persona_partner_ids:
            return  # no personas, or loop guard on a persona's own post

        author = self.env["res.partner"].browse(author_id)
        user = author.user_ids[:1]
        if not user or not user.has_group("aidoo.group_ai_user"):
            return  # only internal users granted the assistant

        is_dm = channel.channel_type == "chat"
        member_ids = set(channel.channel_partner_ids.ids)
        mentioned_ids = self._normalize_partner_ids(msg_values.get("partner_ids"))
        body_html = msg_values.get("body")

        persona = self.env["aidoo.persona"]
        if is_dm:
            # the persona that is a member of this 1:1 channel
            persona = personas.filtered(
                lambda p: p.partner_id.id in member_ids)[:1]
        else:
            # explicit mention: resolved chip first, then literal @name text
            for p in personas:
                if p.partner_id.id in mentioned_ids or self._discuss_body_mentions(
                        body_html, self._persona_mention_names(p)):
                    persona = p
                    break
        if not persona:
            return  # no persona addressed

        bot = persona.partner_id
        if bot.id not in member_ids:
            channel.sudo().add_members(partner_ids=[bot.id])  # auto-join on mention

        body_text = html2plaintext(body_html or "").strip()
        body_text = self._strip_mentions(
            body_text, self._persona_mention_names(persona))
        if not body_text:
            return

        session = self.with_user(user).search([
            ("discuss_channel_id", "=", channel.id),
            ("persona_id", "=", persona.id),
            ("user_id", "=", user.id),
        ], limit=1)
        if not session:
            session = self.with_user(user).create({
                "user_id": user.id,
                "persona_id": persona.id,
                "provider": persona.provider,
                "discuss_channel_id": channel.id,
                "name": "%s · %s" % (persona.name, channel.name or _("Discuss")),
            })

        adapter = session._adapter()
        if not adapter._is_authenticated(persona.bot_user_id.sudo()):
            session._post_bot_message(_(
                "This persona isn't connected yet. Ask an administrator to "
                "connect its %s account.") % adapter._provider_label)
            return
        prefix = _("The user talking to you is %s.") % (
            author.name or user.name or "")
        session._discuss_set_typing(True)  # immediate "typing…" feedback
        try:
            session.send_message(body_text, context_prefix=prefix)
        except UserError as e:
            session._discuss_set_typing(False)
            session._post_bot_message(str(e))
