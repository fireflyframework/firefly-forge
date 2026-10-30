# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""MCP tools let external coding agents operate the same authenticated API."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.utilities.types import Image
from mcp.types import ToolAnnotations
from pydantic import SkipValidation, WithJsonSchema

from firefly_forge.access_controllers import MembershipInput
from firefly_forge.activity_models import ActivityContentQuery
from firefly_forge.agent_checkpoint_models import RestoreInput
from firefly_forge.assistant_attachments import AttachmentInput
from firefly_forge.campaign_delivery_plan_models import CampaignDeliveryPlanInput
from firefly_forge.campaign_report_history import CampaignReportArchiveInput
from firefly_forge.connection_oauth_models import OAuthConnectionInput
from firefly_forge.final_outcome_models import FinalOutcomeContinuationInputV1
from firefly_forge.framework_knowledge_models import (
    FrameworkCheckUpdatesInput,
    FrameworkRefreshInput,
    FrameworkSearchInput,
)
from firefly_forge.git_delivery_models import GitDeliveryInputV1, GitDeliveryPolicyInputV1
from firefly_forge.knowledge_ingestion import KoinonSourceInput
from firefly_forge.knowledge_platform import KoinonKnowledgeSettings, KoinonSqlInput
from firefly_forge.learning_models import LearningPolicy
from firefly_forge.learning_observation_attempt_models import LearningObservationAttemptInput
from firefly_forge.mcp_registry import MCPGrantInput, MCPReviewInput
from firefly_forge.model_profiles import ModelProfileInput
from firefly_forge.models import RepositoryInput, RunInput
from firefly_forge.product_lifecycle import FeatureChangeInput
from firefly_forge.project_campaigns import CampaignAction, CampaignLaunch, CampaignRetry
from firefly_forge.prompts import prompt_text
from firefly_forge.run_creation_models import RunCreationCancelInput, RunCreationInput
from firefly_forge.run_evidence import EvidenceQuery
from firefly_forge.runner_continuations import RunnerContinuationInput
from firefly_forge.sandbox_models import SandboxInput
from firefly_forge.scm_authority_models import SCMActorBindingInputV1
from firefly_forge.scm_poll_functional_models import SCMPollFunctionalGrantInputV1
from firefly_forge.scm_poll_resume_models import SCMPollResumeGrantInputV3
from firefly_forge.scm_product_change_models import SCMProductChangeGrantInputV1
from firefly_forge.scm_resume_models import SCMResumeGrantInputV2
from firefly_forge.task_routing_mcp import TaskRoutingInput
from firefly_forge.tool_broker import ToolPolicyInput
from firefly_forge.user_administration import (
    AdministrationReconciliation,
    AdministrationRecovery,
    UserEnabledInput,
    UserProvisionInput,
)

# Defer native validation to the SDK so argument errors cannot reflect uploaded bytes.
KoinonSettingsRequest = SkipValidation[KoinonKnowledgeSettings]
KoinonSqlRequest = SkipValidation[KoinonSqlInput]
KoinonSourceRequest = SkipValidation[KoinonSourceInput]
AttachmentUploadRequest = Annotated[Any, WithJsonSchema(AttachmentInput.model_json_schema())]
EligibleUserQuery = Annotated[str, WithJsonSchema({"type": "string", "minLength": 3, "maxLength": 120})]
MembershipRequest = Annotated[Any, WithJsonSchema(MembershipInput.model_json_schema())]
OAuthExpectedRevision = Annotated[
    Any, WithJsonSchema(OAuthConnectionInput.model_json_schema()["properties"]["expected_revision"])
]
IdentityEnabled = Annotated[Any, WithJsonSchema(UserEnabledInput.model_json_schema()["properties"]["enabled"])]
IdentityProvisionRequest = Annotated[Any, WithJsonSchema(UserProvisionInput.model_json_schema())]
IdentityReconciliationRequest = Annotated[Any, WithJsonSchema(AdministrationReconciliation.model_json_schema())]
IdentityRecoveryRequest = Annotated[Any, WithJsonSchema(AdministrationRecovery.model_json_schema())]
ObservationAttemptRequest = Annotated[Any, WithJsonSchema(LearningObservationAttemptInput.model_json_schema())]
RunnerContinuationRequest = Annotated[Any, WithJsonSchema(RunnerContinuationInput.model_json_schema())]
CampaignReportArchiveRequest = Annotated[Any, WithJsonSchema(CampaignReportArchiveInput.model_json_schema())]
CampaignReportArchiveCursor = Annotated[Any, WithJsonSchema({"type": "integer", "minimum": 0, "maximum": 100})]
CampaignDeliveryPlanRequest = SkipValidation[CampaignDeliveryPlanInput]
SCMActorBindingRequest = SkipValidation[SCMActorBindingInputV1]
SCMResumeGrantRequest = SkipValidation[SCMResumeGrantInputV2]
SCMPollResumeGrantRequest = SkipValidation[SCMPollResumeGrantInputV3]
RunEvidenceRequest = Annotated[Any, WithJsonSchema(EvidenceQuery.model_json_schema())]
ActivityContentRequest = Annotated[Any, WithJsonSchema(ActivityContentQuery.model_json_schema())]
SCMProductChangeGrantRequest = SkipValidation[SCMProductChangeGrantInputV1]
RecoveryIdentifier = Annotated[Any, WithJsonSchema({"type": "string", "pattern": r"^[a-f0-9]{32}$"})]
CompletionIdentifier = Annotated[Any, WithJsonSchema({"type": "string", "pattern": r"^fbr1_[a-f0-9]{32}$"})]
RunCreationRequest = SkipValidation[RunCreationInput]
RunCreationCancelRequest = SkipValidation[RunCreationCancelInput]
AgentCheckpointRestoreRequest = SkipValidation[RestoreInput]
FinalOutcomeContinuationRequest = Annotated[Any, WithJsonSchema(FinalOutcomeContinuationInputV1.model_json_schema())]
SCMPollFunctionalGrantRequest = SkipValidation[SCMPollFunctionalGrantInputV1]
GitDeliveryPolicyRequest = SkipValidation[GitDeliveryPolicyInputV1]
GitDeliveryRequest = SkipValidation[GitDeliveryInputV1]
DiffManifest = Annotated[Any, WithJsonSchema({"type": "string", "pattern": r"^[a-f0-9]{64}$"})]
DiffPosition = Annotated[Any, WithJsonSchema({"type": "integer", "minimum": 0})]
DiffWindowLimit = Annotated[Any, WithJsonSchema({"type": "integer", "minimum": 1, "maximum": 1048576})]
DiffCheckpointRequest = Annotated[
    Any,
    WithJsonSchema({"anyOf": [{"type": "string", "pattern": r"^[a-f0-9]{32}$"}, {"type": "null"}]}),
]
FrameworkArgument = Annotated[Any, WithJsonSchema({"type": "string", "enum": ["pyfly", "larafly"]})]
FrameworkPageSize = Annotated[Any, WithJsonSchema({"type": "integer", "minimum": 1, "maximum": 200})]
FrameworkCursor = Annotated[Any, WithJsonSchema({"anyOf": [{"type": "string"}, {"type": "null"}]})]
FrameworkCheckRequest = SkipValidation[FrameworkCheckUpdatesInput]
FrameworkRefreshRequest = SkipValidation[FrameworkRefreshInput]
FrameworkSearchRequest = SkipValidation[FrameworkSearchInput]


