/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import {
  request,
  type Connection,
  type CustomerProductDefinition,
  type FunctionalFeature,
  type ProductIntegration,
  type ProductSnapshot,
} from "./api";
import type { ProductDraftSession } from "./ProductDrafts";
import { normalizeProductDesign } from "./ProductDesignSystem";
import {
  saveProductDefinition,
  stable,
  type ProductChangeBinding,
} from "./ProductSave";
export type ProductChangeInput = {
  request_id: string;
  base_revision: number;
  base_digest: string;
  reason: string;
  feature_upserts: FunctionalFeature[];
  remove_feature_ids: string[];
  integration_upserts: ProductIntegration[];
  remove_integration_ids: string[];
  append_journeys: string[];
  append_acceptance: string[];
  append_user_stories: string[];
};
export type ProductChangeImpact = {
  changed_feature_ids: string[];
  unchanged_feature_ids: string[];
  unchanged_feature_digests: Record<string, string>;
  changed_integration_ids: string[];
  affected_journeys: string[];
  regression_acceptance_refs: string[];
  reviews_required: string[];
};
export type ProductChangePreview = {
  input: ProductChangeInput;
  binding: ProductChangeBinding;
  body: string;
  editVersion: number;
  definition: CustomerProductDefinition;
  impact: ProductChangeImpact;
};
const wireIntegration = (item: ProductIntegration): ProductIntegration => ({
  ...item,
  endpoint: item.endpoint ?? null,
  service_ref: item.service_ref ?? null,
  header_names: item.header_names ?? [],
  side_effects: item.side_effects ?? false,
  idempotency: {
    ...item.idempotency,
    key_field: item.idempotency.key_field ?? null,
  },
  inputs: item.inputs.map((field) => ({
    ...field,
    required: field.required ?? true,
    description: field.description ?? "",
  })),
  outputs: item.outputs.map((field) => ({
    ...field,
    required: field.required ?? true,
    description: field.description ?? "",
  })),
});
const wireStrings = <T>(value: T): T =>
  JSON.parse(
    JSON.stringify(value, (_key, item: unknown) =>
      typeof item === "string" ? item.trim() : item,
    ),
  ) as T;
