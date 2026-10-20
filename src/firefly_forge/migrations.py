# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Versioned, additive startup migration for Forge's own PostgreSQL tables."""

from __future__ import annotations

import os

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from firefly_forge.access_persistence import ForgeAccessEventEntity, ForgeMembershipEntity
from firefly_forge.activity_persistence import ForgeEventContentEntity
from firefly_forge.analytics_persistence import ForgeAnalyticsBindingEntity, ForgeAnalyticsConfigurationReceiptEntity
from firefly_forge.application_persistence import ForgeIncidentEntity, ForgeObservationEntity
from firefly_forge.assistant_attachment_persistence import ForgeAssistantAttachmentEntity
from firefly_forge.assistant_persistence import (
    ForgeAssistantEventEntity,
    ForgeAssistantSessionEntity,
    ForgeAssistantTurnEntity,
)
from firefly_forge.campaign_delivery_plan_persistence import ForgeCampaignDeliveryPlanEntity
from firefly_forge.campaign_report_history_persistence import ForgeCampaignReportArchiveEntity
from firefly_forge.collaboration_persistence import (
    ForgeChannelInboxEntity,
    ForgeCollaborationSettingsEntity,
    ForgeProductDefinitionEntity,
    ForgeWebhookInboxEntity,
)
from firefly_forge.connection_oauth_persistence import (
    OAuthAttemptEntity,
    OAuthConnectionEntity,
    OAuthCredentialEvidenceEntity,
    OAuthEffectEntity,
    OAuthEventEntity,
    OAuthGrantEntity,
    OAuthRegistrationEntity,
)
from firefly_forge.execution_request_persistence import ForgeExecutionRequestEntity, ForgeExecutionRequestEventEntity
from firefly_forge.identity_persistence import (
    ForgeIdentityAttemptEntity,
    ForgeIdentitySessionEntity,
    ForgeIdentityUserEntity,
)
from firefly_forge.knowledge_persistence import ForgeKnowledgeUploadEntity
from firefly_forge.learning_observation_attempt_persistence import (
    ForgeLearningObservationAttemptEntity,
    observation_attempt_guards,
)
from firefly_forge.learning_persistence import (
    ForgeLearningEventEntity,
    ForgeLearningProjectionEntity,
    ForgeLearningRecordEntity,
)
from firefly_forge.oauth_provider_policy_persistence import (
    OAuthProviderPolicyEntity,
    OAuthProviderPolicyOperationEntity,
    OAuthProviderPolicyRevisionEntity,
)
from firefly_forge.persistence import (
    ForgeDeploymentEntity,
    ForgeEventEntity,
    ForgeJournalEntity,
    ForgePlanEntity,
    ForgeRepositoryEntity,
    ForgeRunEntity,
    ForgeTaskEntity,
)
from firefly_forge.project_campaign_persistence import (
    ForgeCampaignChildEntity,
    ForgeCampaignEntity,
    ForgeCampaignEventEntity,
)
from firefly_forge.project_persistence import ForgeAnalysisEntity, ForgeProjectEntity, ForgeProjectRevisionEntity
from firefly_forge.run_creation_persistence import (
    ForgeRunCreationCancellationEntity,
    ForgeRunCreationRequestEntity,
    ForgeRunCreationReservationEntity,
)
from firefly_forge.runtime_role_persistence import ForgeRuntimeTopologyEntity
from firefly_forge.sandbox_persistence import ForgeSandboxEntity
from firefly_forge.tool_persistence import ForgeToolInvocationEntity
from firefly_forge.user_administration_persistence import (
    ForgeIdentityAdminEventEntity,
    ForgeIdentityAdminOperationEntity,
)

SCHEMA_REVISION = 30


