# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""One recognizable opaque credential format; no secret is encoded in metadata."""

import re

PERSONAL_TOKEN_PREFIX = "forge_pat_"
PERSONAL_TOKEN_PATTERN = re.compile(r"forge_pat_[a-f0-9]{32}\.[A-Za-z0-9_-]{43}")
PERSONAL_TOKEN_LENGTH = len(PERSONAL_TOKEN_PREFIX) + 32 + 1 + 43
