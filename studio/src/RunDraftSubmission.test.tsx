/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { afterEach, expect, it, vi } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import {
  Children,
  isValidElement,
  type FormEvent,
  type ReactElement,
  type ReactNode,
} from "react";
import type { ForgeRun } from "./api";
import { attemptDigest } from "./LearningObservationAttemptData";
const state = vi.hoisted(() => ({
  submit: null as ((event: FormEvent<HTMLFormElement>) => void) | null,
  buttons: new Map<string, () => void>(),
}));
vi.mock("./Wizard", async (importOriginal) => {
  const actual = await importOriginal<typeof import("./Wizard")>();
  return {
    ...actual,
    Wizard: (props: Parameters<typeof actual.Wizard>[0]) => {
      state.submit = (
        props.children as ReactElement<{
          onSubmit: (event: FormEvent<HTMLFormElement>) => void;
        }>
      ).props.onSubmit;
      state.buttons.clear();
      function inspect(children: ReactNode) {
        Children.forEach(children, (child) => {
          if (
            !isValidElement<{ children?: ReactNode; onClick?: () => void }>(
              child,
            )
          )
            return;
          if (
            child.type === "button" &&
            typeof child.props.children === "string" &&
            child.props.onClick
          )
            state.buttons.set(child.props.children, child.props.onClick);
          inspect(child.props.children);
        });
      }
      inspect(props.children);
      return <actual.Wizard {...props} />;
    },
  };
});
import { NewProduct } from "./App";
import { ProductDraftBook } from "./ProductDrafts";
import { makeCreationMarker } from "./RunCreationData";
const repository = "a".repeat(32);
async function setup() {
  const scope = {
    base: "https://forge.example/api/v1",
    user: "f".repeat(32),
    credentialPresent: false,
  };
  const book = new ProductDraftBook(scope),
    session = book.repository(`run:standalone:${repository}`),
    received: ForgeRun[] = [];
  session.write("brief", "  Verify fictional stock\n", true);
  session.write("commands", '[["pytest", "-q"]]', true);
  session.write("step", 3);
  vi.stubGlobal("matchMedia", () => ({ matches: true }));
  const render = () =>
    renderToStaticMarkup(
      <NewProduct
        drafts={book}
        initialComponent={null}
        initialRepository={repository}
        repositories={[
          {
            id: repository,
            name: "Fictional inventory",
            url: "https://github.com/example/inventory",
            provider: "github",
            default_branch: "main",
          },
        ]}
        profiles={null}
        connected
        connection={{ base: scope.base, token: "" }}
        onStandalone={() => {}}
        onRepository={() => {}}
        onConnection={() => {}}
        onCreated={(run) => received.push(run)}
      />,
    );
  const values = new Map<string, string>();
  vi.stubGlobal("localStorage", {
    getItem: (key: string) => values.get(key) ?? null,
    setItem: (key: string, value: string) => values.set(key, value),
    removeItem: (key: string) => values.delete(key),
  });
  vi.stubGlobal("navigator", {
    locks: {
      request: async (
        _key: string,
        _options: unknown,
        callback: () => unknown,
      ) => callback(),
    },
  });
  let resolve!: (response: Response) => void;
  const run = {
    id: "d".repeat(32),
    repository_id: repository,
    project_id: null,
    component_id: null,
    status: "queued",
    brief: "Verify fictional stock",
  };
  vi.stubGlobal(
    "fetch",
    vi.fn((url: string) =>
      url.endsWith("/runs/" + run.id)
        ? Promise.resolve(new Response(JSON.stringify(run)))
        : new Promise<Response>((value) => {
            resolve = value;
          }),
    ),
  );
  render();
  state.submit!({ preventDefault() {} } as FormEvent<HTMLFormElement>);
  await vi.waitFor(() => expect(fetch).toHaveBeenCalledTimes(1));
  async function finish(response?: Response) {
    const [url, options] = vi.mocked(fetch).mock.calls[0];
    const body = JSON.parse(String(options?.body));
    const unsigned = {
      version: 1,
      request_id: String(url).split("/").at(-1),
      original_actor: {
        user_id: scope.user,
        authentication_method: "local_bootstrap",
      },
      request_digest: await attemptDigest(body),
      input: body.input,
      repository_id: repository,
      project_id: null,
      component_id: null,
      run_id: run.id,
      admitted_at: "2026-10-16T00:00:00+00:00",
      admission_digest: "c".repeat(64),
    };
    const value = { ...unsigned, digest: await attemptDigest(unsigned) };
    resolve(
      response ??
        new Response(
          JSON.stringify({
            ...unsigned,
            digest: await attemptDigest(unsigned),
          }),
          { status: 201 },
        ),
    );
    return value;
  }
  return { book, session, received, finish, run, render, values };
}

