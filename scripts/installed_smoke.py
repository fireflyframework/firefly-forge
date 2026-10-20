# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Verify installed dependencies and assets without importing the source checkout."""

import subprocess
import sys
from importlib.metadata import version
from pathlib import Path

import firefly_forge
from firefly_forge.product_fonts import manrope_license, manrope_style, manrope_wordmark

package = Path(firefly_forge.__file__).parent
assert "site-packages" in package.parts, package
assert version("firefly-forge") == firefly_forge.__version__
assert version("flycanon-sdk") == "26.10.5"
assert (package / "static" / "index.html").is_file()
assert '<path d="' in manrope_wordmark("forge", "#262821")
assert "data:font/woff2;base64," in manrope_style()
assert "SIL OPEN FONT LICENSE" in manrope_license()
result = subprocess.run(
    [str(Path(sys.executable).parent / "forge"), "--version"], check=True, capture_output=True, text=True
)
assert firefly_forge.__version__ in result.stdout
print(f"Installed firefly forge {firefly_forge.__version__}: SDK, CLI, Studio and Manrope assets verified")
