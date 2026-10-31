# -*- coding: utf-8 -*-
# Copyright 2026 CICDoo (https://cicdoo.com)
# SPDX-License-Identifier: LGPL-3.0-or-later OR LicenseRef-Aidoo-Commercial
# Dual-licensed: open source (LGPL-3, see LICENSE) or commercial (see COMMERCIAL_LICENSE.md).
from . import base
from . import claude
from . import codex
from . import gemini
from . import zai
from . import grok  # inherits aidoo.provider.zai: import after it
from . import antigravity
from . import custom