afterEach(() => vi.unstubAllGlobals());

it("clears only the submitted run draft after a successful queue response", async () => {
  const { book, session, received, finish, run } = await setup();
  book.repository("run:standalone:other").write("brief", "Keep B", true);
  expect(session.read("busy", false)).toBe(true);
  await finish();
  await vi.waitFor(() => expect(received).toEqual([run]));
  expect(session.active).toBe(false);
  expect(
    book.repository(`run:standalone:${repository}`).read("brief", ""),
  ).toBe("");
  expect(book.repository("run:standalone:other").read("brief", "")).toBe(
    "Keep B",
  );
});

it("retains the raw draft and review step after a rejected queue request", async () => {
  const { session, received, finish } = await setup();
  await finish(
    new Response(JSON.stringify({ detail: "Execution unavailable" }), {
      status: 503,
    }),
  );
  await vi.waitFor(() => expect(session.read("busy", true)).toBe(false));
  expect(session.read("brief", "")).toBe("  Verify fictional stock\n");
  expect(session.read("commands", "")).toBe('[["pytest", "-q"]]');
  expect(session.read("step", 0)).toBe(3);
  expect(session.read("error", "")).toBe("Execution unavailable");
  expect(received).toEqual([]);
});

it("does not revive an old identity draft or open its run after logout", async () => {
  const { book, session, received, finish } = await setup();
  book.clear();
  await finish();
  await new Promise<void>((resolve) => setTimeout(resolve, 0));
  expect(session.read("busy", false)).toBe(false);
  expect(received).toEqual([]);
  expect(book.hasUnsaved).toBe(false);
  expect(
    book.repository(`run:standalone:${repository}`).read("brief", ""),
  ).toBe("");
});

it("preserves newer run edits when an earlier queue request finishes", async () => {
  const { book, session, received, finish, run } = await setup();
  session.write("brief", "Newer unsubmitted work", true);
  await finish();
  await vi.waitFor(() => expect(received).toEqual([run]));
  expect(book.repository(`run:standalone:${repository}`)).toBe(session);
  expect(session.read("brief", "")).toBe("Newer unsubmitted work");
  expect(session.dirty).toBe(true);
  expect(session.read("busy", true)).toBe(false);
});

it("holds a second submit while the retained request is pending", async () => {
  const { received, finish, run } = await setup();
  state.submit!({ preventDefault() {} } as FormEvent<HTMLFormElement>);
  expect(fetch).toHaveBeenCalledTimes(1);
  await finish();
  await vi.waitFor(() => expect(received).toEqual([run]));
});

it("does not send a replacement creation POST after an uncertain response", async () => {
  const { session, finish } = await setup();
  await finish(
    new Response(JSON.stringify({ detail: "Unknown admission outcome" }), {
      status: 503,
    }),
  );
  await vi.waitFor(() => expect(session.read("busy", true)).toBe(false));
  state.submit!({ preventDefault() {} } as FormEvent<HTMLFormElement>);
  await new Promise<void>((resolve) => setTimeout(resolve, 0));
  expect(fetch).toHaveBeenCalledTimes(1);
});

