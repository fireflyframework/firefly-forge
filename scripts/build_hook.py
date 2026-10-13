# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Include Studio in distributions while keeping source bootstrapping independent."""

from pathlib import Path
from typing import Any

from hatchling.builders.hooks.plugin.interface import BuildHookInterface


class CustomBuildHook(BuildHookInterface):
    def initialize(self, version: str, build_data: dict[str, Any]) -> None:
        if self.target_name != "wheel" or version == "editable":
            return
        studio = Path(self.root) / "studio" / "dist"
        if not (studio / "index.html").is_file():
            raise RuntimeError("Build Studio with npm --prefix studio run build before packaging Forge")
        build_data["force_include"][str(studio)] = "firefly_forge/static"
        build_data["force_include"][str(Path(self.root) / "assets" / "fonts" / "manrope")] = "firefly_forge/fonts/manrope"
