# -*- coding: utf-8 -*-
# Copyright 2026 CICDoo (https://cicdoo.com)
# SPDX-License-Identifier: LGPL-3.0-or-later OR LicenseRef-Aidoo-Commercial
# Dual-licensed: open source (LGPL-3, see LICENSE) or commercial (see COMMERCIAL_LICENSE.md).


def migrate(cr, version):
    """skill_id (Many2one) -> skill_ids (Many2many) on aidoo.session and
    aidoo.scheduled.

    Post-migration: the M2M rel tables exist only after the ORM schema update,
    while the dropped ``skill_id`` columns survive it (Odoo never drops
    columns), so their values are still readable here. Idempotent — the old
    column is dropped after copying, so a re-run is a no-op.
    """
    for table, rel, col in (
        ("aidoo_session", "aidoo_session_skill_rel", "session_id"),
        ("aidoo_scheduled", "aidoo_scheduled_skill_rel", "scheduled_id"),
    ):
        cr.execute(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_name = %s AND column_name = 'skill_id'", (table,))
        if not cr.fetchone():
            continue  # fresh install / already migrated
        # Identifiers are hardcoded constants above — safe to interpolate.
        # The JOIN guards against dangling skill FKs.
        cr.execute(
            """INSERT INTO %(rel)s (%(col)s, skill_id)
               SELECT s.id, s.skill_id FROM %(table)s s
               JOIN aidoo_skill k ON k.id = s.skill_id
               WHERE s.skill_id IS NOT NULL
               ON CONFLICT DO NOTHING""" % {
                "rel": rel, "col": col, "table": table})
        cr.execute("ALTER TABLE %s DROP COLUMN skill_id" % table)
