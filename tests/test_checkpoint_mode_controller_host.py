# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Pure native container registration; this does not start an API or database."""


def test_native_application_registers_the_typed_mode_controller_without_starting_runtime(tmp_path):
    from firefly_forge.api import create_runtime
    from firefly_forge.checkpoint_mode_controllers import CheckpointModeAdoptionController

    runtime = create_runtime(tmp_path, "controlled-native-bootstrap-token-only", "combined")
    assert runtime.context.container.contains_type(CheckpointModeAdoptionController)