async def migrate(session: AsyncSession, schema: str) -> None:
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"), {"key": "forge:migration:" + schema}
    )
    await session.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{schema}"'))
    await session.execute(text(f'SET LOCAL search_path TO "{schema}"'))
    await session.execute(
        text(
            "CREATE TABLE IF NOT EXISTS forge_schema_migrations "
            "(revision INTEGER PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
        )
    )
    version = await session.scalar(text("SELECT max(revision) FROM forge_schema_migrations"))
    if version is not None and version > SCHEMA_REVISION:
        raise RuntimeError("The PostgreSQL schema requires a newer Forge release")
    if version is None or version < SCHEMA_REVISION:
        connection = await session.connection()
        tables = [
            ForgeRunEntity.metadata.tables[entity.__tablename__]
            for entity in (
                ForgeRuntimeTopologyEntity,
                ForgeExecutionRequestEntity,
                ForgeExecutionRequestEventEntity,
                ForgeRunCreationRequestEntity,
                ForgeRunCreationReservationEntity,
                ForgeRunCreationCancellationEntity,
                ForgeRepositoryEntity,
                ForgeRunEntity,
                ForgeEventEntity,
                ForgeEventContentEntity,
                ForgeDeploymentEntity,
                ForgePlanEntity,
                ForgeTaskEntity,
                ForgeJournalEntity,
                ForgeObservationEntity,
                ForgeIncidentEntity,
                ForgeCollaborationSettingsEntity,
                ForgeWebhookInboxEntity,
                ForgeProductDefinitionEntity,
                ForgeChannelInboxEntity,
                ForgeKnowledgeUploadEntity,
                ForgeProjectEntity,
                ForgeAnalyticsBindingEntity,
                ForgeAnalyticsConfigurationReceiptEntity,
                ForgeProjectRevisionEntity,
                ForgeAnalysisEntity,
                ForgeAssistantSessionEntity,
                ForgeAssistantTurnEntity,
                ForgeAssistantEventEntity,
                ForgeAssistantAttachmentEntity,
                ForgeSandboxEntity,
                ForgeCampaignEntity,
                ForgeCampaignChildEntity,
                ForgeCampaignEventEntity,
                ForgeCampaignReportArchiveEntity,
                ForgeCampaignDeliveryPlanEntity,
                ForgeIdentityUserEntity,
                ForgeIdentitySessionEntity,
                ForgeIdentityAttemptEntity,
                ForgeMembershipEntity,
                ForgeAccessEventEntity,
                ForgeIdentityAdminOperationEntity,
                ForgeIdentityAdminEventEntity,
                ForgeToolInvocationEntity,
                ForgeLearningRecordEntity,
                ForgeLearningEventEntity,
                ForgeLearningProjectionEntity,
                ForgeLearningObservationAttemptEntity,
                OAuthConnectionEntity,
                OAuthRegistrationEntity,
                OAuthAttemptEntity,
                OAuthGrantEntity,
                OAuthEffectEntity,
                OAuthEventEntity,
                OAuthCredentialEvidenceEntity,
                OAuthProviderPolicyEntity,
                OAuthProviderPolicyRevisionEntity,
                OAuthProviderPolicyOperationEntity,
            )
        ]
        await connection.run_sync(lambda sync: ForgeRunEntity.metadata.create_all(sync, tables=tables))
        await session.execute(
            text("ALTER TABLE forge_identity_users ADD COLUMN IF NOT EXISTS contact_email VARCHAR(320)")
        )
        await session.execute(
            text(
                "ALTER TABLE forge_identity_users "
                "ADD COLUMN IF NOT EXISTS api_roles JSONB NOT NULL DEFAULT '[]'::jsonb, "
                "ADD COLUMN IF NOT EXISTS api_roles_audience VARCHAR(2048), "
                "ADD COLUMN IF NOT EXISTS api_roles_issued_at DOUBLE PRECISION, "
                "ADD COLUMN IF NOT EXISTS api_roles_expires_at TIMESTAMPTZ"
            )
        )
        await session.execute(
            text(
                "ALTER TABLE forge_assistant_turns "
                "ADD COLUMN IF NOT EXISTS owner_id VARCHAR(32), "
                "ADD COLUMN IF NOT EXISTS claim_epoch BIGINT NOT NULL DEFAULT 0, "
                "ADD COLUMN IF NOT EXISTS lease_expires_at TIMESTAMPTZ, "
                "ADD COLUMN IF NOT EXISTS last_heartbeat_at TIMESTAMPTZ"
            )
        )
        issuer = os.environ.get("FORGE_OIDC_ISSUER")
        if issuer:
            await session.execute(
                text(
                    "UPDATE forge_identity_users AS u SET "
                    "contact_email=p.data #>> '{request,email}', "
                    "display_name=COALESCE(NULLIF(u.display_name,''), "
                    "NULLIF(left(trim(concat_ws(' ',p.data #>> '{request,first_name}', "
                    "p.data #>> '{request,last_name}')),200),''), "
                    "left(p.data #>> '{request,username}',200)) "
                    "FROM forge_identity_admin_operations AS p "
                    "WHERE u.issuer=:issuer AND u.contact_email IS NULL "
                    "AND p.kind='provision' AND p.status='completed' "
                    "AND p.data->>'user_id'=replace(u.id::text,'-','') "
                    "AND p.data->>'subject'=u.subject "
                    "AND length(p.data #>> '{request,email}') BETWEEN 3 AND 320 "
                    "AND p.data #>> '{request,email}' ~ '^[^@[:space:]]+@[^@[:space:]]+[.][^@[:space:]]+$' "
                    "AND EXISTS (SELECT 1 FROM forge_identity_admin_events AS e "
                    "WHERE e.operation_id=p.id AND e.data->>'kind'='remote_receipt' "
                    "AND e.data #>> '{payload,status}'='completed' "
                    "AND e.data #>> '{payload,user_id}'=replace(u.id::text,'-','') "
                    "AND e.data #>> '{payload,subject}'=u.subject)"
                ),
                {"issuer": issuer},
            )
        await session.execute(
            text(
                "CREATE OR REPLACE FUNCTION forge_refuse_immutable_change() "
                "RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN "
                "RAISE EXCEPTION 'Forge execution history is immutable'; END; $$"
            )
        )
        for table in (
            "forge_runtime_topology",
            "forge_execution_request_events",
            "forge_run_creation_requests",
            "forge_run_creation_reservations",
            "forge_run_creation_cancellations",
            "forge_plans",
            "forge_journal",
            "forge_observations",
            "forge_product_definitions",
            "forge_project_revisions",
            "forge_analytics_configuration_receipts",
            "forge_assistant_events",
            "forge_assistant_attachments",
            "forge_event_content",
            "forge_campaign_children",
            "forge_campaign_events",
            "forge_campaign_report_archives",
            "forge_campaign_delivery_plan_revisions",
            "forge_access_events",
            "forge_identity_admin_events",
            "forge_learning_events",
        ):
            await session.execute(
                text(
                    f"CREATE OR REPLACE TRIGGER {table}_immutable "
                    f"BEFORE UPDATE OR DELETE ON {table} FOR EACH ROW "
                    "EXECUTE FUNCTION forge_refuse_immutable_change()"
                )
            )
        await session.execute(
            text("""
            CREATE OR REPLACE FUNCTION forge_guard_execution_request() RETURNS trigger AS $$
            BEGIN
              IF OLD.run_id IS DISTINCT FROM NEW.run_id OR OLD.kind IS DISTINCT FROM NEW.kind
                OR OLD.request_key IS DISTINCT FROM NEW.request_key
                OR OLD.data->'request' IS DISTINCT FROM NEW.data->'request'
                OR OLD.data->'request_digest' IS DISTINCT FROM NEW.data->'request_digest' THEN
                RAISE EXCEPTION 'Execution request admission is immutable';
              END IF;
              RETURN NEW;
            END; $$ LANGUAGE plpgsql
        """)
        )
        await session.execute(
            text(
                "CREATE OR REPLACE TRIGGER forge_execution_request_admission "
                "BEFORE UPDATE ON forge_execution_requests FOR EACH ROW "
                "EXECUTE FUNCTION forge_guard_execution_request()"
            )
        )
        await session.execute(
            text("INSERT INTO forge_schema_migrations(revision) VALUES (:revision)"), {"revision": SCHEMA_REVISION}
        )
        await session.execute(
            text("""
            CREATE OR REPLACE FUNCTION forge_guard_tool_invocation() RETURNS trigger AS $$
            BEGIN
              IF OLD.run_id IS DISTINCT FROM NEW.run_id OR OLD.task_id IS DISTINCT FROM NEW.task_id
                OR OLD.tool IS DISTINCT FROM NEW.tool OR OLD.correlation_id IS DISTINCT FROM NEW.correlation_id
                OR OLD.data->'admission' IS DISTINCT FROM NEW.data->'admission'
                OR OLD.data->'request_digest' IS DISTINCT FROM NEW.data->'request_digest'
                OR (OLD.status IN ('succeeded','rejected','abandoned')
                  AND (OLD.status IS DISTINCT FROM NEW.status OR OLD.data IS DISTINCT FROM NEW.data)) THEN
                RAISE EXCEPTION 'Tool invocation admission and completed outcome are immutable';
              END IF;
              RETURN NEW;
            END; $$ LANGUAGE plpgsql
        """)
        )
        await session.execute(
            text(
                "CREATE OR REPLACE TRIGGER forge_tool_invocation_admission "
                "BEFORE UPDATE ON forge_tool_invocations FOR EACH ROW EXECUTE FUNCTION forge_guard_tool_invocation()"
            )
        )
        await session.execute(
            text(
                "CREATE OR REPLACE TRIGGER forge_tool_invocation_delete "
                "BEFORE DELETE ON forge_tool_invocations FOR EACH ROW EXECUTE FUNCTION forge_refuse_immutable_change()"
            )
        )

        await session.execute(
            text("""
            CREATE OR REPLACE FUNCTION forge_guard_learning_record() RETURNS trigger AS $$
            BEGIN
              IF OLD.run_id IS DISTINCT FROM NEW.run_id OR OLD.repository_id IS DISTINCT FROM NEW.repository_id
                OR OLD.correlation_id IS DISTINCT FROM NEW.correlation_id OR OLD.kind IS DISTINCT FROM NEW.kind
                OR OLD.data IS DISTINCT FROM NEW.data THEN
                RAISE EXCEPTION 'Learning origin and evidence are immutable';
              END IF;
              IF OLD.state IS DISTINCT FROM NEW.state AND NOT (
                OLD.state IN ('candidate','verified') AND NEW.state IN ('invalidated','superseded')
              ) THEN RAISE EXCEPTION 'Learning state transition requires native evidence'; END IF;
              RETURN NEW;
            END; $$ LANGUAGE plpgsql
        """)
        )
        await session.execute(
            text(
                "CREATE OR REPLACE TRIGGER forge_learning_admission BEFORE UPDATE ON forge_learning_records "
                "FOR EACH ROW EXECUTE FUNCTION forge_guard_learning_record()"
            )
        )
        await session.execute(
            text(
                "CREATE OR REPLACE TRIGGER forge_learning_delete BEFORE DELETE ON forge_learning_records "
                "FOR EACH ROW EXECUTE FUNCTION forge_refuse_immutable_change()"
            )
        )

        await session.execute(
            text("""
            CREATE OR REPLACE FUNCTION forge_guard_learning_projection() RETURNS trigger AS $$
            BEGIN
              IF OLD.record_id IS DISTINCT FROM NEW.record_id
                OR OLD.payload_digest IS DISTINCT FROM NEW.payload_digest
                OR OLD.data->'payload' IS DISTINCT FROM NEW.data->'payload'
                OR (OLD.data ? 'target' AND (OLD.data->'target' IS DISTINCT FROM NEW.data->'target'
                    OR OLD.data->'workspace_id' IS DISTINCT FROM NEW.data->'workspace_id'
                    OR OLD.data->'content_sha256' IS DISTINCT FROM NEW.data->'content_sha256'
                    OR OLD.data->'request_id' IS DISTINCT FROM NEW.data->'request_id'))
                OR (OLD.state IN ('verified','abandoned') AND
                    (OLD.state IS DISTINCT FROM NEW.state OR OLD.data IS DISTINCT FROM NEW.data)) THEN
                RAISE EXCEPTION 'Learning projection admission and final outcome are immutable';
              END IF;
              IF OLD.state IS DISTINCT FROM NEW.state AND NOT (
                (OLD.state='pending' AND NEW.state='executing') OR
                (OLD.state='executing' AND NEW.state IN ('source_admitted','unknown','verified')) OR
                (OLD.state IN ('source_admitted','unknown') AND NEW.state IN ('verified','abandoned'))
              ) THEN RAISE EXCEPTION 'Learning projection transition cannot replay a submission'; END IF;
              RETURN NEW;
            END; $$ LANGUAGE plpgsql
        """)
        )
        await session.execute(
            text(
                "CREATE OR REPLACE TRIGGER forge_learning_projection_admission "
                "BEFORE UPDATE ON forge_learning_projections FOR EACH ROW "
                "EXECUTE FUNCTION forge_guard_learning_projection()"
            )
        )
        await session.execute(
            text(
                "CREATE OR REPLACE TRIGGER forge_learning_projection_delete "
                "BEFORE DELETE ON forge_learning_projections FOR EACH ROW "
                "EXECUTE FUNCTION forge_refuse_immutable_change()"
            )
        )

    await _oauth_history_guards(session)
    await _oauth_provider_policy_guards(session)
    await observation_attempt_guards(session)


