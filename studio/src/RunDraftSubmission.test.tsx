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
  captureEffects: false,
  effects: [] as Array<() => void | (() => void)>,
}));
vi.mock("react", async (importOriginal) => {
  const actual = await importOriginal<typeof import("react")>();
  return {
    ...actual,
    useEffect: (...args: Parameters<typeof actual.useEffect>) => {
      actual.useEffect(...args);
      if (state.captureEffects) state.effects.push(args[0]);
    },
  };
});
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
    received: ForgeRun[] = [],
    notices: string[] = [];
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
        onCreated={(run, notice) => {
          received.push(run);
          notices.push(notice);
        }}
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
  return { book, session, received, notices, finish, run, render, values };
}

afterEach(() => {
  state.captureEffects = false;
  state.effects = [];
  vi.unstubAllGlobals();
});

it("clears only the submitted run draft after a successful queue response", async () => {
  const { book, session, received, notices, finish, run } = await setup();
  book.repository("run:standalone:other").write("brief", "Keep B", true);
  expect(session.read("busy", false)).toBe(true);
  await finish();
  await vi.waitFor(() => expect(received).toEqual([run]));
  expect(notices).toEqual(["Your run has been queued."]);
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
        JSON.stringify(
          String(url).includes("/runs/")
            ? c.run
            : String(url).endsWith("/decision")
              ? { version: 1, outcome: "admitted", receipt: value }
              : value,
        ),
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
        JSON.stringify(
          String(url).includes("/runs/")
            ? c.run
            : String(url).endsWith("/decision")
              ? { version: 1, outcome: "admitted", receipt: value }
              : value,
        ),
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

it.each([
  { repository: "", actor: "f".repeat(32), retained: null, failed: false },
  { repository, actor: undefined, retained: null, failed: false },
  { repository, actor: "f".repeat(32), retained: "{}", failed: true },
])(
  "reads retained recovery state only with a selected source and owner: %j",
  async (selection) => {
    const book = new ProductDraftBook({
      base: "https://forge.example/api/v1",
      user: selection.actor,
      credentialPresent: false,
    });
    const draft = book.repository(`run:standalone:${selection.repository}`);
    vi.stubGlobal("localStorage", { getItem: () => selection.retained });
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response(JSON.stringify({ skills: [] }))),
    );
    state.captureEffects = true;
    renderToStaticMarkup(
      <NewProduct
        drafts={book}
        initialComponent={null}
        initialRepository={selection.repository}
        repositories={[]}
        profiles={null}
        connected
        connection={{ base: "https://forge.example/api/v1", token: "" }}
        onStandalone={() => {}}
        onRepository={() => {}}
        onConnection={() => {}}
        onCreated={() => {}}
      />,
    );
    state.captureEffects = false;
    const cleanup = state.effects.map((effect) => effect());
    await new Promise<void>((resolve) => setTimeout(resolve, 0));
    expect(Boolean(draft.read("error", ""))).toBe(selection.failed);
    cleanup.forEach((value) => value?.());
  },
);

it.each(["succeeded", "failed", "cancelled"])(
  "opens an originally admitted %s run without claiming it was queued again",
  async (status) => {
    const c = await setup();
    const receipt = await c.finish(new Response("{}", { status: 503 }));
    await vi.waitFor(() => expect(c.session.read("busy", true)).toBe(false));
    c.run.status = status;
    c.render();
    vi.mocked(fetch).mockImplementation(
      async (url) =>
        new Response(
          JSON.stringify(
            String(url).includes("/runs/")
              ? c.run
              : String(url).endsWith("/decision")
                ? { version: 1, outcome: "admitted", receipt }
                : receipt,
          ),
        ),
    );
    state.buttons.get("Check original result")!();
    await vi.waitFor(() => expect(c.received).toEqual([c.run]));
    expect(c.notices).toEqual(["Your run has been opened."]);
  },
);

async function cancelledDecision(c: Awaited<ReturnType<typeof setup>>) {
  const original = JSON.parse([...c.values.values()][0]);
  const unsigned = {
    version: 1,
    request_id: original.requestId,
    original_actor: {
      user_id: original.actor,
      authentication_method: "local_bootstrap",
    },
    request_digest: original.inputDigest,
    repository_id: original.repository,
    project_id: original.project,
    component_id: original.component,
    cancelled_by: {
      user_id: original.actor,
      authentication_method: "local_bootstrap",
    },
    cancelled_at: "2026-10-17T00:00:00+00:00",
  };
  return {
    version: 1,
    outcome: "cancelled",
    cancellation: { ...unsigned, digest: await attemptDigest(unsigned) },
  };
}

