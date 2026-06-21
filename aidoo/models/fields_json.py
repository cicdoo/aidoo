# -*- coding: utf-8 -*-
# Copyright 2026 CICDoo (https://cicdoo.com)
# SPDX-License-Identifier: LGPL-3.0-or-later OR LicenseRef-Aidoo-Commercial
# Dual-licensed: open source (LGPL-3, see LICENSE) or commercial (see COMMERCIAL_LICENSE.md).
"""Minimal JSON field for Odoo 13.

``fields.Json`` only landed in Odoo 14. This backports the contract Aidoo
relies on: the value is stored as JSON text in a plain ``text`` column, while
server-side code reads and writes ordinary Python objects (lists/dicts). That
keeps every call site (e.g. ``rec.tool_calls or []``) untouched across versions.

The web client renders type ``json`` via the small read-only widget registered
in ``static/src/json_field.js`` (used by the audit-log form).
"""
import json

from odoo import fields


class Json(fields.Field):
    type = "json"
    column_type = ("text", "text")

    def convert_to_column(self, value, record, values=None, validate=True):
        # Python object -> JSON text for storage. A value that is already a
        # string is assumed to be JSON text (e.g. an unchanged value read from
        # the DB) and stored as-is, so it is never double-encoded.
        if value is None or value is False or value == "":
            return None
        if isinstance(value, str):
            return value
        return json.dumps(value)

    def convert_to_cache(self, value, record, validate=True):
        # Normalize assignments to a private parsed copy so mutating a record's
        # value never aliases the shared cache. Odoo 13's DB read path stores the
        # raw column string straight into the cache without calling this, which is
        # why convert_to_record below also parses.
        if value is None or value is False or value == "":
            return None
        if isinstance(value, str):
            try:
                return json.loads(value)
            except ValueError:
                return value
        return json.loads(json.dumps(value))

    def convert_to_record(self, value, record):
        # value may be a parsed object (assignment path) or the raw JSON string
        # straight from the DB column (read path) — return a Python object either way.
        if value is None or value is False or value == "":
            return False
        if isinstance(value, str):
            try:
                return json.loads(value)
            except ValueError:
                return value
        return value

    def convert_to_read(self, value, record, use_name_get=True):
        # Pretty JSON text for display in the (read-only) audit-log form.
        if value in (None, False, ""):
            return ""
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except ValueError:
                return value
        return json.dumps(value, indent=2, ensure_ascii=False)

    def convert_to_write(self, value, record):
        return value
