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
import {
  nativeProjection,
  nativeRecord,
  nativeReceipt,
  nativeVerifiedProjection,
} from "../tests/native-learning-fixture";
import { LearningRecords } from "./LearningRecords";
import type { IdentitySession } from "./Identity";

const session = {
  user: { id: "f".repeat(32), authentication_method: "local_bootstrap" },
  global_admin: true,
  memberships: [],
} as unknown as IdentitySession;
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  sessionStorage.clear();
});
async function show(projection: unknown, currentSession = session) {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input) =>
      Response.json(
        String(input).endsWith("/projection") ? projection : [nativeRecord],
      ),
    ),
  );
  render(
    <LearningRecords
      connection={{ base: "http://localhost/api/v1", token: "fixture-only" }}
      repository={nativeRecord.envelope.context.repository_id}
      session={currentSession}
    />,
  );
  fireEvent.click(
    await screen.findByRole("button", { name: nativeRecord.envelope.question }),
  );
}
async function start(lost = false) {
  const posts: string[] = [];
  let requestId = "";
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input, init) => {
      const path = String(input);
      if (init?.method === "POST") {
        expect(sessionStorage.length).toBe(1);
        const body = JSON.parse(init.body as string);
        requestId = body.request_id;
        expect(body.source_id).toBe(nativeProjection.source_id);
        posts.push(path);
        if (lost) throw new TypeError("Controlled lost reply");
        return Response.json({ ...nativeProjection, state: "verified" });
      }
      if (path.includes("/observations/"))
        return Response.json(nativeReceipt(requestId));
      return Response.json(
        path.endsWith("/projection")
          ? requestId
            ? { ...nativeVerifiedProjection, reconciliation_id: requestId }
            : nativeProjection
          : [nativeRecord],
      );
    }),
  );
  render(
    <LearningRecords
      connection={{ base: "http://localhost/api/v1", token: "fixture-only" }}
      repository={nativeRecord.envelope.context.repository_id}
      session={session}
    />,
  );
  fireEvent.click(
    await screen.findByRole("button", { name: nativeRecord.envelope.question }),
  );
  return posts;
}
it("routes an explicitly native record to scoped source evidence without exposing a V1 action", async () => {
  const posts: string[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input, init) => {
      if (init?.method === "POST") posts.push(String(input));
      return new Response(
        JSON.stringify(
          String(input).endsWith("/projection")
            ? nativeProjection
            : [nativeRecord],
        ),
        { status: 200, headers: { "Content-Type": "application/json" } },
      );
    }),
  );
  render(
    <LearningRecords
      connection={{ base: "http://localhost/api/v1", token: "fixture-only" }}
      repository={nativeRecord.envelope.context.repository_id}
      session={session}
    />,
  );
  fireEvent.click(
    await screen.findByRole("button", { name: nativeRecord.envelope.question }),
  );
  expect(
    await screen.findByRole("heading", { name: "Koinon source evidence" }),
  ).toBeTruthy();
  expect(screen.queryByText("flycanon projection")).toBeNull();
  expect(
    screen.queryByRole("button", { name: "Verify an existing source" }),
  ).toBeNull();
  expect(posts).toEqual([]);
});

it("retains one request before POST and reads its typed exact receipt before showing recorded", async () => {
  const posts = await start();
  fireEvent.click(
    await screen.findByRole("button", { name: "Review source observation" }),
  );
  fireEvent.click(
    screen.getByRole("checkbox", { name: /I checked this existing source/ }),
  );
  fireEvent.click(screen.getByRole("button", { name: "Observe this source" }));
  expect(
    await screen.findByRole("heading", { name: "Source observation recorded" }),
  ).toBeTruthy();
  expect(posts).toHaveLength(1);
  expect(posts[0]).toContain("/projection/observe");
  expect(screen.getByText("inventory-version-7")).toBeTruthy();
  expect(screen.getByText(/does not approve memory/)).toBeTruthy();
});

