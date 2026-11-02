# -*- coding: utf-8 -*-
# Copyright 2026 CICDoo (https://cicdoo.com)
# SPDX-License-Identifier: LGPL-3.0-or-later OR LicenseRef-Aidoo-Commercial
# Dual-licensed: open source (LGPL-3, see LICENSE) or commercial (see COMMERCIAL_LICENSE.md).
"""A user's provider login, mirrored in the database.

The CLIs and native providers read their credentials from files under the
per-user home dir, which is local to the node. On a multi-node cluster (several
Odoo servers on one database) a login made through one node would be unknown to
the others, and a token refresh on one node rotates the refresh token out from
under every other node's copy. This model is the shared copy: each node
reconciles its files against it with a three-way merge (local files, this row,
and the base this node last synced, kept in a marker file next to them).

No access rules: only reached through sudo() from the provider adapters.
"""
import hashlib
import json
import logging
import os
import time

from odoo import fields, models

_logger = logging.getLogger(__name__)

# Per-node marker (under the provider home dir) holding the sha of the payload
# this node last pulled or pushed — the merge base.
SYNC_MARKER = ".aidoo_credential_sync"
# Credential files are small token bundles; anything bigger is not one.
MAX_FILE_BYTES = 256 * 1024


def _sha(payload):
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True).encode()).hexdigest()


class AidooCredential(models.Model):
    _name = "aidoo.credential"
    _description = "Aidoo Provider Login (shared across nodes)"

    user_id = fields.Many2one("res.users", required=True, ondelete="cascade",
                              index=True)
    provider = fields.Char(required=True)
    payload = fields.Text(help="JSON map of credential file (relative to the "
                               "provider home dir) to its content.")
    payload_sha = fields.Char()
    synced_at = fields.Float(help="Epoch seconds of the last push.")

    _user_provider_uniq = models.Constraint(
        "unique(user_id, provider)",
        "One shared login per user and provider.",
    )

    # ------------------------------------------------------------------
    # Local files
    # ------------------------------------------------------------------
    @staticmethod
    def _read_local(home, relpaths):
        """{relpath: content} for the credential files present, plus the newest
        mtime among them (0 when none)."""
        files, newest = {}, 0.0
        for rel in relpaths:
            path = os.path.join(home, rel)
            try:
                st = os.stat(path)
                if st.st_size > MAX_FILE_BYTES:
                    continue
                with open(path, encoding="utf-8") as f:
                    files[rel] = f.read()
                newest = max(newest, st.st_mtime)
            except (OSError, UnicodeDecodeError):
                continue
        return files, newest

    @staticmethod
    def _write_local(home, relpaths, payload):
        """Make the local files match ``payload``: write what it holds, remove
        the declared credential files it does not."""
        for rel in relpaths:
            path = os.path.join(home, rel)
            if rel in payload:
                os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
                tmp = path + ".tmp"
                fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(payload[rel])
                os.replace(tmp, path)
            else:
                try:
                    os.remove(path)
                except OSError:
                    pass

    @staticmethod
    def _read_marker(home):
        try:
            with open(os.path.join(home, SYNC_MARKER)) as f:
                return f.read().strip()
        except OSError:
            return ""

    @staticmethod
    def _write_marker(home, sha):
        os.makedirs(home, mode=0o700, exist_ok=True)
        path = os.path.join(home, SYNC_MARKER)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(sha)

    # ------------------------------------------------------------------
    # Reconcile
    # ------------------------------------------------------------------
    def _row(self, user, provider, lock=False):
        if lock:
            self.env.cr.execute(
                "SELECT id FROM aidoo_credential WHERE user_id = %s AND provider = %s "
                "FOR UPDATE", (user.id, provider))
            found = self.env.cr.fetchone()
            row = self.browse(found[0]) if found else self.browse()
            row.invalidate_recordset()
            return row
        return self.search([("user_id", "=", user.id), ("provider", "=", provider)],
                           limit=1)

    def _sync(self, user, provider, home, relpaths, lock=False):
        """Three-way merge of this node's credential files with the shared row.

        * local unchanged since this node's last sync, row moved on -> pull
        * local changed (login, logout, refresh, CLI rewrite), row unchanged -> push
        * both changed -> the newer side wins (file mtime vs row synced_at)
        * no row yet -> push whatever this node has (first node after upgrade)
        * node never synced (no marker) but holds a login -> treated as a local
          change, so the newer login wins when nodes logged in separately

        ``synced_at`` is the pushed files' mtime (push time for a logout), which
        is what makes "newer" comparable across nodes.

        ``lock`` takes the row FOR UPDATE first, so a caller about to spend a
        rotating refresh token sees any refresh another node just committed."""
        self = self.sudo()
        row = self._row(user, provider, lock=lock)
        local, mtime = self._read_local(home, relpaths)
        local_sha = _sha(local)
        stamp = mtime or time.time()
        if not row:
            if local:
                self.create({"user_id": user.id, "provider": provider,
                             "payload": json.dumps(local), "payload_sha": local_sha,
                             "synced_at": stamp})
            self._write_marker(home, local_sha)
            return "push" if local else "noop"
        if row.payload_sha == local_sha:
            self._write_marker(home, local_sha)
            return "noop"
        base = self._read_marker(home)
        local_changed = base != local_sha if base else bool(local)
        row_changed = base != row.payload_sha
        if local_changed and (not row_changed or stamp > (row.synced_at or 0)):
            row.write({"payload": json.dumps(local), "payload_sha": local_sha,
                       "synced_at": stamp})
            self._write_marker(home, local_sha)
            return "push"
        try:
            remote = json.loads(row.payload or "{}")
        except ValueError:
            remote = {}
        self._write_local(home, relpaths, remote)
        self._write_marker(home, row.payload_sha)
        return "pull"