async def _oauth_history_guards(session: AsyncSession) -> None:
    for table in ("forge_oauth_registrations", "forge_oauth_events", "forge_oauth_credential_evidence"):
        await session.execute(
            text(
                f"CREATE OR REPLACE TRIGGER {table}_immutable "
                f"BEFORE UPDATE OR DELETE ON {table} FOR EACH ROW "
                "EXECUTE FUNCTION forge_refuse_immutable_change()"
            )
        )
    await session.execute(
        text("""
        CREATE OR REPLACE FUNCTION forge_guard_oauth_effect() RETURNS trigger AS $$
        BEGIN
          IF OLD.id IS DISTINCT FROM NEW.id OR OLD.connection_id IS DISTINCT FROM NEW.connection_id
            OR OLD.request_id IS DISTINCT FROM NEW.request_id OR OLD.kind IS DISTINCT FROM NEW.kind
            OR OLD.created_at IS DISTINCT FROM NEW.created_at
            OR OLD.lease_expires_at IS DISTINCT FROM NEW.lease_expires_at
            OR (OLD.data - ARRAY['result','observed_at','disposition']) IS DISTINCT FROM
               (NEW.data - ARRAY['result','observed_at','disposition'])
            OR (OLD.state IN ('completed','rejected','unknown') AND
              (OLD.state IS DISTINCT FROM NEW.state OR OLD.data IS DISTINCT FROM NEW.data)) THEN
            RAISE EXCEPTION 'OAuth effect admission and closed outcome are immutable';
          END IF;
          IF OLD.state IS DISTINCT FROM NEW.state AND NOT (
            OLD.state='executing' AND NEW.state IN ('completed','rejected','unknown')
          ) THEN RAISE EXCEPTION 'OAuth effect transition cannot replay a request'; END IF;
          RETURN NEW;
        END; $$ LANGUAGE plpgsql
        """)
    )
    await session.execute(
        text(
            "CREATE OR REPLACE TRIGGER forge_oauth_effect_admission "
            "BEFORE UPDATE ON forge_oauth_effects FOR EACH ROW EXECUTE FUNCTION forge_guard_oauth_effect()"
        )
    )
    await session.execute(
        text(
            "CREATE OR REPLACE TRIGGER forge_oauth_effect_delete "
            "BEFORE DELETE ON forge_oauth_effects FOR EACH ROW EXECUTE FUNCTION forge_refuse_immutable_change()"
        )
    )