it("explicitly cancels an unresolved original before allowing newer input to submit with a new ID", async () => {
  const c = await setup();
  const originalUrl = vi.mocked(fetch).mock.calls[0][0];
  await c.finish(new Response("{}", { status: 503 }));
  await vi.waitFor(() => expect(c.session.read("busy", true)).toBe(false));
  const decision = await cancelledDecision(c);
  c.session.write("brief", "Newer unsent work", true);
  c.render();
  vi.mocked(fetch).mockResolvedValueOnce(
    new Response(JSON.stringify(decision), { status: 201 }),
  );
  expect(state.buttons.has("Cancel this request")).toBe(true);
  state.buttons.get("Cancel this request")!();
  await vi.waitFor(() => expect(c.values.size).toBe(0));
  expect(c.session.read("brief", "")).toBe("Newer unsent work");
  expect(c.session.active).toBe(true);
  expect(c.render()).toContain(
    "Request cancelled. You can edit and start again.",
  );
  vi.mocked(fetch).mockImplementation(() => new Promise<Response>(() => {}));
  state.submit!({ preventDefault() {} } as FormEvent<HTMLFormElement>);
  await vi.waitFor(() => expect(fetch).toHaveBeenCalledTimes(3));
  const calls = vi.mocked(fetch).mock.calls;
  expect(calls[1][0]).toBe(String(originalUrl) + "/cancel");
  expect(calls[2][0]).not.toBe(originalUrl);
  expect(JSON.parse(String(calls[2][1]?.body)).input.brief).toBe(
    "Newer unsent work",
  );
  expect(c.values.size).toBe(1);
  expect(c.received).toEqual([]);
});

it("can cancel while the original HTTP response waits and fences its late callbacks from a newer request", async () => {
  const c = await setup();
  const decision = await cancelledDecision(c);
  c.render();
  vi.mocked(fetch).mockResolvedValueOnce(
    new Response(JSON.stringify(decision)),
  );
  expect(state.buttons.has("Cancel this request")).toBe(true);
  state.buttons.get("Cancel this request")!();
  await vi.waitFor(() => expect(c.values.size).toBe(0));
  c.session.write("brief", "New request after explicit cancellation", true);
  c.render();
  vi.mocked(fetch).mockImplementation(() => new Promise<Response>(() => {}));
  state.submit!({ preventDefault() {} } as FormEvent<HTMLFormElement>);
  await vi.waitFor(() => expect(fetch).toHaveBeenCalledTimes(3));
  const pending = [...c.values.values()][0];
  await c.finish();
  await new Promise<void>((resolve) => setTimeout(resolve, 0));
  expect(c.received).toEqual([]);
  expect([...c.values.values()]).toEqual([pending]);
  expect(c.session.read("busy", false)).toBe(true);
  expect(c.session.read("brief", "")).toBe(
    "New request after explicit cancellation",
  );
});

it("recovers a lost cancellation response by exact decision GET without another POST", async () => {
  const c = await setup();
  await c.finish(new Response("{}", { status: 503 }));
  await vi.waitFor(() => expect(c.session.read("busy", true)).toBe(false));
  const decision = await cancelledDecision(c);
  c.render();
  vi.mocked(fetch).mockRejectedValueOnce(
    new Error("Lost cancellation response"),
  );
  expect(state.buttons.has("Cancel this request")).toBe(true);
  state.buttons.get("Cancel this request")!();
  await vi.waitFor(() => expect(c.session.read("busy", true)).toBe(false));
  expect(c.values.size).toBe(1);
  c.render();
  vi.mocked(fetch).mockResolvedValueOnce(
    new Response(JSON.stringify(decision)),
  );
  state.buttons.get("Check original result")!();
  await vi.waitFor(() => expect(c.values.size).toBe(0));
  const calls = vi.mocked(fetch).mock.calls;
  expect(calls.map((call) => call[1]?.method ?? "GET")).toEqual([
    "POST",
    "POST",
    "GET",
  ]);
  expect(calls[2][0]).toBe(String(calls[0][0]) + "/decision");
  expect(c.received).toEqual([]);
});

