# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Create checksums for every distributable release asset."""

import hashlib
import sys
from pathlib import Path

directory = Path(sys.argv[1])
target = directory / "SHA256SUMS"
target.write_text("".join(
    f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}\n"
    for path in sorted(directory.iterdir()) if path.is_file() and path != target
))