def create_server() -> MCPServer:
    server = MCPServer("firefly forge")

    @server.tool(
        description=prompt_text("mcp/get_final_outcome_candidate"),
        annotations=ToolAnnotations(
            read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False
        ),
    )
    def get_final_outcome_candidate(identifier: RecoveryIdentifier, request_id: RecoveryIdentifier) -> dict[str, Any]:
        from firefly_forge.cli import client

        with client() as connection:
            return connection.final_outcome_continuation_candidate(identifier, request_id)

    @server.tool(
        description=prompt_text("mcp/request_final_outcome_continuation"),
        annotations=ToolAnnotations(
            read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=True
        ),
    )
    def request_final_outcome_continuation(
        identifier: RecoveryIdentifier,
        request_id: RecoveryIdentifier,
        action_id: RecoveryIdentifier,
        request: FinalOutcomeContinuationRequest,
    ) -> dict[str, Any]:
        from firefly_forge.cli import client

        with client() as connection:
            return connection.request_final_outcome_continuation(identifier, request_id, action_id, request)

    @server.tool(
        description=prompt_text("mcp/get_final_outcome_status"),
        annotations=ToolAnnotations(
            read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False
        ),
    )
    def get_final_outcome_status(identifier: RecoveryIdentifier, request_id: RecoveryIdentifier) -> dict[str, Any]:
        from firefly_forge.cli import client

        with client() as connection:
            return connection.final_outcome_continuation_status(identifier, request_id)

    @server.tool(description=prompt_text("mcp/list_repositories"))
    def list_repositories() -> list[dict[str, Any]]:
        from firefly_forge.cli import client

        with client() as connection:
            return connection.repositories()

    @server.tool(description=prompt_text("mcp/connect_repository"))
    def connect_repository(name: str, url: str, provider: str = "git", branch: str = "main") -> dict[str, Any]:
        from firefly_forge.cli import client

        request = RepositoryInput.model_validate(
            {"name": name, "url": url, "provider": provider, "default_branch": branch}
        )
        with client() as connection:
            return connection.add_repository(request)

    @server.tool(description=prompt_text("mcp/create_run"))
    def create_run(
        repository_id: str,
        brief: str,
        backend: str | None = None,
        frontend: str | None = None,
        agent: str | None = None,
        operation: str = "build",
        checks: list[list[str]] | None = None,
        skills: list[str] | None = None,
        project_id: str | None = None,
        component_id: str | None = None,
        roles: dict[str, Any] | None = None,
        model_profile_id: str | None = None,
        review_model_profile_id: str | None = None,
    ) -> dict[str, Any]:
        from firefly_forge.cli import client

        request = RunInput.model_validate(
            {
                "repository_id": repository_id,
                "brief": brief,
                **({"backend": backend} if backend is not None else {}),
                **({"frontend": frontend} if frontend is not None else {}),
                **({"project_id": project_id} if project_id is not None else {}),
                **({"component_id": component_id} if component_id is not None else {}),
                **({"roles": roles} if roles is not None else {}),
                **({"agent": agent} if agent is not None else {}),
                **({"model_profile_id": model_profile_id} if model_profile_id is not None else {}),
                **(
                    {"assurance": {"review_model_profile_id": review_model_profile_id}}
                    if review_model_profile_id is not None
                    else {}
                ),
                "operation": operation,
                **({"commands": [{"argv": argv} for argv in checks]} if checks is not None else {}),
                **({"skills": skills} if skills is not None else {}),
            }
        )
        with client() as connection:
            return connection.create_run(request)

    @server.tool(
        description=prompt_text("mcp/request_run_creation"),
        annotations=ToolAnnotations(
            read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=True
        ),
    )
    def request_run_creation(request_id: RecoveryIdentifier, request: RunCreationRequest) -> dict[str, Any]:
        from firefly_forge.cli import client

        with client() as connection:
            return connection.request_run_creation(request_id, request)

    @server.tool(
        description=prompt_text("mcp/get_run_creation_request"),
        annotations=ToolAnnotations(
            read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False
        ),
    )
    def get_run_creation_request(request_id: RecoveryIdentifier) -> dict[str, Any]:
        from firefly_forge.cli import client

        with client() as connection:
            return connection.run_creation_request(request_id)

    @server.tool(
        description=prompt_text("mcp/cancel_run_creation_request"),
        annotations=ToolAnnotations(
            read_only_hint=False, destructive_hint=True, idempotent_hint=False, open_world_hint=True
        ),
    )
    def cancel_run_creation_request(
        request_id: RecoveryIdentifier, request: RunCreationCancelRequest
    ) -> dict[str, Any]:
        from firefly_forge.cli import client

        with client() as connection:
            return connection.cancel_run_creation_request(request_id, request)

    @server.tool(
        description=prompt_text("mcp/get_run_creation_decision"),
        annotations=ToolAnnotations(
            read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False
        ),
    )
    def get_run_creation_decision(request_id: RecoveryIdentifier) -> dict[str, Any]:
        from firefly_forge.cli import client

        with client() as connection:
            return connection.run_creation_decision(request_id)

    @server.tool(
        description=prompt_text("mcp/get_agent_checkpoint_candidates"),
        annotations=ToolAnnotations(
            read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False
        ),
    )
    def get_agent_checkpoint_candidates(identifier: RecoveryIdentifier) -> dict[str, Any]:
        from firefly_forge.cli import client

        with client() as connection:
            return connection.agent_checkpoint_candidates(identifier)

    @server.tool(
        description=prompt_text("mcp/request_agent_checkpoint_restore"),
        annotations=ToolAnnotations(
            read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=True
        ),
    )
    def request_agent_checkpoint_restore(
        identifier: RecoveryIdentifier, completion_id: CompletionIdentifier, request: AgentCheckpointRestoreRequest
    ) -> dict[str, Any]:
        from firefly_forge.cli import client

        with client() as connection:
            return connection.request_agent_checkpoint_restore(identifier, completion_id, request)

    @server.tool(
        description=prompt_text("mcp/get_agent_checkpoint_restore"),
        annotations=ToolAnnotations(
            read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False
        ),
    )
    def get_agent_checkpoint_restore(
        identifier: RecoveryIdentifier, completion_id: CompletionIdentifier, request_id: RecoveryIdentifier
    ) -> dict[str, Any]:
        from firefly_forge.cli import client

        with client() as connection:
            return connection.agent_checkpoint_restore(identifier, completion_id, request_id)

    @server.tool(description=prompt_text("mcp/get_run"))
    def get_run(identifier: str) -> dict[str, Any]:
        from firefly_forge.cli import client

        with client() as connection:
            return connection.get_run(identifier)

    @server.tool(description=prompt_text("mcp/get_execution_requests"))
    def get_execution_requests(identifier: str) -> list[dict[str, Any]]:
        from firefly_forge.cli import client

        with client() as connection:
            return connection.execution_requests(identifier)

    @server.tool(
        description=prompt_text("mcp/request_runner_continuation"),
        annotations=ToolAnnotations(
            read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=True
        ),
    )
    def request_runner_continuation(identifier: str, request: RunnerContinuationRequest) -> dict[str, Any]:
        from firefly_forge.cli import client

        with client() as connection:
            return connection.request_runner_continuation(identifier, request)

    @server.tool(
        description=prompt_text("mcp/get_runner_continuation"),
        annotations=ToolAnnotations(
            read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False
        ),
    )
    def get_runner_continuation(identifier: str, request_id: str) -> dict[str, Any]:
        from firefly_forge.cli import client

        with client() as connection:
            return connection.runner_continuation(identifier, request_id)

    @server.tool(
        description=prompt_text("mcp/get_runner_continuation_candidates"),
        annotations=ToolAnnotations(
            read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False
        ),
    )
    def get_runner_continuation_candidates(identifier: str) -> dict[str, Any]:
        from firefly_forge.cli import client

        with client() as connection:
            return connection.runner_continuation_candidates(identifier)

    @server.tool(description=prompt_text("mcp/reconcile_execution_request"))
    def reconcile_execution_request(identifier: str, request_id: str, reconciliation_id: str) -> dict[str, Any]:
        from firefly_forge.cli import client

        with client() as connection:
            return connection.reconcile_execution_request(identifier, request_id, reconciliation_id)

    @server.tool(description=prompt_text("mcp/cancel_run"))
    def cancel_run(identifier: str) -> dict[str, Any]:
        from firefly_forge.cli import client

        with client() as connection:
            return connection.action(identifier, "cancel")

    @server.tool(description=prompt_text("mcp/publish_run"))
    def publish_run(identifier: str) -> dict[str, Any]:
        from firefly_forge.cli import client

        with client() as connection:
            return connection.action(identifier, "publish")

    @server.tool(description=prompt_text("mcp/get_development_plan"))
    def get_development_plan(identifier: str) -> dict[str, Any]:
        from firefly_forge.cli import client

        with client() as connection:
            return connection.plan(identifier)

    @server.tool(
        description=prompt_text("mcp/query_run_evidence"),
        annotations=ToolAnnotations(
            read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False
        ),
    )
    def query_run_evidence(identifier: str, query: RunEvidenceRequest) -> dict[str, Any]:
        from firefly_forge.cli import client

        with client() as connection:
            return connection.run_evidence(identifier, query)

    @server.tool(
        description=prompt_text("mcp/get_run_activity_content"),
        annotations=ToolAnnotations(
            read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False
        ),
    )
    def get_run_activity_content(identifier: str, event_id: str, query: ActivityContentRequest) -> dict[str, Any]:
        from firefly_forge.cli import client

        with client() as connection:
            return connection.run_activity_content(identifier, event_id, query)

    @server.tool(description=prompt_text("mcp/get_execution_journal"))
    def get_execution_journal(identifier: str) -> dict[str, Any]:
        from firefly_forge.cli import client

        with client() as connection:
            return connection.journal(identifier)

    @server.tool(description=prompt_text("mcp/resume_run"))
    def resume_run(identifier: str) -> dict[str, Any]:
        from firefly_forge.cli import client

        with client() as connection:
            return connection.action(identifier, "resume")

    @server.tool(description=prompt_text("mcp/handoff_run"))
    def handoff_run(
        identifier: str,
        agent: Literal["codex", "claude", "bridge", "agentic"] | None = None,
        model_profile_id: str | None = None,
    ) -> dict[str, Any]:
        from firefly_forge.cli import client

        with client() as connection:
            return connection.handoff(identifier, agent, model_profile_id=model_profile_id)

    @server.tool(description=prompt_text("mcp/list_source_connections"))
    def list_source_connections() -> list[dict[str, Any]]:
        from firefly_forge.cli import client

        with client() as connection:
            return connection.source_connections()

    @server.tool(description=prompt_text("mcp/connect_granted_repository"))
    def connect_granted_repository(provider: str, remote_id: str) -> dict[str, Any]:
        from firefly_forge.cli import client

        with client() as connection:
            return connection.connect_granted_repository(provider, remote_id)

    @server.tool(
        description=prompt_text("mcp/get_knowledge_configuration"),
        annotations=ToolAnnotations(
            read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False
        ),
    )
    def get_knowledge_configuration() -> dict[str, Any]:
        from firefly_forge.cli import client

        with client() as connection:
            return connection.knowledge_configuration()

    @server.tool(description=prompt_text("mcp/configure_koinon_knowledge"))
    def configure_koinon_knowledge(settings: KoinonSettingsRequest, clear_access_token: bool = False) -> dict[str, Any]:
        from firefly_forge.cli import client

        with client() as connection:
            return connection.configure_koinon_knowledge(settings, clear_access_token=clear_access_token)

    @server.tool(
        description=prompt_text("mcp/check_koinon_knowledge"),
        annotations=ToolAnnotations(
            read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False
        ),
    )
    def check_koinon_knowledge(repository_id: str) -> dict[str, Any]:
        from firefly_forge.cli import client

        with client() as connection:
            return connection.check_koinon_knowledge(repository_id)

    @server.tool(
        description=prompt_text("mcp/get_project_knowledge_schema"),
        annotations=ToolAnnotations(
            read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False
        ),
    )
    def get_project_knowledge_schema(repository_id: str, dataset_id: str) -> dict[str, Any]:
        from firefly_forge.cli import client

        with client() as connection:
            return connection.knowledge_schema(repository_id, dataset_id)

    @server.tool(
        description=prompt_text("mcp/read_project_knowledge_chunks"),
        annotations=ToolAnnotations(
            read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False
        ),
    )
    def read_project_knowledge_chunks(
        repository_id: str, document_id: str, offset: int = 0, limit: int = 50, expected_identity: str | None = None
    ) -> dict[str, Any]:
        from firefly_forge.cli import client

        with client() as connection:
            return connection.knowledge_chunks(
                repository_id, document_id, offset=offset, limit=limit, expected_identity=expected_identity
            )

    @server.tool(
        description=prompt_text("mcp/execute_project_knowledge_sql"),
        annotations=ToolAnnotations(
            read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=False
        ),
    )
    def execute_project_knowledge_sql(run_id: str, request: KoinonSqlRequest) -> dict[str, Any]:
        from firefly_forge.cli import client

        with client() as connection:
            return connection.query_knowledge_sql(run_id, request)

    @server.tool(
        description=prompt_text("mcp/get_project_knowledge_sql_request"),
        annotations=ToolAnnotations(
            read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False
        ),
    )
    def get_project_knowledge_sql_request(run_id: str, request_id: str) -> dict[str, Any]:
        from firefly_forge.cli import client

        with client() as connection:
            return connection.knowledge_sql_request(run_id, request_id)

    @server.tool(
        description=prompt_text("mcp/ingest_koinon_source"),
        annotations=ToolAnnotations(
            read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=True
        ),
    )
    def ingest_koinon_source(run_id: RecoveryIdentifier, request: KoinonSourceRequest) -> dict[str, Any]:
        from firefly_forge.cli import client

        with client() as connection:
            return connection.ingest_koinon_source(run_id, request)

    @server.tool(
        description=prompt_text("mcp/upload_koinon_document"),
        annotations=ToolAnnotations(
            read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=True
        ),
    )
    def upload_koinon_document(repository_id: RecoveryIdentifier, request: KoinonSourceRequest) -> dict[str, Any]:
        from firefly_forge.cli import client

        with client() as connection:
            return connection.upload_koinon_document(repository_id, request)

    @server.tool(
        description=prompt_text("mcp/get_koinon_ingestion"),
        annotations=ToolAnnotations(
            read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False
        ),
    )
    def get_koinon_ingestion(
        run_id: RecoveryIdentifier, kind: Literal["source", "memory"], request_id: RecoveryIdentifier
    ) -> dict[str, Any]:
        from firefly_forge.cli import client

        with client() as connection:
            return connection.koinon_ingestion(run_id, kind, request_id)

    @server.tool(
        description=prompt_text("mcp/observe_koinon_ingestion"),
        annotations=ToolAnnotations(
            read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=True
        ),
    )
    def observe_koinon_ingestion(
        run_id: RecoveryIdentifier, kind: Literal["source", "memory"], request_id: RecoveryIdentifier, remote_id: str
    ) -> dict[str, Any]:
        from firefly_forge.cli import client

        with client() as connection:
            return connection.observe_koinon_ingestion(run_id, kind, request_id, remote_id)

    @server.tool(
        description=prompt_text("mcp/get_knowledge_upload"),
        annotations=ToolAnnotations(
            read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=True
        ),
    )
    def get_knowledge_upload(repository_id: RecoveryIdentifier, request_id: RecoveryIdentifier) -> dict[str, Any]:
        from firefly_forge.cli import client

        with client() as connection:
            return connection.knowledge_upload(repository_id, request_id)

    @server.tool(
        description=prompt_text("mcp/reconcile_knowledge_upload"),
        annotations=ToolAnnotations(
            read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=True
        ),
    )
    def reconcile_knowledge_upload(
        repository_id: RecoveryIdentifier, request_id: RecoveryIdentifier, source_id: str
    ) -> dict[str, Any]:
        from firefly_forge.cli import client

        with client() as connection:
            return connection.reconcile_knowledge_upload(repository_id, request_id, source_id)

    @server.tool(description=prompt_text("mcp/search_project_knowledge", version="1.0.1"))
    def search_project_knowledge(repository_id: str, query: str) -> dict[str, Any]:
        from firefly_forge.cli import client

        with client() as connection:
            return connection.search_knowledge(repository_id, query)

    @server.tool(
        description=prompt_text("mcp/get_framework_knowledge_status"),
        annotations=ToolAnnotations(
            read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False
        ),
    )
    def get_framework_knowledge_status() -> dict[str, Any]:
        from firefly_forge.cli import client

        with client() as connection:
            return connection.framework_knowledge_status()

    @server.tool(
        description=prompt_text("mcp/check_framework_updates"),
        annotations=ToolAnnotations(
            read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=True
        ),
    )
    def check_framework_updates(framework: FrameworkArgument, request: FrameworkCheckRequest) -> dict[str, Any]:
        from firefly_forge.cli import client

        with client() as connection:
            return connection.check_framework_updates(framework, request)

    @server.tool(
        description=prompt_text("mcp/refresh_framework_knowledge"),
        annotations=ToolAnnotations(
            read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=True
        ),
    )
    def refresh_framework_knowledge(
        framework: FrameworkArgument, request_id: RecoveryIdentifier, request: FrameworkRefreshRequest
    ) -> dict[str, Any]:
        from firefly_forge.cli import client

        with client() as connection:
            return connection.refresh_framework_knowledge(framework, request_id, request)

    @server.tool(
        description=prompt_text("mcp/get_framework_knowledge_refresh"),
        annotations=ToolAnnotations(
            read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False
        ),
    )
    def get_framework_knowledge_refresh(framework: FrameworkArgument, request_id: RecoveryIdentifier) -> dict[str, Any]:
        from firefly_forge.cli import client

        with client() as connection:
            return connection.framework_knowledge_refresh(framework, request_id)

    @server.tool(
        description=prompt_text("mcp/observe_framework_knowledge_refresh"),
        annotations=ToolAnnotations(
            read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=True
        ),
    )
    def observe_framework_knowledge_refresh(
        framework: FrameworkArgument, request_id: RecoveryIdentifier
    ) -> dict[str, Any]:
        from firefly_forge.cli import client

        with client() as connection:
            return connection.observe_framework_knowledge_refresh(framework, request_id)

    @server.tool(
        description=prompt_text("mcp/list_framework_corpora"),
        annotations=ToolAnnotations(
            read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False
        ),
    )
    def list_framework_corpora(
        framework: FrameworkArgument, cursor: FrameworkCursor = None, page_size: FrameworkPageSize = 50
    ) -> dict[str, Any]:
        from firefly_forge.cli import client

        with client() as connection:
            return connection.framework_corpora(framework, cursor=cursor, page_size=page_size)

    @server.tool(
        description=prompt_text("mcp/list_framework_corpus_sources"),
        annotations=ToolAnnotations(
            read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False
        ),
    )
    def list_framework_corpus_sources(
        framework: FrameworkArgument,
        corpus_ref: RecoveryIdentifier,
        cursor: FrameworkCursor = None,
        page_size: FrameworkPageSize = 50,
    ) -> dict[str, Any]:
        from firefly_forge.cli import client

        with client() as connection:
            return connection.framework_corpus_sources(framework, corpus_ref, cursor=cursor, page_size=page_size)

    @server.tool(
        description=prompt_text("mcp/search_framework_knowledge"),
        annotations=ToolAnnotations(
            read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=True
        ),
    )
    def search_framework_knowledge(framework: FrameworkArgument, request: FrameworkSearchRequest) -> dict[str, Any]:
        from firefly_forge.cli import client

        with client() as connection:
            return connection.search_framework_knowledge(framework, request)

    @server.tool(description=prompt_text("mcp/index_verified_source"))
    def index_verified_source(identifier: str, paths: list[str]) -> dict[str, Any]:
        from firefly_forge.cli import client

        with client() as connection:
            return connection.index_source(identifier, paths)

    @server.tool(description=prompt_text("mcp/remember_verified_run", version="1.0.1"))
    def remember_verified_run(identifier: str) -> dict[str, Any]:
        from firefly_forge.cli import client

        with client() as connection:
            return connection.remember(identifier)

    @server.tool(description=prompt_text("mcp/list_deployment_targets"))
    def list_deployment_targets() -> list[dict[str, Any]]:
        from firefly_forge.cli import client

        with client() as connection:
            return connection.deployment_targets()

    @server.tool(description=prompt_text("mcp/deploy_verified_run"))
    def deploy_verified_run(identifier: str, target_id: str) -> dict[str, Any]:
        from firefly_forge.cli import client

        with client() as connection:
            return connection.deploy(identifier, target_id)

    @server.tool(description=prompt_text("mcp/rollback_deployment"))
    def rollback_deployment(identifier: str) -> dict[str, Any]:
        from firefly_forge.cli import client

        with client() as connection:
            return connection.rollback(identifier)

    @server.tool(description=prompt_text("mcp/get_product_definition"))
    def get_product_definition(repository_id: str) -> dict[str, Any] | None:
        from firefly_forge.cli import client

        with client() as connection:
            return connection.product_definition(repository_id)

    @server.tool(description=prompt_text("mcp/save_product_definition"))
    def save_product_definition(repository_id: str, definition: dict[str, Any]) -> dict[str, Any]:
        from firefly_forge.cli import client

        with client() as connection:
            return connection.save_product_definition(repository_id, definition)

    @server.tool(description=prompt_text("mcp/preview_product_change"))
    def preview_product_change(repository_id: str, request: FeatureChangeInput) -> dict[str, Any]:
        from firefly_forge.cli import client

        with client() as connection:
            return connection.preview_product_change(repository_id, request)

    @server.tool(description=prompt_text("mcp/apply_product_change"))
    def apply_product_change(repository_id: str, request: FeatureChangeInput) -> dict[str, Any]:
        from firefly_forge.cli import client

        with client() as connection:
            return connection.apply_product_change(repository_id, request)

    @server.tool(description=prompt_text("mcp/list_model_profiles"))
    def list_model_profiles() -> list[dict[str, Any]]:
        from firefly_forge.cli import client

        with client() as connection:
            return connection.model_profiles()

    @server.tool(description=prompt_text("mcp/configure_model_profile"))
    def configure_model_profile(
        profile_id: str, request: ModelProfileInput, credential_file: str | None = None
    ) -> dict[str, Any]:
        from pathlib import Path

        from firefly_forge.cli import client, model_profile_request

        model = model_profile_request(request, Path(credential_file) if credential_file else None)
        with client() as connection:
            return connection.save_model_profile(profile_id, model)

    @server.tool(description=prompt_text("mcp/test_model_profile"))
    def test_model_profile(profile_id: str) -> dict[str, Any]:
        from firefly_forge.cli import client

        with client() as connection:
            return connection.test_model_profile(profile_id)

    @server.tool(description=prompt_text("mcp/get_task_routing"))
    def get_task_routing() -> dict[str, Any]:
        from firefly_forge.cli import client

        with client() as connection:
            return connection.task_routing()

    @server.tool(description=prompt_text("mcp/save_task_routing"))
    def save_task_routing(request: TaskRoutingInput) -> dict[str, Any]:
        from firefly_forge.cli import client

        with client() as connection:
            return connection.save_task_routing(request)

    @server.tool(description=prompt_text("mcp/get_repository_workflow"))
    def get_repository_workflow(repository_id: str) -> dict[str, Any]:
        from firefly_forge.cli import client

        with client() as connection:
            return connection.repository_workflow(repository_id)

    @server.tool(description=prompt_text("mcp/list_repository_receipts"))
    def list_repository_receipts(repository_id: str | None = None) -> list[dict[str, Any]]:
        from firefly_forge.cli import client

        with client() as connection:
            return connection.repository_receipts(repository_id)

    @server.tool(description=prompt_text("mcp/list_cloud_providers"))
    def list_cloud_providers() -> list[dict[str, Any]]:
        from firefly_forge.cli import client

        with client() as connection:
            return connection.cloud_providers()

    @server.tool(description=prompt_text("mcp/render_cloud_recipe"))
    def render_cloud_recipe(recipe: dict[str, Any]) -> dict[str, Any]:
        from firefly_forge.cli import client

        with client() as connection:
            return connection.render_cloud_recipe(recipe)

    @server.tool(description=prompt_text("mcp/list_applications"))
    def list_applications() -> list[dict[str, Any]]:
        from firefly_forge.cli import client

        with client() as connection:
            return connection.applications()

    @server.tool(description=prompt_text("mcp/get_application"))
    def get_application(identifier: str) -> dict[str, Any]:
        from firefly_forge.cli import client

        with client() as connection:
            return connection.application(identifier)

    @server.tool(description=prompt_text("mcp/check_application"))
    def check_application(identifier: str) -> dict[str, Any]:
        from firefly_forge.cli import client

        with client() as connection:
            return connection.check_application(identifier)

    @server.tool(description=prompt_text("mcp/list_incidents"))
    def list_incidents() -> list[dict[str, Any]]:
        from firefly_forge.cli import client

        with client() as connection:
            return connection.incidents()

    @server.tool(description=prompt_text("mcp/request_incident_maintenance"))
    def request_incident_maintenance(
        identifier: str, brief: str, checks: list[list[str]], agent: str = "codex"
    ) -> dict[str, Any]:
        from firefly_forge.cli import client

        with client() as connection:
            return connection.maintain_incident(identifier, brief, checks, agent)

    @server.tool(description=prompt_text("mcp/resolve_incident"))
    def resolve_incident(identifier: str, resolution: str) -> dict[str, Any]:
        from firefly_forge.cli import client

        with client() as connection:
            return connection.resolve_incident(identifier, resolution)

    @server.tool(description=prompt_text("mcp/list_projects"))
    def list_projects() -> list[dict[str, Any]]:
        from firefly_forge.cli import client

        with client() as connection:
            return connection.projects()

    @server.tool(description=prompt_text("mcp/configure_project"))
    def configure_project(definition: dict[str, Any], project_id: str | None = None) -> dict[str, Any]:
        from firefly_forge.cli import client

        with client() as connection:
            return connection.configure_project(definition, project_id)

    @server.tool(description=prompt_text("mcp/get_project"))
    def get_project(project_id: str) -> dict[str, Any]:
        from firefly_forge.cli import client

        with client() as connection:
            return connection.project(project_id)

    @server.tool(
        description=prompt_text("mcp/get_git_delivery_policy"),
        annotations=ToolAnnotations(
            read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False
        ),
    )
    def get_git_delivery_policy(project_id: RecoveryIdentifier) -> dict[str, Any]:
        from firefly_forge.cli import client

        with client() as connection:
            return connection.git_delivery_policy(project_id)

    @server.tool(
        description=prompt_text("mcp/configure_git_delivery_policy"),
        annotations=ToolAnnotations(
            read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=True
        ),
    )
    def configure_git_delivery_policy(
        project_id: RecoveryIdentifier, request: GitDeliveryPolicyRequest
    ) -> dict[str, Any]:
        from firefly_forge.cli import client

        with client() as connection:
            return connection.configure_git_deliveries(project_id, request)

    @server.tool(
        description=prompt_text("mcp/get_git_deliveries"),
        annotations=ToolAnnotations(
            read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False
        ),
    )
    def get_git_deliveries(run_id: RecoveryIdentifier) -> dict[str, Any]:
        from firefly_forge.cli import client

        with client() as connection:
            return connection.git_deliveries(run_id)

    @server.tool(
        description=prompt_text("mcp/request_git_delivery"),
        annotations=ToolAnnotations(
            read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=True
        ),
    )
    def request_git_delivery(run_id: RecoveryIdentifier, request: GitDeliveryRequest) -> dict[str, Any]:
        from firefly_forge.cli import client

        with client() as connection:
            return connection.request_git_delivery(run_id, request)

    @server.tool(
        description=prompt_text("mcp/get_git_delivery_request"),
        annotations=ToolAnnotations(
            read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False
        ),
    )
    def get_git_delivery_request(run_id: RecoveryIdentifier, request_id: RecoveryIdentifier) -> dict[str, Any]:
        from firefly_forge.cli import client

        with client() as connection:
            return connection.git_delivery_request(run_id, request_id)

    @server.tool(
        description=prompt_text("mcp/get_git_delivery_observations"),
        annotations=ToolAnnotations(
            read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=True
        ),
    )
    def get_git_delivery_observations(run_id: RecoveryIdentifier, request_id: RecoveryIdentifier) -> dict[str, Any]:
        from firefly_forge.cli import client

        with client() as connection:
            return connection.git_delivery_observations(run_id, request_id)

    @server.tool(
        description=prompt_text("mcp/get_run_diff_index"),
        annotations=ToolAnnotations(
            read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False
        ),
    )
    def get_run_diff_index(
        run_id: RecoveryIdentifier,
        expected_manifest: DiffManifest,
        page: DiffPosition = 0,
        request_id: DiffCheckpointRequest = None,
    ) -> dict[str, Any]:
        from firefly_forge.cli import client

        with client() as connection:
            return connection.run_diff_index(run_id, expected_manifest, page=page, request_id=request_id).model_dump(
                mode="json"
            )

    @server.tool(
        description=prompt_text("mcp/get_run_diff_file"),
        annotations=ToolAnnotations(
            read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False
        ),
    )
    def get_run_diff_file(
        run_id: RecoveryIdentifier,
        expected_manifest: DiffManifest,
        file_index: DiffPosition,
        offset: DiffPosition = 0,
        limit: DiffWindowLimit = 32768,
        request_id: DiffCheckpointRequest = None,
    ) -> dict[str, Any]:
        from firefly_forge.cli import client

        with client() as connection:
            return connection.run_diff_file(
                run_id, expected_manifest, file_index=file_index, offset=offset, limit=limit, request_id=request_id
            ).model_dump(mode="json")

    @server.tool(description=prompt_text("mcp/get_project_revision"))
    def get_project_revision(project_id: str, revision: int) -> dict[str, Any]:
        from firefly_forge.cli import client

        with client() as connection:
            return connection.project_revision(project_id, revision)

    @server.tool(description=prompt_text("mcp/list_source_analyses"))
    def list_source_analyses(project_id: str) -> list[dict[str, Any]]:
        from firefly_forge.cli import client

        with client() as connection:
            return connection.project_analyses(project_id)

    @server.tool(description=prompt_text("mcp/analyze_project"))
    def analyze_project(project_id: str, component_ids: list[str] | None = None) -> dict[str, Any]:
        from firefly_forge.cli import client

        with client() as connection:
            return connection.analyze_project(project_id, component_ids)

    @server.tool(description=prompt_text("mcp/get_source_analysis"))
    def get_source_analysis(project_id: str, analysis_id: str) -> dict[str, Any]:
        from firefly_forge.cli import client

        with client() as connection:
            return connection.project_analysis(project_id, analysis_id)

    @server.tool(description=prompt_text("mcp/review_source_analysis"))
    def review_source_analysis(project_id: str, analysis_id: str, digest: str) -> dict[str, Any]:
        from firefly_forge.cli import client

        with client() as connection:
            return connection.review_project_analysis(project_id, analysis_id, {"digest": digest, "reviewed": True})

    @server.tool(description=prompt_text("mcp/list_product_conversations"))
    def list_product_conversations(repository_id: str) -> list[dict[str, Any]]:
        from firefly_forge.cli import client

        with client() as connection:
            return connection.assistant_conversations(repository_id)

    @server.tool(description=prompt_text("mcp/start_product_conversation"))
    def start_product_conversation(
        repository_id: str,
        agent: Literal["bridge", "agentic"] | None = None,
        project_id: str | None = None,
        task: Literal["product", "functions", "design", "architecture"] = "product",
        model_profile_id: str | None = None,
        analysis_id: str | None = None,
        analysis_digest: str | None = None,
        analysis_component_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        from firefly_forge.cli import client

        with client() as connection:
            return connection.create_assistant_conversation(
                repository_id,
                agent=agent,
                project_id=project_id,
                task=task,
                model_profile_id=model_profile_id,
                analysis_id=analysis_id,
                analysis_digest=analysis_digest,
                analysis_component_ids=analysis_component_ids,
            )

    @server.tool(description=prompt_text("mcp/get_product_conversation"))
    def get_product_conversation(repository_id: str, conversation_id: str) -> dict[str, Any]:
        from firefly_forge.cli import client

        with client() as connection:
            return connection.assistant_conversation(repository_id, conversation_id)

    @server.tool(description=prompt_text("mcp/get_product_conversation_capabilities"))
    def get_product_conversation_capabilities(repository_id: str, conversation_id: str) -> dict[str, Any]:
        from firefly_forge.cli import client

        with client() as connection:
            return connection.assistant_capabilities(repository_id, conversation_id)

    @server.tool(description=prompt_text("mcp/recover_product_design_turn"))
    def recover_product_design_turn(
        repository_id: str,
        conversation_id: str,
        turn_id: str,
        request_id: str,
        expected_epoch: int,
        legacy_confirmed_stopped: bool = False,
    ) -> dict[str, Any]:
        from firefly_forge.cli import client

        with client() as connection:
            return connection.recover_assistant_turn(
                repository_id,
                conversation_id,
                turn_id,
                {
                    "request_id": request_id,
                    "expected_epoch": expected_epoch,
                    "legacy_confirmed_stopped": legacy_confirmed_stopped,
                },
            )

    @server.tool(description=prompt_text("mcp/request_product_design_turn"))
    def request_product_design_turn(
        repository_id: str,
        conversation_id: str,
        request_id: str,
        message: str,
        attachment_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        from firefly_forge.cli import client

        with client() as connection:
            return connection.assistant_turn(
                repository_id,
                conversation_id,
                {"request_id": request_id, "message": message, "attachment_ids": attachment_ids},
            )

    @server.tool(description=prompt_text("mcp/list_product_attachments"))
    def list_product_attachments(repository_id: str, conversation_id: str) -> list[dict[str, Any]]:
        from firefly_forge.cli import client

        with client() as connection:
            return connection.assistant_attachments(repository_id, conversation_id)

    @server.tool(description=prompt_text("mcp/get_product_attachment"))
    def get_product_attachment(repository_id: str, conversation_id: str, attachment_id: str) -> dict[str, Any]:
        from firefly_forge.cli import client

        with client() as connection:
            return connection.assistant_attachment(repository_id, conversation_id, attachment_id)

    @server.tool(description=prompt_text("mcp/upload_product_attachment"))
    def upload_product_attachment(
        repository_id: str,
        conversation_id: str,
        request: AttachmentUploadRequest,
    ) -> dict[str, Any]:
        from firefly_forge.cli import client

        with client() as connection:
            return connection.upload_assistant_attachment(repository_id, conversation_id, request)

    @server.tool(description=prompt_text("mcp/get_agent_roles"))
    def get_agent_roles() -> dict[str, Any]:
        from firefly_forge.cli import client

        with client() as connection:
            return connection.agent_roles()

    @server.tool(
        description=prompt_text("mcp/get_agent_role_lifecycle"), annotations=ToolAnnotations(read_only_hint=True)
    )
    def get_agent_role_lifecycle() -> dict[str, Any]:
        from firefly_forge.cli import client

        with client() as connection:
            return connection.agent_role_lifecycle()

    @server.tool(
        description=prompt_text("mcp/get_repository_agent_role_lifecycle"),
        annotations=ToolAnnotations(read_only_hint=True),
    )
    def get_repository_agent_role_lifecycle(repository_id: str) -> dict[str, Any]:
        from firefly_forge.cli import client

        with client() as connection:
            return connection.repository_role_lifecycle(repository_id)

    @server.tool(
        description=prompt_text("mcp/get_identity_configuration"), annotations=ToolAnnotations(read_only_hint=True)
    )
    def get_identity_configuration() -> dict[str, Any]:
        from firefly_forge.cli import client

        with client() as connection:
            return connection.identity_configuration()

    @server.tool(description=prompt_text("mcp/get_identity_session"), annotations=ToolAnnotations(read_only_hint=True))
    def get_identity_session() -> dict[str, Any]:
        from firefly_forge.cli import client

        with client() as connection:
            return connection.identity_session()

    @server.tool(description=prompt_text("mcp/list_project_members"), annotations=ToolAnnotations(read_only_hint=True))
    def list_project_members(project_id: str) -> list[dict[str, Any]]:
        from firefly_forge.cli import client

        with client() as connection:
            return connection.project_members(project_id)

    @server.tool(
        description=prompt_text("mcp/set_project_membership"),
        annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True),
    )
    def set_project_membership(project_id: str, user_id: str, request: MembershipRequest) -> dict[str, Any]:
        from firefly_forge.cli import client

        with client() as connection:
            return connection.set_project_membership(project_id, user_id, request)

    @server.tool(
        description=prompt_text("mcp/list_scm_actor_binding_candidates"),
        annotations=ToolAnnotations(
            read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False
        ),
    )
    def list_scm_actor_binding_candidates(
        project_id: str, repository_id: str, cursor: str | None = None
    ) -> dict[str, Any]:
        from firefly_forge.cli import client

        with client() as connection:
            return connection.scm_actor_binding_candidates(project_id, repository_id, cursor)

    @server.tool(
        description=prompt_text("mcp/configure_scm_actor_binding"),
        annotations=ToolAnnotations(
            read_only_hint=False, destructive_hint=True, idempotent_hint=True, open_world_hint=True
        ),
    )
    def configure_scm_actor_binding(
        project_id: str, repository_id: str, request: SCMActorBindingRequest
    ) -> dict[str, Any]:
        from firefly_forge.cli import client

        with client() as connection:
            return connection.configure_scm_actor_binding(project_id, repository_id, request)

    @server.tool(
        description=prompt_text("mcp/list_scm_actor_bindings"),
        annotations=ToolAnnotations(
            read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False
        ),
    )
    def list_scm_actor_bindings(project_id: str, repository_id: str, cursor: str | None = None) -> dict[str, Any]:
        from firefly_forge.cli import client

        with client() as connection:
            return connection.scm_actor_bindings(project_id, repository_id, cursor)

    @server.tool(
        description=prompt_text("mcp/get_scm_actor_binding"),
        annotations=ToolAnnotations(
            read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False
        ),
    )
    def get_scm_actor_binding(project_id: str, repository_id: str, request_id: str) -> dict[str, Any]:
        from firefly_forge.cli import client

        with client() as connection:
            return connection.scm_actor_binding(project_id, repository_id, request_id)

    @server.tool(
        description=prompt_text("mcp/configure_scm_resume_grant"),
        annotations=ToolAnnotations(
            read_only_hint=False, destructive_hint=True, idempotent_hint=True, open_world_hint=True
        ),
    )
    def configure_scm_resume_grant(
        project_id: str, repository_id: str, request: SCMResumeGrantRequest
    ) -> dict[str, Any]:
        from firefly_forge.cli import client

        with client() as connection:
            return connection.configure_scm_resume_grant(project_id, repository_id, request)

    @server.tool(
        description=prompt_text("mcp/get_scm_resume_grant"),
        annotations=ToolAnnotations(
            read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False
        ),
    )
    def get_scm_resume_grant(project_id: str, repository_id: str, request_id: str) -> dict[str, Any]:
        from firefly_forge.cli import client

        with client() as connection:
            return connection.scm_resume_grant(project_id, repository_id, request_id)

    @server.tool(
        description=prompt_text("mcp/get_current_scm_resume_grant"),
        annotations=ToolAnnotations(
            read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False
        ),
    )
    def get_current_scm_resume_grant(project_id: str, repository_id: str, binding_request_id: str) -> dict[str, Any]:
        from firefly_forge.cli import client

        with client() as connection:
            return connection.current_scm_resume_grant(project_id, repository_id, binding_request_id)

    @server.tool(
        description=prompt_text("mcp/configure_scm_polling_resume_grant"),
        annotations=ToolAnnotations(
            read_only_hint=False, destructive_hint=True, idempotent_hint=True, open_world_hint=True
        ),
    )
    def configure_scm_polling_resume_grant(
        project_id: str, repository_id: str, request: SCMPollResumeGrantRequest
    ) -> dict[str, Any]:
        from firefly_forge.cli import client

        with client() as connection:
            return connection.configure_scm_polling_resume_grant(project_id, repository_id, request)

    @server.tool(
        description=prompt_text("mcp/get_scm_polling_resume_grant"),
        annotations=ToolAnnotations(
            read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False
        ),
    )
    def get_scm_polling_resume_grant(project_id: str, repository_id: str, request_id: str) -> dict[str, Any]:
        from firefly_forge.cli import client

        with client() as connection:
            return connection.scm_polling_resume_grant(project_id, repository_id, request_id)

    @server.tool(
        description=prompt_text("mcp/get_current_scm_polling_resume_grant"),
        annotations=ToolAnnotations(
            read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False
        ),
    )
    def get_current_scm_polling_resume_grant(
        project_id: str, repository_id: str, binding_request_id: str
    ) -> dict[str, Any]:
        from firefly_forge.cli import client

        with client() as connection:
            return connection.current_scm_polling_resume_grant(project_id, repository_id, binding_request_id)

    @server.tool(
        description=prompt_text("mcp/review_scm_polling_resume"),
        annotations=ToolAnnotations(
            read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False
        ),
    )
    def review_scm_polling_resume(
        project_id: str, repository_id: str, binding_request_id: str, run_id: str
    ) -> dict[str, Any]:
        from firefly_forge.cli import client

        with client() as connection:
            return connection.review_scm_polling_resume(project_id, repository_id, binding_request_id, run_id)

    @server.tool(
        description=prompt_text("mcp/configure_scm_polling_functional_grant"),
        annotations=ToolAnnotations(
            read_only_hint=False, destructive_hint=True, idempotent_hint=True, open_world_hint=True
        ),
    )
    def configure_scm_polling_functional_grant(
        project_id: str, repository_id: str, request: SCMPollFunctionalGrantRequest
    ) -> dict[str, Any]:
        from firefly_forge.cli import client

        with client() as connection:
            return connection.configure_scm_polling_functional_grant(project_id, repository_id, request)

    @server.tool(
        description=prompt_text("mcp/get_scm_polling_functional_grant"),
        annotations=ToolAnnotations(
            read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False
        ),
    )
    def get_scm_polling_functional_grant(project_id: str, repository_id: str, request_id: str) -> dict[str, Any]:
        from firefly_forge.cli import client

        with client() as connection:
            return connection.scm_polling_functional_grant(project_id, repository_id, request_id)

    @server.tool(
        description=prompt_text("mcp/get_current_scm_polling_functional_grant"),
        annotations=ToolAnnotations(
            read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False
        ),
    )
    def get_current_scm_polling_functional_grant(
        project_id: str, repository_id: str, binding_request_id: str
    ) -> dict[str, Any]:
        from firefly_forge.cli import client

        with client() as connection:
            return connection.current_scm_polling_functional_grant(project_id, repository_id, binding_request_id)

    @server.tool(
        description=prompt_text("mcp/review_scm_polling_functional"),
        annotations=ToolAnnotations(
            read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False
        ),
    )
    def review_scm_polling_functional(project_id: str, repository_id: str, binding_request_id: str) -> dict[str, Any]:
        from firefly_forge.cli import client

        with client() as connection:
            return connection.review_scm_polling_functional(project_id, repository_id, binding_request_id)

    @server.tool(
        description=prompt_text("mcp/configure_scm_product_change_grant"),
        annotations=ToolAnnotations(
            read_only_hint=False, destructive_hint=True, idempotent_hint=True, open_world_hint=True
        ),
    )
    def configure_scm_product_change_grant(
        project_id: str, repository_id: str, request: SCMProductChangeGrantRequest
    ) -> dict[str, Any]:
        from firefly_forge.cli import client

        with client() as connection:
            return connection.configure_scm_product_change_grant(project_id, repository_id, request)

    @server.tool(
        description=prompt_text("mcp/get_scm_product_change_grant"),
        annotations=ToolAnnotations(
            read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False
        ),
    )
    def get_scm_product_change_grant(project_id: str, repository_id: str, request_id: str) -> dict[str, Any]:
        from firefly_forge.cli import client

        with client() as connection:
            return connection.scm_product_change_grant(project_id, repository_id, request_id)

    @server.tool(
        description=prompt_text("mcp/get_current_scm_product_change_grant"),
        annotations=ToolAnnotations(
            read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False
        ),
    )
    def get_current_scm_product_change_grant(
        project_id: str, repository_id: str, binding_request_id: str
    ) -> dict[str, Any]:
        from firefly_forge.cli import client

        with client() as connection:
            return connection.current_scm_product_change_grant(project_id, repository_id, binding_request_id)

    @server.tool(
        description=prompt_text("mcp/get_scm_product_change"),
        annotations=ToolAnnotations(
            read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False
        ),
    )
    def get_scm_product_change(project_id: str, repository_id: str, request_id: str) -> dict[str, Any]:
        from firefly_forge.cli import client

        with client() as connection:
            return connection.scm_product_change(project_id, repository_id, request_id)

    @server.tool(description=prompt_text("mcp/list_identity_users"), annotations=ToolAnnotations(read_only_hint=True))
    def list_identity_users() -> list[dict[str, Any]]:
        from firefly_forge.cli import client

        with client() as connection:
            return connection.identity_users()

    @server.tool(
        description=prompt_text("mcp/find_project_eligible_users"), annotations=ToolAnnotations(read_only_hint=True)
    )
    def find_project_eligible_users(project_id: str, query: EligibleUserQuery) -> list[dict[str, Any]]:
        from firefly_forge.cli import client

        with client() as connection:
            return connection.project_eligible_users(project_id, query)

    @server.tool(
        description=prompt_text("mcp/get_identity_administration"), annotations=ToolAnnotations(read_only_hint=True)
    )
    def get_identity_administration() -> dict[str, Any]:
        from firefly_forge.cli import client

        with client() as connection:
            return connection.identity_administration()

    @server.tool(
        description=prompt_text("mcp/list_identity_operations"), annotations=ToolAnnotations(read_only_hint=True)
    )
    def list_identity_operations() -> list[dict[str, Any]]:
        from firefly_forge.cli import client

        with client() as connection:
            return connection.identity_operations()

    @server.tool(
        description=prompt_text("mcp/set_identity_user_enabled"),
        annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True),
    )
    def set_identity_user_enabled(user_id: str, enabled: IdentityEnabled) -> dict[str, Any]:
        from firefly_forge.cli import client

        with client() as connection:
            return connection.set_identity_user_enabled(user_id, enabled)

    @server.tool(
        description=prompt_text("mcp/provision_identity_user"),
        annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True),
    )
    def provision_identity_user(request: IdentityProvisionRequest) -> dict[str, Any]:
        from firefly_forge.cli import client

        with client() as connection:
            return connection.provision_identity_user(request)

    @server.tool(
        description=prompt_text("mcp/invite_identity_user"),
        annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True),
    )
    def invite_identity_user(user_id: str, request_id: str) -> dict[str, Any]:
        from firefly_forge.cli import client

        with client() as connection:
            return connection.invite_identity_user(user_id, {"request_id": request_id})

    @server.tool(
        description=prompt_text("mcp/reconcile_identity_operation"),
        annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True),
    )
    def reconcile_identity_operation(operation_id: str, request: IdentityReconciliationRequest) -> dict[str, Any]:
        from firefly_forge.cli import client

        with client() as connection:
            return connection.reconcile_identity_operation(operation_id, request)

    @server.tool(
        description=prompt_text("mcp/recover_identity_operation"),
        annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True),
    )
    def recover_identity_operation(operation_id: str, request: IdentityRecoveryRequest) -> dict[str, Any]:
        from firefly_forge.cli import client

        with client() as connection:
            return connection.recover_identity_operation(operation_id, request)

    @server.tool(description=prompt_text("mcp/save_agent_roles"))
    def save_agent_roles(document: dict[str, Any]) -> dict[str, Any]:
        from firefly_forge.cli import client

        with client() as connection:
            return connection.save_agent_roles(document)

    @server.tool(description=prompt_text("mcp/get_repository_agent_roles"))
    def get_repository_agent_roles(repository_id: str) -> dict[str, Any]:
        from firefly_forge.cli import client

        with client() as connection:
            return connection.repository_roles(repository_id)

    @server.tool(description=prompt_text("mcp/save_repository_agent_roles"))
    def save_repository_agent_roles(repository_id: str, roles: dict[str, Any]) -> dict[str, Any]:
        from firefly_forge.cli import client

        with client() as connection:
            return connection.save_repository_roles(repository_id, roles)

    @server.tool(description=prompt_text("mcp/get_worker_messages"))
    def get_worker_messages(run_id: str) -> list[dict[str, Any]]:
        from firefly_forge.cli import client

        with client() as connection:
            return connection.worker_messages(run_id)

    @server.tool(description=prompt_text("mcp/respond_worker_request"))
    def respond_worker_request(
        run_id: str, request_id: str, response_id: str, answer: str, actor: str = "operator"
    ) -> dict[str, Any]:
        from firefly_forge.cli import client

        with client() as connection:
            return connection.respond_worker(run_id, request_id, {"id": response_id, "actor": actor, "answer": answer})

    @server.tool(description=prompt_text("mcp/list_project_campaigns"))
    def list_project_campaigns(project_id: str) -> list[dict[str, Any]]:
        from firefly_forge.cli import client

        with client() as connection:
            return connection.project_campaigns(project_id)

    @server.tool(description=prompt_text("mcp/launch_project_campaign"))
    def launch_project_campaign(project_id: str, request: CampaignLaunch) -> dict[str, Any]:
        from firefly_forge.cli import client

        with client() as connection:
            return connection.launch_campaign(project_id, request)

    @server.tool(description=prompt_text("mcp/get_project_campaign"))
    def get_project_campaign(project_id: str, campaign_id: str) -> dict[str, Any]:
        from firefly_forge.cli import client

        with client() as connection:
            return connection.project_campaign(project_id, campaign_id)

    @server.tool(
        description=prompt_text("mcp/get_project_campaign_report"),
        annotations=ToolAnnotations(
            read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False
        ),
    )
    def get_project_campaign_report(project_id: str, campaign_id: str) -> dict[str, Any]:
        from firefly_forge.cli import client

        with client() as connection:
            return connection.project_campaign_report(project_id, campaign_id)

    @server.tool(
        description=prompt_text("mcp/archive_project_campaign_report"),
        annotations=ToolAnnotations(
            read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=False
        ),
    )
    def archive_project_campaign_report(
        project_id: str, campaign_id: str, request: CampaignReportArchiveRequest
    ) -> dict[str, Any]:
        from firefly_forge.cli import client

        with client() as connection:
            return connection.archive_campaign_report(project_id, campaign_id, request)

    @server.tool(
        description=prompt_text("mcp/get_project_campaign_report_archive"),
        annotations=ToolAnnotations(
            read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False
        ),
    )
    def get_project_campaign_report_archive(project_id: str, campaign_id: str, request_id: str) -> dict[str, Any]:
        from firefly_forge.cli import client

        with client() as connection:
            return connection.campaign_report_archive(project_id, campaign_id, request_id)

    @server.tool(
        description=prompt_text("mcp/list_project_campaign_report_archives"),
        annotations=ToolAnnotations(
            read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False
        ),
    )
    def list_project_campaign_report_archives(
        project_id: str, campaign_id: str, after: CampaignReportArchiveCursor = 0
    ) -> dict[str, Any]:
        from firefly_forge.cli import client

        with client() as connection:
            return connection.campaign_report_archives(project_id, campaign_id, after)

    @server.tool(
        description=prompt_text("mcp/save_project_campaign_delivery_plan"),
        annotations=ToolAnnotations(
            read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=False
        ),
    )
    def save_project_campaign_delivery_plan(
        project_id: str, campaign_id: str, request: CampaignDeliveryPlanRequest
    ) -> dict[str, Any]:
        from firefly_forge.cli import client

        with client() as connection:
            return connection.save_campaign_delivery_plan(project_id, campaign_id, request)

    @server.tool(
        description=prompt_text("mcp/list_project_campaign_delivery_plans"),
        annotations=ToolAnnotations(
            read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False
        ),
    )
    def list_project_campaign_delivery_plans(project_id: str, campaign_id: str) -> dict[str, Any]:
        from firefly_forge.cli import client

        with client() as connection:
            return connection.campaign_delivery_plans(project_id, campaign_id)

    @server.tool(
        description=prompt_text("mcp/get_project_campaign_delivery_plan"),
        annotations=ToolAnnotations(
            read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False
        ),
    )
    def get_project_campaign_delivery_plan(project_id: str, campaign_id: str, request_id: str) -> dict[str, Any]:
        from firefly_forge.cli import client

        with client() as connection:
            return connection.campaign_delivery_plan(project_id, campaign_id, request_id)

    @server.tool(
        description=prompt_text("mcp/get_project_campaign_delivery_progress"),
        annotations=ToolAnnotations(
            read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False
        ),
    )
    def get_project_campaign_delivery_progress(
        project_id: str, campaign_id: str, request_id: str, report_request_id: str
    ) -> dict[str, Any]:
        from firefly_forge.cli import client

        with client() as connection:
            return connection.campaign_delivery_progress(project_id, campaign_id, request_id, report_request_id)

    @server.tool(description=prompt_text("mcp/cancel_project_campaign"))
    def cancel_project_campaign(project_id: str, campaign_id: str, request: CampaignAction) -> dict[str, Any]:
        from firefly_forge.cli import client

        with client() as connection:
            return connection.cancel_campaign(project_id, campaign_id, request)

    @server.tool(description=prompt_text("mcp/resume_project_campaign"))
    def resume_project_campaign(project_id: str, campaign_id: str, request: CampaignAction) -> dict[str, Any]:
        from firefly_forge.cli import client

        with client() as connection:
            return connection.resume_campaign(project_id, campaign_id, request)

    @server.tool(description=prompt_text("mcp/retry_project_campaign"))
    def retry_project_campaign(project_id: str, campaign_id: str, request: CampaignRetry) -> dict[str, Any]:
        from firefly_forge.cli import client

        with client() as connection:
            return connection.retry_campaign(project_id, campaign_id, request)

    @server.tool(description=prompt_text("mcp/get_repository_runner"))
    def get_repository_runner(repository_id: str) -> dict[str, Any]:
        from firefly_forge.cli import client

        with client() as connection:
            return connection.repository_runner(repository_id)

    @server.tool(description=prompt_text("mcp/save_repository_runner"))
    def save_repository_runner(repository_id: str, sandbox: SandboxInput) -> dict[str, Any]:
        from firefly_forge.cli import client

        with client() as connection:
            return connection.save_repository_runner(repository_id, sandbox)

    @server.tool(description=prompt_text("mcp/check_repository_runner"))
    def check_repository_runner(repository_id: str) -> dict[str, Any]:
        from firefly_forge.cli import client

        with client() as connection:
            return connection.runner_readiness(repository_id)

    @server.tool(description=prompt_text("mcp/probe_runner"))
    def probe_runner(sandbox: SandboxInput) -> dict[str, Any]:
        from firefly_forge.cli import client

        with client() as connection:
            return connection.probe_runner(sandbox)

    @server.tool(description=prompt_text("mcp/get_internet_search_configuration"))
    def get_internet_search_configuration() -> dict[str, Any]:
        from firefly_forge.cli import client

        with client() as connection:
            return connection.search_configuration()

    @server.tool(description=prompt_text("mcp/configure_internet_search"))
    def configure_internet_search(
        api_key_file: str | None = None,
        clear_secret: bool = False,
        provider: Literal["brave", "tavily"] | None = None,
        include_domains: list[str] | None = None,
    ) -> dict[str, Any]:
        from pathlib import Path

        from firefly_forge.cli import client, private_secret_file

        if api_key_file and clear_secret:
            raise ValueError("Choose an owned private key file or explicit clear_secret")
        key = private_secret_file(Path(api_key_file)) if api_key_file else None
        with client() as connection:
            return connection.configure_search(
                api_key=key, clear_secret=clear_secret, provider=provider, include_domains=include_domains
            )

    @server.tool(description=prompt_text("mcp/test_internet_search"))
    def test_internet_search() -> dict[str, Any]:
        from firefly_forge.cli import client

        with client() as connection:
            return connection.test_search()

    @server.tool(description=prompt_text("mcp/search_internet"))
    def search_internet(query: str, max_results: int = 5) -> dict[str, Any]:
        from firefly_forge.cli import client

        with client() as connection:
            return connection.internet_search(query, max_results)

    @server.tool(description=prompt_text("mcp/get_search_citation"))
    def get_search_citation(citation_id: str) -> dict[str, Any]:
        from firefly_forge.cli import client

        with client() as connection:
            return connection.search_citation(citation_id)

    @server.tool(description=prompt_text("mcp/get_browser_evidence_image"))
    def get_browser_evidence_image(evidence_id: str) -> Image:
        from firefly_forge.cli import client

        with client() as connection:
            return Image(data=connection.browser_screenshot(evidence_id), format="png")

    @server.tool(description=prompt_text("mcp/get_external_mcp_catalog"))
    def get_external_mcp_catalog() -> list[dict[str, Any]]:
        from firefly_forge.cli import client

        with client() as connection:
            return connection.external_mcp_catalog()

    @server.tool(description=prompt_text("mcp/get_oauth_provider_policies"))
    def get_oauth_provider_policies() -> dict[str, Any]:
        from firefly_forge.cli import client

        with client() as connection:
            return connection.oauth_provider_policies()

    @server.tool(description=prompt_text("mcp/configure_local_oauth_provider_policy"))
    def configure_local_oauth_provider_policy(
        enabled: bool,
        local_origin: str,
        expected_revision: int,
        request_id: str,
    ) -> dict[str, Any]:
        from firefly_forge.cli import client

        with client() as connection:
            return connection.configure_local_oauth_provider_policy(
                {
                    "enabled": enabled,
                    "local_origin": local_origin,
                    "expected_revision": expected_revision,
                    "request_id": request_id,
                }
            )

    @server.tool(description=prompt_text("mcp/get_connection_oauth_providers"))
    def get_connection_oauth_providers() -> list[dict[str, Any]]:
        from firefly_forge.cli import client

        with client() as connection:
            return connection.connection_oauth_providers()

    @server.tool(description=prompt_text("mcp/get_connection_oauth"))
    def get_connection_oauth(name: str) -> dict[str, Any]:
        from firefly_forge.cli import client

        with client() as connection:
            return connection.connection_oauth(name)

    @server.tool(description=prompt_text("mcp/list_connection_oauth"))
    def list_connection_oauth(project_id: str | None = None) -> list[dict[str, Any]]:
        from firefly_forge.cli import client

        with client() as connection:
            return connection.connection_oauth_connections(project_id)

    @server.tool(description=prompt_text("mcp/get_connection_oauth_effect"))
    def get_connection_oauth_effect(name: str, request_id: str) -> dict[str, Any]:
        from firefly_forge.cli import client

        with client() as connection:
            return connection.connection_oauth_effect(name, request_id)

    @server.tool(description=prompt_text("mcp/supersede_connection_oauth"))
    def supersede_connection_oauth(
        name: str,
        expected_revision: int,
        unknown_effect_ids: list[str],
        reason: str,
        acknowledge_unknown: bool,
        provider: str,
        client_id: str,
        client_secret_file: str | None = None,
        public_client: bool | None = None,
        project_id: str | None = None,
        authentication: Literal["client_secret_post", "client_secret_basic"] = "client_secret_post",
    ) -> dict[str, Any]:
        from pathlib import Path

        from firefly_forge.cli import client, private_secret_file

        registration: dict[str, Any] = {
            "provider": provider,
            "client_id": client_id,
            "public_client": public_client,
            "project_id": project_id,
            "authentication": authentication,
        }
        if client_secret_file:
            registration["client_secret"] = private_secret_file(Path(client_secret_file))
        with client() as connection:
            return connection.supersede_connection_oauth(
                name,
                {
                    "expected_revision": expected_revision,
                    "unknown_effect_ids": unknown_effect_ids,
                    "reason": reason,
                    "acknowledge_unknown": acknowledge_unknown,
                    "registration": registration,
                },
            )

    @server.tool(description=prompt_text("mcp/configure_connection_oauth"))
    def configure_connection_oauth(
        name: str,
        provider: str,
        client_id: str,
        client_secret_file: str | None = None,
        public_client: bool | None = None,
        project_id: str | None = None,
        authentication: Literal["client_secret_post", "client_secret_basic"] = "client_secret_post",
        expected_revision: OAuthExpectedRevision = None,
    ) -> dict[str, Any]:
        from pathlib import Path

        from firefly_forge.cli import client, private_secret_file

        request: dict[str, Any] = {
            "provider": provider,
            "client_id": client_id,
            "public_client": public_client,
            "project_id": project_id,
            "authentication": authentication,
        }
        if expected_revision is not None:
            request["expected_revision"] = expected_revision
        if client_secret_file:
            request["client_secret"] = private_secret_file(Path(client_secret_file))
        with client() as connection:
            return connection.configure_connection_oauth(name, request)

    @server.tool(description=prompt_text("mcp/prepare_connection_oauth"))
    def prepare_connection_oauth(name: str) -> dict[str, Any]:
        from firefly_forge.cli import client

        with client() as connection:
            return connection.prepare_connection_oauth(name)

    @server.tool(description=prompt_text("mcp/refresh_connection_oauth"))
    def refresh_connection_oauth(name: str, request_id: str) -> dict[str, Any]:
        from firefly_forge.cli import client

        with client() as connection:
            return connection.refresh_connection_oauth(name, request_id)

    @server.tool(description=prompt_text("mcp/disconnect_connection_oauth"))
    def disconnect_connection_oauth(name: str) -> dict[str, Any]:
        from firefly_forge.cli import client

        with client() as connection:
            return connection.disconnect_connection_oauth(name)

    @server.tool(description=prompt_text("mcp/configure_external_mcp"))
    def configure_external_mcp(
        connector: str, enabled: bool = False, api_key_file: str | None = None, clear_secret: bool = False
    ) -> dict[str, Any]:
        from pathlib import Path

        from firefly_forge.cli import client, private_secret_file

        key = private_secret_file(Path(api_key_file)) if api_key_file else None
        with client() as connection:
            return connection.configure_external_mcp(
                connector, {"enabled": enabled, "api_key": key, "clear_secret": clear_secret}
            )

    @server.tool(description=prompt_text("mcp/link_external_mcp_oauth"))
    def link_external_mcp_oauth(
        connector: str, connection_name: str, project_id: str, enabled: bool = True
    ) -> dict[str, Any]:
        from firefly_forge.cli import client

        with client() as connection:
            return connection.link_external_mcp_oauth(
                connector, {"connection_name": connection_name, "project_id": project_id, "enabled": enabled}
            )

    @server.tool(description=prompt_text("mcp/discover_external_mcp"))
    def discover_external_mcp(connector: str) -> dict[str, Any]:
        from firefly_forge.cli import client

        with client() as connection:
            return connection.discover_external_mcp(connector)

    @server.tool(description=prompt_text("mcp/review_external_mcp"))
    def review_external_mcp(connector: str, request: MCPReviewInput) -> dict[str, Any]:
        from firefly_forge.cli import client

        with client() as connection:
            return connection.review_external_mcp(connector, request.model_dump())

    @server.tool(description=prompt_text("mcp/get_external_mcp_grants"))
    def get_external_mcp_grants(project_id: str) -> list[dict[str, Any]]:
        from firefly_forge.cli import client

        with client() as connection:
            return connection.external_mcp_grants(project_id)

    @server.tool(description=prompt_text("mcp/grant_external_mcp"))
    def grant_external_mcp(project_id: str, connector: str, request: MCPGrantInput) -> dict[str, Any]:
        from firefly_forge.cli import client

        with client() as connection:
            return connection.grant_external_mcp(project_id, connector, request.model_dump())

    @server.tool(description=prompt_text("mcp/revoke_external_mcp"))
    def revoke_external_mcp(project_id: str, grant_id: str) -> dict[str, Any]:
        from firefly_forge.cli import client

        with client() as connection:
            return connection.revoke_external_mcp(project_id, grant_id)

    @server.tool(description=prompt_text("mcp/get_tool_policy"))
    def get_tool_policy() -> dict[str, Any]:
        from firefly_forge.cli import client

        with client() as connection:
            return connection.tool_policy()

    @server.tool(description=prompt_text("mcp/configure_tool_policy"))
    def configure_tool_policy(request: ToolPolicyInput) -> dict[str, Any]:
        from firefly_forge.cli import client

        with client() as connection:
            return connection.configure_tool_policy(request.model_dump())

    @server.tool(description=prompt_text("mcp/get_run_tool_invocations"))
    def get_run_tool_invocations(run_id: str) -> list[dict[str, Any]]:
        from firefly_forge.cli import client

        with client() as connection:
            return connection.tool_invocations(run_id)

    @server.tool(description=prompt_text("mcp/abandon_tool_invocation"))
    def abandon_tool_invocation(run_id: str, correlation_id: str, request_id: str) -> dict[str, Any]:
        from firefly_forge.cli import client

        with client() as connection:
            return connection.abandon_tool_invocation(run_id, correlation_id, request_id)

    @server.tool(description=prompt_text("mcp/get_learning_policy"))
    def get_learning_policy() -> dict[str, Any]:
        from firefly_forge.cli import client

        with client() as connection:
            return connection.learning_policy()

    @server.tool(description=prompt_text("mcp/configure_learning_policy"))
    def configure_learning_policy(request: LearningPolicy) -> dict[str, Any]:
        from firefly_forge.cli import client

        with client() as connection:
            return connection.configure_learning_policy(request.model_dump())

    @server.tool(description=prompt_text("mcp/list_learning_records"))
    def list_learning_records(repository_id: str) -> list[dict[str, Any]]:
        from firefly_forge.cli import client

        with client() as connection:
            return connection.learning_records(repository_id)

    @server.tool(description=prompt_text("mcp/invalidate_learning"))
    def invalidate_learning(record_id: str, reason: str, request_id: str) -> dict[str, Any]:
        from firefly_forge.cli import client

        with client() as connection:
            return connection.invalidate_learning(record_id, reason, request_id)

    @server.tool(
        description=prompt_text("mcp/get_learning_invalidation_receipt"),
        annotations=ToolAnnotations(
            read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False
        ),
    )
    def get_learning_invalidation_receipt(record_id: str, request_id: str) -> dict[str, Any]:
        from firefly_forge.cli import client

        with client() as connection:
            return connection.learning_invalidation_receipt(record_id, request_id)

    @server.tool(
        description=prompt_text("mcp/get_learning_projection_observation_receipt"),
        annotations=ToolAnnotations(
            read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False
        ),
    )
    def get_learning_projection_observation_receipt(record_id: str, request_id: str) -> dict[str, Any]:
        from firefly_forge.cli import client

        with client() as connection:
            return connection.learning_projection_observation_receipt(record_id, request_id)

    @server.tool(description=prompt_text("mcp/get_learning_projection"))
    def get_learning_projection(record_id: str) -> dict[str, Any]:
        from firefly_forge.cli import client

        with client() as connection:
            return connection.learning_projection(record_id)

    @server.tool(description=prompt_text("mcp/submit_learning_projection", version="1.0.1"))
    def submit_learning_projection(record_id: str, request_id: str) -> dict[str, Any]:
        from firefly_forge.cli import client

        with client() as connection:
            return connection.submit_learning_projection(record_id, request_id)

    @server.tool(description=prompt_text("mcp/observe_learning_projection", version="1.0.1"))
    def observe_learning_projection(record_id: str, request_id: str, source_id: str | None = None) -> dict[str, Any]:
        from firefly_forge.cli import client

        with client() as connection:
            return connection.observe_learning_projection(record_id, request_id, source_id)

    @server.tool(
        description=prompt_text("mcp/observe_learning_projection_attempt"),
        annotations=ToolAnnotations(
            read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=True
        ),
    )
    def observe_learning_projection_attempt(record_id: str, request: ObservationAttemptRequest) -> dict[str, Any]:
        from firefly_forge.cli import client

        with client() as connection:
            return connection.observe_learning_projection_attempt(record_id, request)

    @server.tool(
        description=prompt_text("mcp/get_learning_projection_observation_attempt"),
        annotations=ToolAnnotations(
            read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False
        ),
    )
    def get_learning_projection_observation_attempt(record_id: str, attempt_id: str) -> dict[str, Any]:
        from firefly_forge.cli import client

        with client() as connection:
            return connection.learning_projection_observation_attempt(record_id, attempt_id)

    return server


def serve() -> None:
    create_server().run(transport="stdio")
