/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import type { ProductDraftBook } from "./ProductDrafts";

export type ReviewedSourceContext = {
  project_id: string;
  analysis_id: string;
  analysis_digest: string;
  analysis_component_ids: string[];
};
export type ReviewedSourceSnapshot = {
  project_id: string;
  analysis_id: string;
  analysis_digest: string;
  context_digest: string;
  source_reviewed: true;
  business_journeys_confirmed: false;
  live_behavior_verified: false;
  components: { id: string }[];
  included_facts: number;
  omitted_facts: number;
  truncated: boolean;
  citations: { id: string; path: string; line: number; commit: string }[];
};
const nativeId = /^[a-f0-9]{32}$/;
const digest = /^[a-f0-9]{64}$/;
const componentId = /^[a-z][a-z0-9-]{0,63}$/;
const componentIds = (ids: unknown): ids is string[] =>
  Array.isArray(ids) &&
  ids.length > 0 &&
  ids.every((id) => typeof id === "string" && componentId.test(id)) &&
  new Set(ids).size === ids.length;

export function reviewedSourcePayload(
  source: ReviewedSourceContext,
  projectId?: string,
): ReviewedSourceContext {
  if (
    !nativeId.test(source.project_id) ||
    !nativeId.test(source.analysis_id) ||
    !digest.test(source.analysis_digest) ||
    source.project_id !== projectId ||
    !componentIds(source.analysis_component_ids)
  )
    throw new Error(
      "Review the source report and its project before creating a conversation.",
    );
  return {
    project_id: source.project_id,
    analysis_id: source.analysis_id,
    analysis_digest: source.analysis_digest,
    analysis_component_ids: [...source.analysis_component_ids],
  };
}

export function reviewedSourceSnapshot(
  value: unknown,
): ReviewedSourceSnapshot | null {
  if (!value || typeof value !== "object") return null;
  const source = value as ReviewedSourceSnapshot;
  if (
    !nativeId.test(source.project_id) ||
    !nativeId.test(source.analysis_id) ||
    !digest.test(source.analysis_digest) ||
    !digest.test(source.context_digest) ||
    source.source_reviewed !== true ||
    source.business_journeys_confirmed !== false ||
    source.live_behavior_verified !== false ||
    !Array.isArray(source.components) ||
    !componentIds(source.components.map((item) => item?.id)) ||
    !Number.isInteger(source.included_facts) ||
    source.included_facts < 0 ||
    !Number.isInteger(source.omitted_facts) ||
    source.omitted_facts < 0 ||
    source.truncated !== source.omitted_facts > 0 ||
    !Array.isArray(source.citations) ||
    !source.citations.every(
      (item) =>
        item &&
        typeof item.id === "string" &&
        typeof item.path === "string" &&
        Number.isInteger(item.line) &&
        item.line > 0 &&
        typeof item.commit === "string",
    )
  )
    return null;
  return source;
}

export function matchesReviewedSource(
  value: unknown,
  source: ReviewedSourceContext,
): boolean {
  const snapshot = reviewedSourceSnapshot(value);
  const ids = new Set(source.analysis_component_ids);
  return Boolean(
    snapshot &&
    snapshot.project_id === source.project_id &&
    snapshot.analysis_id === source.analysis_id &&
    snapshot.analysis_digest === source.analysis_digest &&
    snapshot.components.length === ids.size &&
    snapshot.components.every((item) => ids.has(item.id)),
  );
}

type RequestState = {
  creationUncertain: boolean;
  uncertainRequests: Readonly<Record<string, string>>;
};
export class AssistantRequestCustody {
  private state: RequestState = {
    creationUncertain: false,
    uncertainRequests: {},
  };
  private readonly listeners = new Set<() => void>();
  readonly getSnapshot = () => this.state;
  readonly subscribe = (listener: () => void) => {
    this.listeners.add(listener);
    return () => {
      this.listeners.delete(listener);
    };
  };
  private update(next: RequestState) {
    this.state = next;
    for (const listener of this.listeners) listener();
  }
  markCreationPending() {
    if (this.state.creationUncertain)
      throw new Error(
        "Inspect the unconfirmed conversation before creating another.",
      );
    this.update({ ...this.state, creationUncertain: true });
  }
  confirmCreation() {
    this.update({ ...this.state, creationUncertain: false });
  }
  selectRecordedConversation() {
    this.confirmCreation();
  }
  markTurnPending(conversation: string, request: string) {
    if (this.state.uncertainRequests[conversation])
      throw new Error("Inspect the recorded request before sending another.");
    this.update({
      ...this.state,
      uncertainRequests: {
        ...this.state.uncertainRequests,
        [conversation]: request,
      },
    });
  }
  confirmTurn(conversation: string, request: string) {
    if (this.state.uncertainRequests[conversation] !== request) return;
    const next = { ...this.state.uncertainRequests };
    delete next[conversation];
    this.update({ ...this.state, uncertainRequests: next });
  }
}

// The book already follows the API/identity scope; draft replacement must not erase request custody.
const books = new WeakMap<
  ProductDraftBook,
  Map<string, AssistantRequestCustody>
>();
export function assistantRequestCustody(
  book: ProductDraftBook,
  repository: string,
  projectId?: string,
) {
  let scopes = books.get(book);
  if (!scopes) {
    scopes = new Map();
    books.set(book, scopes);
  }
  const key = JSON.stringify([repository, projectId ?? null]);
  let custody = scopes.get(key);
  if (!custody) {
    custody = new AssistantRequestCustody();
    scopes.set(key, custody);
  }
  return custody;
}