const same = (a: unknown, b: unknown) => stable(a) === stable(b);
function inventory<T extends { id: string }>(before: T[], after: T[]) {
  if (
    after.some((item) => !/^[a-z][a-z0-9-]{0,63}$/.test(item.id)) ||
    new Set(after.map((item) => item.id)).size !== after.length
  )
    throw new Error("Each contract needs a unique stable reference.");
  return {
    upserts: after.filter(
      (item) =>
        !same(
          item,
          before.find((old) => old.id === item.id),
        ),
    ),
    removals: before
      .filter((item) => !after.some((next) => next.id === item.id))
      .map((item) => item.id),
  };
}
export function buildProductChange(
  base: ProductSnapshot,
  definition: CustomerProductDefinition,
  requestId: string,
  reason: string,
): ProductChangeInput {
  if (
    !/^[a-f0-9]{32}$/.test(requestId) ||
    !/^[a-f0-9]{64}$/.test(base.digest) ||
    !Number.isSafeInteger(base.revision) ||
    base.revision < 1
  )
    throw new Error(
      "Load a saved product revision before previewing a change.",
    );
  if (!reason.trim() || reason.trim().length > 2000)
    throw new Error("Describe why this contract needs to change.");
  const before = base.definition;
  for (const key of [
    "customer",
    "problem",
    "owners",
    "security_requirements",
    "nonfunctional_requirements",
  ] as const)
    if (!same(before[key], definition[key]))
      throw new Error(
        "Use Save definition for changes to context, owners or requirements.",
      );
  if (
    !same(before.processes ?? [], definition.processes ?? []) ||
    !same(before.ai_origin ?? null, definition.ai_origin ?? null) ||
    !same(
      normalizeProductDesign(before.visual_design),
      normalizeProductDesign(definition.visual_design),
    ) ||
    !same(
      before.architecture ? { ...before.architecture, reviewed: false } : null,
      definition.architecture
        ? { ...definition.architecture, reviewed: false }
        : null,
    ) ||
    (definition.design_review &&
      !same(before.design_review, definition.design_review))
  )
    throw new Error(
      "Use Save definition for changes to processes, design, architecture or proposal source.",
    );
  const append = (old: string[], next: string[]) => {
    if (
      next.length < old.length ||
      !same(old, next.slice(0, old.length)) ||
      new Set(next).size !== next.length
    )
      throw new Error(
        "Incremental changes append journeys, criteria and stories. Use Save definition to replace or reorder them.",
      );
    return next.slice(old.length);
  };
  const features = inventory(before.features ?? [], definition.features ?? []),
    integrations = inventory(
      (before.integrations ?? []).map(wireIntegration),
      (definition.integrations ?? []).map(wireIntegration),
    );
  const result = {
    request_id: requestId,
    base_revision: base.revision,
    base_digest: base.digest,
    reason: reason.trim(),
    feature_upserts: features.upserts,
    remove_feature_ids: features.removals,
    integration_upserts: integrations.upserts,
    remove_integration_ids: integrations.removals,
    append_journeys: append(before.journey, definition.journey),
    append_acceptance: append(before.acceptance, definition.acceptance),
    append_user_stories: append(before.user_stories, definition.user_stories),
  };
  if (
    ![
      result.feature_upserts,
      result.remove_feature_ids,
      result.integration_upserts,
      result.remove_integration_ids,
      result.append_journeys,
      result.append_acceptance,
      result.append_user_stories,
    ].some((items) => items.length)
  )
    throw new Error("There is no incremental contract change to preview.");
  return wireStrings(result);
}
export function currentProductChange(
  session: ProductDraftSession,
  preview: Pick<ProductChangePreview, "input" | "editVersion">,
) {
  return (
    session.active &&
    !session.stale &&
    !session.saveHeld &&
    session.editVersion === preview.editVersion &&
    session.base?.revision === preview.input.base_revision &&
    session.base.digest === preview.input.base_digest
  );
}
async function requestDigest(input: ProductChangeInput) {
  return Array.from(
    new Uint8Array(
      await crypto.subtle.digest(
        "SHA-256",
        new TextEncoder().encode(stable(input)),
      ),
    ),
  )
    .map((byte) => byte.toString(16).padStart(2, "0"))
    .join("");
}
export async function previewProductChange(
  session: ProductDraftSession,
  connection: Connection,
  definition: CustomerProductDefinition,
  reason: string,
): Promise<ProductChangePreview> {
  if (
    !session.active ||
    !session.observed ||
    !session.base ||
    session.stale ||
    session.saveHeld
  )
    throw new Error(
      "Load and settle the current saved definition before previewing a change.",
    );
  const input = buildProductChange(
      session.base,
      definition,
      crypto.randomUUID().replaceAll("-", ""),
      reason,
    ),
    editVersion = session.editVersion;
  const request_digest = await requestDigest(input);
  if (!currentProductChange(session, { input, editVersion }))
    throw new Error("The product editing session changed before previewing.");
  const body = JSON.stringify(input),
    binding = { request_id: input.request_id, request_digest };
  const result = await request<{
    base_revision: number;
    base_digest: string;
    definition: CustomerProductDefinition;
    impact: ProductChangeImpact;
    implementation_status: string;
  }>(
    connection,
    `/repositories/${session.repository}/product/changes/preview`,
    { method: "POST", body },
  );
  const preview = {
    input,
    binding,
    body,
    editVersion,
    definition: result?.definition,
    impact: result?.impact,
  };
  if (!currentProductChange(session, preview))
    throw new Error(
      "The draft or saved revision changed. Preview the current change again.",
    );
  const expected = structuredClone(definition);
  expected.functional_reviewed = false;
  expected.design_review = null;
  if (expected.architecture) expected.architecture.reviewed = false;
  // Existing contracts retain their saved order; upserts do not reorder the inventory.
  for (const key of ["features", "integrations"] as const) {
    const items = definition[key] ?? [],
      old = session.base!.definition[key] ?? [];
    (expected[key] as Array<FunctionalFeature | ProductIntegration>) = [
      ...old
        .filter((item) => items.some((next) => next.id === item.id))
        .map((item) => items.find((next) => next.id === item.id)!),
      ...items.filter(
        (item) => !old.some((previous) => previous.id === item.id),
      ),
    ];
  }
  const normalized = (value: CustomerProductDefinition) => ({
    ...value,
    ai_origin: value.ai_origin ?? null,
    architecture: value.architecture ?? null,
    design_review: value.design_review ?? null,
    processes: value.processes ?? [],
    features: value.features ?? [],
    integrations: (value.integrations ?? []).map(wireIntegration),
    visual_design: normalizeProductDesign(value.visual_design),
  });
  if (
    result.base_revision !== input.base_revision ||
    result.base_digest !== input.base_digest ||
    result.implementation_status !== "not_started" ||
    !same(
      wireStrings(normalized(result.definition)),
      wireStrings(normalized(expected)),
    ) ||
    !result.impact ||
    ![
      result.impact.changed_feature_ids,
      result.impact.unchanged_feature_ids,
      result.impact.changed_integration_ids,
      result.impact.affected_journeys,
      result.impact.regression_acceptance_refs,
      result.impact.reviews_required,
    ].every(
      (items) =>
        Array.isArray(items) && items.every((item) => typeof item === "string"),
    ) ||
    !result.impact.unchanged_feature_digests ||
    Object.values(result.impact.unchanged_feature_digests).some(
      (value) => !/^[a-f0-9]{64}$/.test(value),
    )
  )
    throw new Error(
      "The change preview does not match the current product and submitted patch.",
    );
  return structuredClone(preview);
}
export async function saveProductChange(
  session: ProductDraftSession,
  connection: Connection,
  preview: ProductChangePreview,
) {
  const reviewed = structuredClone(preview);
  if (
    reviewed.body !== JSON.stringify(reviewed.input) ||
    reviewed.binding.request_id !== reviewed.input.request_id ||
    reviewed.binding.request_digest !== (await requestDigest(reviewed.input))
  )
    throw new Error(
      "The reviewed change request changed. Preview the contract again.",
    );
  if (!currentProductChange(session, reviewed))
    throw new Error(
      "The preview is no longer current. Preview the edited contract again.",
    );
  return saveProductDefinition(
    session,
    connection,
    reviewed.definition,
    false,
    {
      body: reviewed.body,
      binding: reviewed.binding,
    },
  );
}
