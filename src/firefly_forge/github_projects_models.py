# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Explicit, credential-free GitHub ProjectsV2 contracts."""

from typing import Annotated, Literal, Self

from pydantic import Field, JsonValue, field_validator, model_validator

from firefly_forge.git_delivery_models import GitDeliveryContract
from firefly_forge.scm_authority_models import Digest, NativeID

State = Literal[
    "queued", "running", "paused", "needs_reconciliation", "failed", "cancelled", "interrupted", "succeeded"
]
STATES: tuple[State, ...] = (
    "queued",
    "running",
    "paused",
    "needs_reconciliation",
    "failed",
    "cancelled",
    "interrupted",
    "succeeded",
)
NodeID = Annotated[str, Field(pattern=r"^[A-Za-z0-9_=-]{1,200}$")]
Owner = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9-]{0,99}$")]
OwnerKind = Literal["user", "organization"]
ConnectionName = Annotated[str, Field(pattern=r"^[a-z][a-z0-9-]{0,63}$")]


class BoardDiscoveryInputV1(GitDeliveryContract):
    version: Literal[1]
    connection_name: ConnectionName
    owner_kind: OwnerKind
    owner: Owner
    number: int = Field(ge=1, le=2_147_483_647)


class BoardCreateInputV1(GitDeliveryContract):
    version: Literal[1]
    request_id: NativeID
    connection_name: ConnectionName
    owner_kind: Literal["user"] = "user"
    owner: Owner
    title: str = Field(min_length=1, max_length=256)
    acknowledge_private_creation: Literal[True]

    @field_validator("acknowledge_private_creation", mode="before")
    @classmethod
    def explicit_creation(cls, value: object) -> object:
        if value is not True:
            raise ValueError("Explicitly authorize creating a private personal board")
        return value

    @field_validator("title")
    @classmethod
    def title_text(cls, value: str) -> str:
        if not value.strip() or any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise ValueError("Use a nonempty single-line board title")
        return value


class BoardConnectionInputV1(BoardDiscoveryInputV1):
    request_id: NativeID
    expected_digest: Digest | None
    board_id: NodeID
    field_id: NodeID
    state_options: dict[str, NodeID]
    enabled: bool
    include_existing: bool
    acknowledge_managed_field: Literal[True]

    @field_validator("acknowledge_managed_field", mode="before")
    @classmethod
    def explicit_consent(cls, value: object) -> object:
        if value is not True:
            raise ValueError("Explicitly acknowledge the selected managed field")
        return value

    @model_validator(mode="after")
    def exact_states(self) -> Self:
        if set(self.state_options) != set(STATES):
            raise ValueError("Map every supported factory state and no others")
        return self


class BoardSyncInputV1(GitDeliveryContract):
    version: Literal[1]
    request_id: NativeID
    expected_binding_digest: Digest


class BoardObserveInputV1(GitDeliveryContract):
    version: Literal[1]
    expected_request_digest: Digest


class BoardOption(GitDeliveryContract):
    id: NodeID
    name: str = Field(min_length=1, max_length=256)


class BoardField(GitDeliveryContract):
    id: NodeID
    name: str = Field(min_length=1, max_length=256)
    options: list[BoardOption] = Field(min_length=1)


class BoardIdentity(GitDeliveryContract):
    id: NodeID
    title: str = Field(min_length=1, max_length=256)
    url: str = Field(min_length=1, max_length=2048)
    owner_kind: OwnerKind
    owner: Owner
    number: int = Field(ge=1)
    viewer_can_update: bool
    closed: bool
    private: Literal[True]


class BoardCreationReceiptV1(GitDeliveryContract):
    version: Literal[1] = 1
    id: NativeID
    project_id: NativeID
    repository_id: NativeID
    request_digest: Digest
    status: Literal["unknown", "completed"]
    board: BoardIdentity | None
    replay_authorized: Literal[False] = False
    created_at: str


class BoardRepository(GitDeliveryContract):
    id: NodeID
    name_with_owner: str = Field(min_length=3, max_length=256)
    private: bool


class BoardViewer(GitDeliveryContract):
    id: NodeID
    login: Owner


class BoardDiscoveryV1(GitDeliveryContract):
    version: Literal[1] = 1
    connection_name: ConnectionName
    board: BoardIdentity
    repository: BoardRepository
    viewer: BoardViewer
    fields: list[BoardField] = Field(min_length=1)
    discovery_digest: Digest
    truncated: Literal[False] = False


class BoardRunProjection(GitDeliveryContract):
    run_id: NativeID
    state: State
    operation: Literal["build", "maintain", "test"]
    delivery_scope: Literal[
        "implementation", "checkpoint_verification", "checkpoint_maintenance", "maintenance", "verification"
    ]
    source_checkpoint: Digest | None
    product_completion_verified: Literal[False] = False


class BoardBindingV1(GitDeliveryContract):
    digest: Digest
    revision: int = Field(ge=1)
    enabled: bool
    connection_name: ConnectionName
    owner_kind: OwnerKind
    owner: Owner
    number: int = Field(ge=1)
    board_id: NodeID
    board_url: str
    field_id: NodeID
    state_options: dict[str, NodeID]
    include_existing: bool


class BoardPermissions(GitDeliveryContract):
    configure: bool
    sync: bool


class BoardRequestSummary(GitDeliveryContract):
    id: NativeID
    status: Literal["queued", "executing", "completed", "conflict", "unknown", "failed"]
    operation: Literal["build", "maintain", "test"] | None = None
    delivery_scope: (
        Literal["implementation", "checkpoint_verification", "checkpoint_maintenance", "maintenance", "verification"]
        | None
    ) = None
    source_checkpoint: Digest | None = None
    product_completion_verified: Literal[False] = False


class BoardConnectionViewV1(GitDeliveryContract):
    version: Literal[1] = 1
    project_id: NativeID
    repository_id: NativeID
    binding: BoardBindingV1 | None
    permissions: BoardPermissions
    status: Literal["not_configured", "ready", "authorization_required", "conflict", "unknown"]
    latest_request: BoardRequestSummary | None


class BoardContentReference(GitDeliveryContract):
    kind: Literal["issue", "pull_request"]
    number: int = Field(ge=1)
    source_run_id: NativeID
    receipt_digest: Digest


class BoardEvent(GitDeliveryContract):
    request_id: NativeID
    sequence: int = Field(ge=1)
    previous_hash: Digest
    kind: str = Field(min_length=1, max_length=64)
    evidence: dict[str, JsonValue]
    created_at: str
    digest: Digest


class BoardRequestReceiptV1(GitDeliveryContract):
    version: Literal[1] = 1
    id: NativeID
    project_id: NativeID
    repository_id: NativeID
    run_id: NativeID
    status: Literal["queued", "executing", "completed", "conflict", "unknown", "failed"]
    request_digest: Digest
    binding_digest: Digest
    projection: BoardRunProjection
    contents: list[BoardContentReference] = Field(min_length=1, max_length=2)
    branch: str | None
    checkpoint_revision: str | None
    source_tree: str | None
    campaign_id: NativeID | None
    events: list[BoardEvent]
    result: dict[str, JsonValue] | None
    created_at: str