it.each([404, 403, 409, 200])(
  "keeps the exact original after an unresolved cancellation response %i",
  async (status) => {
    const c = await setup();
    await c.finish(new Response("{}", { status: 503 }));
    await vi.waitFor(() => expect(c.session.read("busy", true)).toBe(false));
    const retained = [...c.values.values()][0];
    c.render();
    vi.mocked(fetch).mockResolvedValueOnce(
      new Response(
        status === 200 ? JSON.stringify({ outcome: "cancelled" }) : "{}",
        { status },
      ),
    );
    state.buttons.get("Cancel this request")!();
    await vi.waitFor(() => expect(c.session.read("busy", true)).toBe(false));
    c.render();
    state.submit!({ preventDefault() {} } as FormEvent<HTMLFormElement>);
    await vi.waitFor(() => expect(c.session.read("busy", true)).toBe(false));
    expect([...c.values.values()]).toEqual([retained]);
    expect(fetch).toHaveBeenCalledTimes(2);
    expect(c.received).toEqual([]);
  },
);

it("adopts another tab's newer marker without deleting it or discarding edits after cancellation", async () => {
  const c = await setup();
  await c.finish(new Response("{}", { status: 503 }));
  await vi.waitFor(() => expect(c.session.read("busy", true)).toBe(false));
  const decision = await cancelledDecision(c);
  const original = JSON.parse([...c.values.values()][0]);
  c.render();
  let resolve!: (response: Response) => void;
  vi.mocked(fetch).mockImplementationOnce(
    () =>
      new Promise<Response>((done) => {
        resolve = done;
      }),
  );
  state.buttons.get("Cancel this request")!();
  await vi.waitFor(() => expect(fetch).toHaveBeenCalledTimes(2));
  const newer = await makeCreationMarker(original, {
    repository_id: repository,
    brief: "Other tab's input",
  });
  c.values.set([...c.values.keys()][0], JSON.stringify(newer));
  c.session.write("brief", "Keep this tab's edits", true);
  resolve(new Response(JSON.stringify(decision)));
  await vi.waitFor(() => expect(c.session.read("busy", true)).toBe(false));
  expect(JSON.parse([...c.values.values()][0])).toEqual(newer);
  expect(c.session.read("creation", null)).toEqual(newer);
  expect(c.session.read("brief", "")).toBe("Keep this tab's edits");
  expect(c.received).toEqual([]);
  expect(c.render()).toContain("Another request is waiting");
});

it("releases an in-memory hold on a verified terminal decision when another tab already removed the marker", async () => {
  const c = await setup();
  await c.finish(new Response("{}", { status: 503 }));
  await vi.waitFor(() => expect(c.session.read("busy", true)).toBe(false));
  const decision = await cancelledDecision(c);
  c.values.clear();
  c.render();
  vi.mocked(fetch).mockResolvedValueOnce(
    new Response(JSON.stringify(decision)),
  );
  state.buttons.get("Check original result")!();
  await vi.waitFor(() => expect(c.session.read("creation", "held")).toBeNull());
  expect(c.values.size).toBe(0);
  expect(c.received).toEqual([]);
});

it("opens the existing admission when cancellation loses the admission race", async () => {
  const c = await setup();
  const receipt = await c.finish(new Response("{}", { status: 503 }));
  await vi.waitFor(() => expect(c.session.read("busy", true)).toBe(false));
  c.render();
  vi.mocked(fetch).mockImplementation(
    async (url) =>
      new Response(
        JSON.stringify(
          String(url).endsWith("/cancel")
            ? { version: 1, outcome: "admitted", receipt }
            : c.run,
        ),
      ),
  );
  state.buttons.get("Cancel this request")!();
  await vi.waitFor(() => expect(c.received).toEqual([c.run]));
  expect(c.values.size).toBe(0);
  expect(fetch).toHaveBeenCalledTimes(3);
  expect(c.notices).toEqual(["Your run has been queued."]);
});

it("ignores a cancellation response after sign-out and retains its durable original", async () => {
  const c = await setup();
  const decision = await cancelledDecision(c);
  const retained = [...c.values.values()][0];
  c.render();
  let resolve!: (response: Response) => void;
  vi.mocked(fetch).mockImplementationOnce(
    () =>
      new Promise<Response>((done) => {
        resolve = done;
      }),
  );
  state.buttons.get("Cancel this request")!();
  await vi.waitFor(() => expect(fetch).toHaveBeenCalledTimes(2));
  c.book.clear();
  resolve(new Response(JSON.stringify(decision)));
  await new Promise<void>((done) => setTimeout(done, 20));
  expect([...c.values.values()]).toEqual([retained]);
  expect(c.received).toEqual([]);
  expect(c.book.hasUnsaved).toBe(false);
});
