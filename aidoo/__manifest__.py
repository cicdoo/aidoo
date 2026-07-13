# -*- coding: utf-8 -*-
# Copyright 2026 CICDoo (https://cicdoo.com)
# SPDX-License-Identifier: LGPL-3.0-or-later OR LicenseRef-Aidoo-Commercial
# Dual-licensed: open source (LGPL-3, see LICENSE) or commercial (see COMMERCIAL_LICENSE.md).
{
    'name': "Aidoo",
    'summary': "Aidoo — chat with your Odoo, safely. One assistant, four AI providers "
               "(Claude, Codex, Gemini, Grok), permission-aware ORM tools and "
               "read-only SQL reporting.",
    'description': """
Aidoo — Unified AI Assistant for Odoo
=====================================
A chat interface (menu + OWL client action) that drives a headless AI CLI on the
server. The provider is chosen **per conversation** — Claude (Anthropic Claude
Code), Codex (OpenAI), Gemini (Google) or Grok (xAI) — behind one set of safe
tools exposed over a sandboxed MCP bridge:

* Every ORM action runs with the **current user's** permission level (never superuser).
* Raw SQL reporting is **read-only** (SELECT only) and gated to the *AI SQL Analyst* group.
* The CLI's own built-in tools (Bash, file write) are disabled; it can only act
  through the Odoo tool endpoints. Web access (WebFetch/WebSearch / web search) is
  an opt-in per-user grant, off by default.
* Replies stream into the chat in real time over the Odoo bus.

Authentication is per-user and per-provider (no shared subscription): each user
connects their own account from the chat — Claude via OAuth, Codex via device
code, Gemini/Grok by importing credentials or an API key — stored privately in a
per-user, per-provider config dir. Chatting is gated until the conversation's
provider is connected.

Claude and Grok drive the provider's CLI on the server (binary required);
Codex, Gemini and Z.AI are native in-process providers — they talk to the
provider's backend directly from Python, no binary needed.
""",
    'author': "CICDoo",
    'website': "https://cicdoo.com",
    'category': 'Productivity/AI',
    'version': '19.0.1.7.0',
    'license': 'LGPL-3',
    'depends': ['web', 'bus', 'mail'],
    'external_dependencies': {'python': ['requests']},
    'data': [
        'security/aidoo_security.xml',
        'security/ir.model.access.csv',
        'data/aidoo_tool_data.xml',
        'data/aidoo_bot_data.xml',
        'data/aidoo_action.xml',
        'views/res_config_settings_views.xml',
        'views/res_users_views.xml',
        'views/aidoo_log_views.xml',
        'views/aidoo_scheduled_views.xml',
        'views/aidoo_skill_views.xml',
        'views/aidoo_persona_views.xml',
    ],
    'post_init_hook': 'post_init_hook',
    'assets': {
        'web.assets_backend': [
            'aidoo/static/src/scss/aidoo.scss',
            'aidoo/static/src/aidoo_service.js',
            'aidoo/static/src/markdown.js',
            'aidoo/static/src/artifacts.js',
            'aidoo/static/src/components/report_frame.js',
            'aidoo/static/src/components/report_frame.xml',
            'aidoo/static/src/components/message.js',
            'aidoo/static/src/components/message.xml',
            'aidoo/static/src/components/message_list.js',
            'aidoo/static/src/components/message_list.xml',
            'aidoo/static/src/components/composer.js',
            'aidoo/static/src/components/composer.xml',
            'aidoo/static/src/components/skill_picker_dialog.js',
            'aidoo/static/src/components/skill_picker_dialog.xml',
            'aidoo/static/src/chat_action.js',
            'aidoo/static/src/chat_action.xml',
            'aidoo/static/src/cog_menu_item.js',
            'aidoo/static/src/cog_menu_item.xml',
            'aidoo/static/src/view_context_service.js',
            'aidoo/static/src/view_patches.js',
            'aidoo/static/src/discuss/res_partner_model_patch.js',
            'aidoo/static/src/discuss/discuss_command_palette_patch.js',
            'aidoo/static/src/floating_button.js',
            'aidoo/static/src/floating_button.xml',
        ],
    },
    'application': True,
    'installable': True,
}
