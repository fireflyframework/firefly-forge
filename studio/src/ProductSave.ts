/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import {
  ApiError,
  request,
  type Connection,
  type CustomerProductDefinition,
  type ProductSnapshot,
} from "./api";
import type { ProductDraftSession } from "./ProductDrafts";

export type ProductSaveAttempt = {
  id: number;
  expected_revision: number;
  expected_digest: string | null;
  definition: CustomerProductDefinition;
  body: string;
  editVersion: number;
  phase: "sending" | "unknown";
};
let sequence = 0;
const stable = (value: unknown): string =>
  JSON.stringify(value, (_key, item: unknown) =>
    item && typeof item === "object" && !Array.isArray(item)
      ? Object.fromEntries(
          Object.entries(item).sort(([a], [b]) => a.localeCompare(b)),
        )
      : item,
  );
function snapshot(value: unknown, repository: string): ProductSnapshot | null {
  if (value === null) return null;
  const item = value as ProductSnapshot;
  const definition = item?.definition;
  if (
    !item ||
    item.repository_id !== repository ||
    !Number.isSafeInteger(item.revision) ||
    item.revision < 1 ||
    typeof item.digest !== "string" ||
    !/^[a-f0-9]{64}$/.test(item.digest) ||
    typeof item.created_at !== "string" ||
    !definition ||
    typeof definition.customer !== "string" ||
    typeof definition.problem !== "string" ||
    ![
      definition.journey,
      definition.user_stories,
      definition.acceptance,
      definition.owners,
      definition.security_requirements,
      definition.nonfunctional_requirements,
    ].every(
      (items) =>
        Array.isArray(items) &&
        items.every((entry) => typeof entry === "string"),
    )
  )
    throw new Error(
      "The saved product response has an invalid repository or definition binding.",
    );
  return structuredClone(item);
}
function active(session: ProductDraftSession) {
  if (!session.active)
    throw new Error("This product editing session has ended.");
}
export async function saveProductDefinition(
  session: ProductDraftSession,
  connection: Connection,
  definition: CustomerProductDefinition,
  retry = false,
): Promise<ProductSnapshot> {
  active(session);
  if (session.saveHeld && !retry)
    throw new Error(
      "Review the retained product save before sending another save.",
    );
  let attempt = session.saveAttempt;
  if (!retry) {
    if (!session.observed || session.stale)
      throw new Error("Load the current saved product before saving.");
    if (session.base) snapshot(session.base, session.repository);
    const input = {
      expected_revision: session.base?.revision ?? 0,
      expected_digest: session.base?.digest ?? null,
      definition: structuredClone(definition),
    };
    attempt = {
      id: ++sequence,
      ...input,
      body: JSON.stringify(input),
      editVersion: session.editVersion,
      phase: "sending",
    };
    session.retainSave(attempt);
  }
  if (!attempt || (retry && attempt.phase !== "unknown"))
    throw new Error("There is no uncertain original save to retry.");
  if (retry) session.retrySave(attempt.id);
  try {
    const value = snapshot(
      await request<unknown>(
        connection,
        `/repositories/${session.repository}/product/conditional`,
        { method: "PUT", body: attempt.body },
      ),
      session.repository,
    );
    active(session);
    if (!value || value.revision !== attempt.expected_revision + 1)
      throw new Error(
        "The conditional save returned an unexpected revision. Inspect the saved definition.",
      );
    if (session.releaseSave(attempt.id))
      session.acceptSaved(value, attempt.editVersion);
    return value;
  } catch (error) {
    if (
      !retry &&
      error instanceof ApiError &&
      error.responseKind === "http" &&
      ([401, 403, 404, 422].includes(error.status) ||
        (error.status === 409 && error.code === "product_save_not_applied"))
    )
      session.releaseSave(attempt.id);
    else session.markSaveUnknown(attempt.id);
    throw error;
  }
}
export async function reconcileProductSave(
  session: ProductDraftSession,
  connection: Connection,
): Promise<ProductSnapshot | null> {
  active(session);
  const attempt = session.saveAttempt;
  if (!attempt || attempt.phase !== "unknown")
    throw new Error("There is no uncertain product save to inspect.");
  const value = snapshot(
    await request<unknown>(
      connection,
      `/repositories/${session.repository}/product`,
      { cache: "no-store" },
    ),
    session.repository,
  );
  if (!session.active || session.saveAttempt?.id !== attempt.id) return null;
  if (!value || value.revision <= attempt.expected_revision) return null;
  // The original conditional write now has a stale cut and cannot overwrite a later edit.
  if (stable(value.definition) === stable(attempt.definition)) {
    session.releaseSave(attempt.id);
    session.acceptSaved(value, attempt.editVersion);
    return value;
  }
  session.observe(value, session.brand);
  session.releaseSave(attempt.id);
  return null;
}