it("holds a lost reply and only reads the original receipt when asked", async () => {
  const posts = await start(true);
  fireEvent.click(
    await screen.findByRole("button", { name: "Review source observation" }),
  );
  fireEvent.click(
    screen.getByRole("checkbox", { name: /I checked this existing source/ }),
  );
  fireEvent.click(screen.getByRole("button", { name: "Observe this source" }));
  expect(
    await screen.findByRole("heading", { name: "Observation outcome unknown" }),
  ).toBeTruthy();
  expect(
    screen.queryByRole("button", { name: "Observe this source" }),
  ).toBeNull();
  expect(posts).toHaveLength(1);
  fireEvent.click(
    screen.getByRole("button", { name: "Read exact observation" }),
  );
  expect(
    await screen.findByRole("heading", { name: "Source observation recorded" }),
  ).toBeTruthy();
  expect(posts).toHaveLength(1);
});

it("withholds POST if the pending identity cannot be saved", async () => {
  const posts = await start();
  fireEvent.click(
    await screen.findByRole("button", { name: "Review source observation" }),
  );
  fireEvent.click(
    screen.getByRole("checkbox", { name: /I checked this existing source/ }),
  );
  const storage = sessionStorage;
  vi.stubGlobal("sessionStorage", {
    getItem: storage.getItem.bind(storage),
    setItem: () => {
      throw new Error("Storage unavailable");
    },
  });
  fireEvent.click(screen.getByRole("button", { name: "Observe this source" }));
  await waitFor(() =>
    expect(screen.getByRole("alert").textContent).toContain(
      "No observation was submitted",
    ),
  );
  expect(posts).toHaveLength(0);
});

it.each(["pending", "executing"])(
  "keeps %s status explicit without offering an observation",
  async (state) => {
    await show(
      state === "pending"
        ? { id: nativeProjection.id, record_id: nativeRecord.id, state }
        : { ...nativeProjection, state, source_id: undefined },
    );
    await waitFor(() =>
      expect(screen.queryByText("Reading native source status…")).toBeNull(),
    );
    expect(
      screen.getByText(
        state === "pending" ? "Not submitted" : "Source submission in progress",
      ),
    ).toBeTruthy();
    expect(
      screen.queryByRole("button", { name: "Review source observation" }),
    ).toBeNull();
  },
);

it("keeps upstream private review separate from a source-observation receipt", async () => {
  await show({
    ...nativeProjection,
    state: "review_required",
    unsupported_private_field: "do-not-render-this",
  });
  expect(await screen.findByRole("alert")).toHaveProperty(
    "textContent",
    expect.stringContaining("private review in Koinon"),
  );
  expect(screen.queryByText("do-not-render-this")).toBeNull();
  expect(
    screen.queryByRole("button", { name: "Observe this source" }),
  ).toBeNull();
});

it("withholds new observations when the current member can review but cannot edit", async () => {
  await show(nativeProjection, {
    ...session,
    global_admin: false,
    memberships: [
      {
        project_id: nativeRecord.envelope.context.project_id,
        role: "reviewer",
        revision: 1,
        permissions: ["read", "review"],
      },
    ],
  } as IdentitySession);
  expect(
    (
      (await screen.findByRole("button", {
        name: "Review source observation",
      })) as HTMLButtonElement
    ).disabled,
  ).toBe(true);
  expect(
    screen.getByText(/Current project edit and review permission/),
  ).toBeTruthy();
});

it("clears private source evidence on current access refusal and preserves the request for readback", async () => {
  await start();
  fireEvent.click(
    await screen.findByRole("button", { name: "Review source observation" }),
  );
  fireEvent.click(
    screen.getByRole("checkbox", { name: /I checked this existing source/ }),
  );
  vi.stubGlobal(
    "fetch",
    vi.fn(async () =>
      Response.json({ detail: "Current grant revoked" }, { status: 403 }),
    ),
  );
  fireEvent.click(screen.getByRole("button", { name: "Observe this source" }));
  await waitFor(() =>
    expect(document.querySelector(".native-learning")).toBeNull(),
  );
  expect(screen.getByText(/Private evidence has been cleared/)).toBeTruthy();
  expect(sessionStorage.length).toBe(1);
});
