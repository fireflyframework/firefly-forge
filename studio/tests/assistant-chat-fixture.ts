/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { expect, type Route } from "@playwright/test";

export function assistantChatFixture(
  repository: string,
  mode: "empty" | "recorded" | "busy" = "recorded",
) {
  const conversation = "b".repeat(32),
    turnId = "d".repeat(32);
  const file = {
    id: "f".repeat(32),
    request_id: "e".repeat(32),
    filename: "reservation-notes.md",
    mime_type: "text/markdown",
    kind: "document",
    content_bytes: 240,
    sha256: "e".repeat(64),
    status: "ready",
    text_chars: 240,
    truncated: false,
    created_at: "2026-10-22T10:00:00Z",
  };
  const record = {
    id: conversation,
    repository_id: repository,
    agent: "fixture-agent",
    task: "product",
    created_at: "2026-10-22T10:00:00Z",
    execution_config: { model: "fixture-text-model" },
  };
  const reply = {
    message:
      "Reserve stock only after checking the available quantity. Keep the reservation and the remaining quantity in the same transaction.\n\n```python\nasync def reserve(sku: str, quantity: int):\n    return await inventory.reserve(sku, quantity)\n# " +
      "long-code-example-".repeat(24) +
      "\n```\n\nReview the verification result before approving the change.",
    questions: ["Who can release an expired reservation?"],
    assumptions: [],
    citation_ids: [],
    proposal: null,
  };
  const state = {
    created: 0,
    sent: 0,
    reads: 0,
    createOutcome: "completed" as "completed" | "rejected" | "uncertain",
    turnOutcome: "completed" as "completed" | "uncertain",
    creationWait: Promise.resolve(),
    sessions: (mode === "empty" ? [] : [record]) as Record<string, unknown>[],
    turns: (mode === "empty"
      ? []
      : [
          {
            id: turnId,
            request_id: "c".repeat(32),
            message: "Outline safe inventory reservations.",
            status: mode === "busy" ? "processing" : "completed",
            created_at: record.created_at,
            reply: mode === "busy" ? null : reply,
            context: { attachments: [file] },
          },
        ]) as Record<string, unknown>[],
    turnBodies: [] as Record<string, unknown>[],
  };
  async function handle(route: Route) {
    const req = route.request(),
      path = new URL(req.url()).pathname;
    if (!path.includes(`/repositories/${repository}/assistant/conversations`))
      return false;
    let json: unknown = [];
    if (path.endsWith("/assistant/conversations")) {
      if (req.method() === "POST") {
        state.created++;
        const body = req.postDataJSON();
        expect(Object.keys(body).sort()).toEqual(
          body.project_id ? ["project_id", "task"] : ["task"],
        );
        await state.creationWait;
        if (state.createOutcome === "rejected") {
          await route.fulfill({
            status: 422,
            json: { detail: "Choose a supported task." },
          });
          return true;
        }
        const value = {
          ...record,
          task: body.task,
          ...(body.project_id ? { project_id: body.project_id } : {}),
        };
        state.sessions = [value];
        if (state.createOutcome === "uncertain") {
          await route.abort("failed");
          return true;
        }
        json = value;
      } else {
        state.reads++;
        json = state.sessions;
      }
    } else if (
      state.sessions.some((item) =>
        path.endsWith(`/assistant/conversations/${item.id}`),
      )
    )
      json = {
        ...state.sessions.find((item) => path.endsWith(`/${item.id}`)),
        turns: path.endsWith(`/${conversation}`) ? state.turns : [],
        journal: { verified: true, head: "a".repeat(64), entries: [] },
      };
    else if (path.endsWith("/capabilities"))
      json = { image_input: { status: "unverified" } };
    else if (path.endsWith("/attachments")) json = [file];
    else if (path.endsWith("/turns")) {
      state.sent++;
      const body = req.postDataJSON();
      state.turnBodies.push(body);
      expect(body.request_id).toMatch(/^[a-f0-9]{32}$/);
      if (state.turnOutcome === "uncertain") {
        await route.abort("failed");
        return true;
      }
      const turn = {
        id: turnId,
        request_id: body.request_id,
        message: body.message,
        status: "completed",
        created_at: record.created_at,
        reply,
      };
      state.turns = [turn];
      json = turn;
    }
    await route.fulfill({ json });
    return true;
  }
  return { state, handle, conversation, file };
}
