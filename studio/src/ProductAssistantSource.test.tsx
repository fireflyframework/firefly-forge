// @vitest-environment happy-dom
/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { afterEach, expect, it, vi } from "vitest";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { ProductAssistant } from "./ProductAssistant";
import { ProductDraftBook } from "./ProductDrafts";
import {
  assistantRequestCustody,
  type ReviewedSourceContext,
} from "./SourceAnalysisContext";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});
const repository = "a".repeat(32);
const source: ReviewedSourceContext = {
  project_id: "b".repeat(32),
  analysis_id: "c".repeat(32),
  analysis_digest: "d".repeat(64),
  analysis_component_ids: ["stock-api"],
};
function snapshot(context = source) {
  return {
    project_id: context.project_id,
    analysis_id: context.analysis_id,
    analysis_digest: context.analysis_digest,
    context_digest: "e".repeat(64),
    source_reviewed: true,
    business_journeys_confirmed: false,
    live_behavior_verified: false,
    components: context.analysis_component_ids.map((id) => ({ id })),
    included_facts: 3,
    omitted_facts: 2,
    truncated: true,
    citations: [
      {
        id: "as-is-receipt",
        path: "api/stock.py",
        line: 8,
        commit: "f".repeat(40),
      },
    ],
  };
}
function fixture() {
  const calls: { path: string; body: Record<string, unknown> }[] = [];
  const older = {
    id: "1".repeat(32),
    repository_id: repository,
    project_id: source.project_id,
    agent: "bridge",
    task: "product",
    created_at: "2026-10-31T10:00:00Z",
    turns: [],
    analysis_snapshot: snapshot({ ...source, analysis_id: "2".repeat(32) }),
  };
  let created: Record<string, unknown> | null = null;
  const options = { wait: Promise.resolve(), loseTurn: false };
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init?: RequestInit) => {
      const path = String(url);
      let value: unknown;
      if (init?.method === "POST") {
        const body = JSON.parse(init.body as string);
        calls.push({ path, body });
        if (path.endsWith("/conversations")) {
          await options.wait;
          created = {
            ...older,
            id: "3".repeat(32),
            analysis_snapshot: snapshot(body as ReviewedSourceContext),
          };
          value = created;
        } else {
          if (options.loseTurn)
            throw new TypeError("Controlled uncertain turn");
          value = {
            id: "4".repeat(32),
            request_id: body.request_id,
            status: "completed",
          };
        }
      } else if (path.endsWith("/conversations"))
        value = created ? [created, older] : [older];
      else if (path.endsWith("/capabilities"))
        value = { image_input: { status: "unverified" } };
      else value = path.endsWith(`/${older.id}`) ? older : created;
      return new Response(JSON.stringify(value), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      });
    }),
  );
  const book = new ProductDraftBook();
  const custody = assistantRequestCustody(book, repository, source.project_id);
  const props = {
    connection: { base: "http://fixture.invalid/api/v1", token: "" },
    repository,
    projectId: source.project_id,
    onConfigure: vi.fn(),
    onUse: vi.fn(),
    custody,
  };
  return { calls, options, props, custody, older };
}

it("prepares a new source draft without a POST and creates only after explicit confirmation", async () => {
  const f = fixture();
  render(<ProductAssistant {...f.props} reviewedSource={source} />);
  const create = await screen.findByRole("button", {
    name: "Create conversation with reviewed source",
  });
  await waitFor(() => expect(create).toHaveProperty("disabled", false));
  expect(f.calls).toEqual([]);
  fireEvent.click(create);
  await waitFor(() => expect(f.calls).toHaveLength(1));
  expect(f.calls[0].body).toEqual({ task: "product", ...source });
  await screen.findByText(/3 included · 2 omitted/);
  expect(screen.getByText("api/stock.py:8")).toBeTruthy();
  expect(f.calls.some((item) => item.path.endsWith("/turns"))).toBe(false);
  fireEvent.click(screen.getByRole("button", { name: "Conversation history" }));
  fireEvent.change(screen.getByLabelText("Recorded conversation"), {
    target: { value: f.older.id },
  });
  await screen.findByText(/Recorded source report 22222222/);
  expect(f.calls).toHaveLength(1);
});

it("keeps a dispatched creation held after unmount and a different reviewed report", async () => {
  const f = fixture();
  let release = () => {};
  f.options.wait = new Promise<void>((resolve) => {
    release = resolve;
  });
  const first = render(
    <ProductAssistant {...f.props} reviewedSource={source} />,
  );
  const create = await screen.findByRole("button", {
    name: "Create conversation with reviewed source",
  });
  await waitFor(() => expect(create).toHaveProperty("disabled", false));
  fireEvent.click(create);
  await waitFor(() => expect(f.calls).toHaveLength(1));
  first.unmount();
  render(
    <ProductAssistant
      {...f.props}
      reviewedSource={{ ...source, analysis_id: "5".repeat(32) }}
    />,
  );
  await screen.findByText(/new conversation could not be confirmed/);
  expect(
    screen.getByLabelText("Product design message").closest("fieldset"),
  ).toHaveProperty("disabled", true);
  release();
  await waitFor(() =>
    expect(f.custody.getSnapshot().creationUncertain).toBe(true),
  );
  expect(f.calls).toHaveLength(1);
  fireEvent.click(screen.getByRole("button", { name: "Conversation history" }));
  fireEvent.click(screen.getByRole("button", { name: "Refresh record" }));
  await waitFor(() =>
    expect(
      screen
        .getByLabelText("Recorded conversation")
        .querySelector('option[value="33333333333333333333333333333333"]'),
    ).not.toBeNull(),
  );
  fireEvent.change(screen.getByLabelText("Recorded conversation"), {
    target: { value: "3".repeat(32) },
  });
  await screen.findByText(/Recorded source report cccccccc/);
  expect(f.calls).toHaveLength(1);
});

it("retains a conversation's unknown turn when a source handoff remounts the assistant", async () => {
  const f = fixture();
  f.options.loseTurn = true;
  const first = render(<ProductAssistant {...f.props} />);
  const editor = screen.getByLabelText("Product design message");
  await waitFor(() =>
    expect(editor.closest("fieldset")).toHaveProperty("disabled", false),
  );
  fireEvent.change(editor, { target: { value: "Review this source" } });
  fireEvent.submit(editor.closest("form")!);
  await waitFor(() => expect(f.calls).toHaveLength(1));
  await screen.findByText(/last request outcome is uncertain/);
  const held = { ...f.custody.getSnapshot().uncertainRequests };
  first.unmount();
  render(<ProductAssistant {...f.props} reviewedSource={source} />);
  await screen.findByText(/last request outcome is uncertain/);
  expect(f.custody.getSnapshot().uncertainRequests).toEqual(held);
  expect(
    screen.getByLabelText("Product design message").closest("fieldset"),
  ).toHaveProperty("disabled", true);
  expect(f.calls).toHaveLength(1);
  fireEvent.click(screen.getByRole("button", { name: "Conversation history" }));
  fireEvent.change(screen.getByLabelText("Recorded conversation"), {
    target: { value: f.older.id },
  });
  fireEvent.click(screen.getByRole("button", { name: "New conversation" }));
  expect(
    screen.getByLabelText("Product design message").closest("fieldset"),
  ).toHaveProperty("disabled", true);
  expect(f.custody.getSnapshot().uncertainRequests).toEqual(held);
  expect(f.calls).toHaveLength(1);
});