it("recovers the original admitted run after response loss using GET only", async () => {
  const c = await setup();
  const value = await c.finish(
    new Response(JSON.stringify({ detail: "Delivery unknown" }), {
      status: 503,
    }),
  );
  await vi.waitFor(() => expect(c.session.read("busy", true)).toBe(false));
  c.render();
  vi.mocked(fetch).mockImplementation(
    async (url) =>
      new Response(
        JSON.stringify(String(url).includes("/runs/") ? c.run : value),
      ),
  );
  state.buttons.get("Check original result")!();
  await vi.waitFor(() => expect(c.received).toEqual([c.run]));
  expect(
    vi.mocked(fetch).mock.calls.map((call) => call[1]?.method ?? "GET"),
  ).toEqual(["POST", "GET", "GET"]);
  expect(c.values.size).toBe(0);
});
it("holds a missing exact read and only retries the same original input on explicit action", async () => {
  const c = await setup();
  const value = await c.finish(new Response("{}", { status: 503 }));
  await vi.waitFor(() => expect(c.session.read("busy", true)).toBe(false));
  c.render();
  vi.mocked(fetch).mockResolvedValueOnce(new Response("{}", { status: 404 }));
  state.buttons.get("Check original result")!();
  await vi.waitFor(() => expect(c.session.read("busy", true)).toBe(false));
  expect(fetch).toHaveBeenCalledTimes(2);
  expect(c.values.size).toBe(1);
  c.session.write("brief", "Newer unsent work", true);
  c.render();
  vi.mocked(fetch).mockImplementation(
    async (url) =>
      new Response(
        JSON.stringify(String(url).includes("/runs/") ? c.run : value),
      ),
  );
  state.buttons.get("Send original request again")!();
  await vi.waitFor(() => expect(c.received).toEqual([c.run]));
  const calls = vi.mocked(fetch).mock.calls;
  expect(calls[2][0]).toBe(calls[0][0]);
  expect(calls[2][1]?.body).toBe(calls[0][1]?.body);
  expect(c.session.read("brief", "")).toBe("Newer unsent work");
  expect(c.session.active).toBe(true);
});
it("keeps the durable identity through discarding a draft without creating replacement work", async () => {
  const c = await setup();
  await c.finish(new Response("{}", { status: 503 }));
  await vi.waitFor(() => expect(c.session.read("busy", true)).toBe(false));
  c.book.discard(c.session);
  const next = c.book.repository(`run:standalone:${repository}`);
  next.write("step", 3);
  next.write("brief", "Replacement fields", true);
  c.render();
  state.submit!({ preventDefault() {} } as FormEvent<HTMLFormElement>);
  await vi.waitFor(() => expect(next.read("busy", true)).toBe(false));
  expect(fetch).toHaveBeenCalledTimes(1);
  expect(
    next.read<{ requestId: string }>("creation", { requestId: "" }).requestId,
  ).toBe(JSON.parse([...c.values.values()][0]).requestId);
});
it("retains confirmed admission when the current run read is denied", async () => {
  const c = await setup();
  vi.mocked(fetch).mockImplementationOnce(
    async () => new Response("{}", { status: 403 }),
  );
  await c.finish();
  await vi.waitFor(() => expect(c.session.read("busy", true)).toBe(false));
  expect(c.received).toEqual([]);
  expect(c.values.size).toBe(1);
  expect(
    c.session.read<{ run_id: string } | null>("admitted", null)?.run_id,
  ).toBe(c.run.id);
  state.submit!({ preventDefault() {} } as FormEvent<HTMLFormElement>);
  await vi.waitFor(() => expect(c.session.read("busy", true)).toBe(false));
  expect(fetch).toHaveBeenCalledTimes(2);
});

it("keeps the in-memory original hold when durable storage disappears", async () => {
  const c = await setup();
  await c.finish(new Response("{}", { status: 503 }));
  await vi.waitFor(() => expect(c.session.read("busy", true)).toBe(false));
  c.values.clear();
  c.render();
  state.submit!({ preventDefault() {} } as FormEvent<HTMLFormElement>);
  await new Promise<void>((resolve) => setTimeout(resolve, 20));
  expect(fetch).toHaveBeenCalledTimes(1);
  expect(c.received).toEqual([]);
  expect(c.session.read("creation", null)).not.toBeNull();
});

it("does not carry admission or missing-result retry state into another repository request", async () => {
  const c = await setup();
  await c.finish(new Response("{}", { status: 503 }));
  await vi.waitFor(() => expect(c.session.read("busy", true)).toBe(false));
  c.render();
  vi.mocked(fetch).mockResolvedValueOnce(new Response("{}", { status: 404 }));
  state.buttons.get("Check original result")!();
  await vi.waitFor(() => expect(c.session.read("busy", true)).toBe(false));
  const original = c.session.read<{ requestId: string }>("creation", {
    requestId: "",
  });
  c.session.write("admitted", {
    request_id: original.requestId,
    run_id: "d".repeat(32),
  });
  const other = "b".repeat(32);
  c.session.write("repositoryId", other, true);
  c.session.write(
    "creation",
    await makeCreationMarker(
      {
        base: "https://forge.example/api/v1",
        actor: "f".repeat(32),
        repository: other,
        project: null,
        component: null,
      },
      { repository_id: other, brief: "Other fictional work" },
    ),
  );
  const markup = c.render();
  expect(markup).not.toContain("Run admission confirmed");
  expect(markup).not.toContain("Send original request again");
  expect(markup).toContain("Original run request retained");
});

it.each([403, 409, 422, 503, 200])(
  "keeps the original request held after an unresolved %i response",
  async (status) => {
    const c = await setup();
    const requestId = String(vi.mocked(fetch).mock.calls[0][0])
      .split("/")
      .at(-1);
    await c.finish(
      new Response(status === 200 ? "unreadable receipt" : "{}", { status }),
    );
    await vi.waitFor(() => expect(c.session.read("busy", true)).toBe(false));
    c.render();
    state.submit!({ preventDefault() {} } as FormEvent<HTMLFormElement>);
    await vi.waitFor(() => expect(c.session.read("busy", true)).toBe(false));
    expect(fetch).toHaveBeenCalledTimes(1);
    expect(JSON.parse([...c.values.values()][0]).requestId).toBe(requestId);
    expect(c.session.read("brief", "")).toBe("  Verify fictional stock\n");
    expect(c.received).toEqual([]);
  },
);
