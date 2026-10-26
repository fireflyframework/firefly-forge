# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Journal event names must fit the actual existing native persistence contract."""

from firefly_forge.persistence import ForgeJournalEntity
from firefly_forge.unknown_command_proof import ACKNOWLEDGEMENT


def test_acknowledgement_kind_fits_existing_native_journal_column():
    width = ForgeJournalEntity.__table__.c.kind.type.length
    assert width == 32
    assert len(ACKNOWLEDGEMENT) <= width
    assert ACKNOWLEDGEMENT == "unknown_command_acknowledgement"
