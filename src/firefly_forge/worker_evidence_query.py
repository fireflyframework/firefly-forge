# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Model-facing retained-evidence input, scoped to the worker read budget."""

from pydantic import BaseModel, ConfigDict, Field

from firefly_forge.run_evidence import EvidenceQuery


class WorkerEvidenceQuery(EvidenceQuery):
    limit: int = Field(default=10, ge=1, le=10)


class WorkerEvidenceReadInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, hide_input_in_errors=True)
    query: WorkerEvidenceQuery
