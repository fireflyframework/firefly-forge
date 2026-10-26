# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""The native controller is registered without starting an API or database."""


def test_native_application_registers_original_selection_controller(tmp_path):
    from firefly_forge.api import create_runtime
    from firefly_forge.original_selection_controllers import OriginalSelectionReadmissionController

    runtime = create_runtime(tmp_path, "controlled-native-bootstrap-token-only", "combined")
    assert runtime.context.container.contains_type(OriginalSelectionReadmissionController)
