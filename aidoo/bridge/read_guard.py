#!/usr/bin/env python3
# Copyright 2026 CICDoo (https://cicdoo.com)
# SPDX-License-Identifier: LGPL-3.0-or-later OR LicenseRef-Aidoo-Commercial
# Dual-licensed: open source (LGPL-3, see LICENSE) or commercial (see COMMERCIAL_LICENSE.md).
"""PreToolUse hook that sandboxes the Claude Code `Read` tool.

The AI assistant re-enables `Read` so the model can open files the user
attached, but ONLY under the per-session uploads dir. A path-scoped allow rule
is not enough on its own: in `default` permission mode the CLI auto-approves
read-only tools, so an unguarded `Read` could reach any file the odoo user can
(credentials, /etc, the scratch root's mcp.json that holds the bridge token).

This hook runs BEFORE permission evaluation and a `deny` decision overrides that
auto-approval, so it is the real boundary. It reads the hook payload on stdin,
resolves the requested path (following symlinks), and denies anything that does
not resolve to inside the allowed directory passed as argv[1].

Dependency-free and never imports Odoo — it is spawned by the CLI, not Odoo.
"""
import json
import os
import sys


def _emit_deny(reason):
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }))
    # Exit 0: the decision is carried in the JSON, not the exit status.
    sys.exit(0)


def main():
    allowed = os.path.realpath(sys.argv[1]) if len(sys.argv) > 1 else ""
    try:
        data = json.load(sys.stdin)
    except Exception:  # noqa: BLE001 - malformed payload => fail closed
        _emit_deny("Could not parse the Read request; denied.")
        return

    tool_input = data.get("tool_input") or {}
    fp = tool_input.get("file_path") or ""
    if not allowed or not fp:
        _emit_deny("Read is restricted to attached files in the working directory.")
        return

    # Resolve relative paths against the CLI's working directory, then follow
    # symlinks so a link inside the uploads dir can't escape it.
    if not os.path.isabs(fp):
        fp = os.path.join(data.get("cwd") or "", fp)
    real = os.path.realpath(fp)

    if real == allowed or real.startswith(allowed + os.sep):
        # Allowed: emit nothing and let the normal permission flow proceed.
        sys.exit(0)

    _emit_deny(
        "Read denied: you may only read the files the user attached to this "
        "conversation (in your working directory), not other files on the server.")


if __name__ == "__main__":
    main()