async def _oauth_provider_policy_guards(session: AsyncSession) -> None:
    for table in ("forge_oauth_provider_policy_revisions", "forge_oauth_provider_policy_operations"):
        await session.execute(
            text(
                f"CREATE OR REPLACE TRIGGER {table}_immutable "
                f"BEFORE UPDATE OR DELETE ON {table} FOR EACH ROW "
                "EXECUTE FUNCTION forge_refuse_immutable_change()"
            )
        )
    await session.execute(
        text("""
        CREATE OR REPLACE FUNCTION forge_guard_oauth_provider_policy() RETURNS trigger AS $$
        BEGIN
          IF OLD.id IS DISTINCT FROM NEW.id OR OLD.name IS DISTINCT FROM NEW.name
            OR NEW.revision <> OLD.revision + 1 THEN
            RAISE EXCEPTION 'Provider policy identity is immutable and revisions must advance';
          END IF;
          RETURN NEW;
        END; $$ LANGUAGE plpgsql
    """)
    )
    await session.execute(
        text(
            "CREATE OR REPLACE TRIGGER forge_oauth_provider_policy_revision "
            "BEFORE UPDATE ON forge_oauth_provider_policies FOR EACH ROW "
            "EXECUTE FUNCTION forge_guard_oauth_provider_policy()"
        )
    )
    await session.execute(
        text(
            "CREATE OR REPLACE TRIGGER forge_oauth_provider_policy_delete "
            "BEFORE DELETE ON forge_oauth_provider_policies FOR EACH ROW "
            "EXECUTE FUNCTION forge_refuse_immutable_change()"
        )
    )
