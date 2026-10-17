/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { afterEach, expect, it, vi } from "vitest";
import { attemptDigest } from "./LearningObservationAttemptData";
import {
  makeCreationMarker,
  retainCreationMarker,
  recoverCreation,
  sendCreation,
  readCreationMarker,
  clearCreationMarker,
  type CreationLocks,
} from "./RunCreationData";
const scope = {
  base: "https://forge.example/api/v1",
  actor: "f".repeat(32),
  repository: "a".repeat(32),
  project: null,
  component: null,
};
function storage() {
  const map = new Map<string, string>();
  return {
    getItem: (k: string) => map.get(k) ?? null,
    setItem: (k: string, v: string) => {
      map.set(k, v);
    },
    removeItem: (k: string) => {
      map.delete(k);
    },
  };
}
function serial(): CreationLocks {
  let tail = Promise.resolve();
  return {
    request<T>(
      _name: string,
      _options: { mode: "exclusive" },
      callback: () => T | PromiseLike<T>,
    ): Promise<T> {
      const next = tail.then(callback);
      tail = next.then(
        () => undefined,
        () => undefined,
      );
      return next;
    },
  };
}
const body = { repository_id: scope.repository, brief: "Fictional stock" };
async function receipt(marker: Awaited<ReturnType<typeof makeCreationMarker>>) {
  const value = {
    version: 1,
    request_id: marker.requestId,
    original_actor: {
      user_id: scope.actor,
      authentication_method: "local_bootstrap",
    },
    request_digest: marker.inputDigest,
    input: JSON.parse(marker.body).input,
    repository_id: scope.repository,
    project_id: null,
    component_id: null,
    run_id: "d".repeat(32),
    admitted_at: "2026-10-16T00:00:00+00:00",
    admission_digest: "c".repeat(64),
  };
  return { ...value, digest: await attemptDigest(value) };
}
afterEach(() => vi.unstubAllGlobals());
it("admits one original identity under a scope lock before any POST", async () => {
  const disk = storage(),
    locks = serial(),
    first = await makeCreationMarker(scope, body),
    second = await makeCreationMarker(scope, body);
  const results = await Promise.allSettled([
    retainCreationMarker(disk, first, locks, () => true),
    retainCreationMarker(disk, second, locks, () => true),
  ]);
  expect(results.filter((r) => r.status === "fulfilled")).toHaveLength(1);
  expect(results.filter((r) => r.status === "rejected")).toHaveLength(1);
  expect((await readCreationMarker(disk, scope))?.requestId).toBe(
    first.requestId,
  );
});
it("fails closed without WebLocks or after scope changes inside grant", async () => {
  const marker = await makeCreationMarker(scope, body),
    disk = storage();
  await expect(
    retainCreationMarker(disk, marker, undefined, () => true),
  ).rejects.toThrow();
  await expect(
    retainCreationMarker(disk, marker, serial(), () => false),
  ).rejects.toThrow();
  expect(await readCreationMarker(disk, scope)).toBeNull();
});
it("keeps an original identity after response loss and missing exact readback without another POST", async () => {
  const marker = await makeCreationMarker(scope, body),
    disk = storage();
  await retainCreationMarker(disk, marker, serial(), () => true);
  vi.stubGlobal(
    "fetch",
    vi
      .fn()
      .mockRejectedValueOnce(new Error("lost response"))
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ detail: "not found" }), { status: 404 }),
      ),
  );
  await expect(
    sendCreation({ base: scope.base, token: "" }, marker, () => true),
  ).rejects.toThrow();
  await expect(
    recoverCreation({ base: scope.base, token: "" }, marker, () => true),
  ).rejects.toThrow();
  expect(fetch).toHaveBeenCalledTimes(2);
  expect(vi.mocked(fetch).mock.calls[0][1]?.method).toBe("POST");
  expect(vi.mocked(fetch).mock.calls[1][1]?.method ?? "GET").toBe("GET");
  expect((await readCreationMarker(disk, scope))?.requestId).toBe(
    marker.requestId,
  );
});
it("verifies the original owner, input and receipt digest before returning admission", async () => {
  const marker = await makeCreationMarker(scope, body),
    value = await receipt(marker);
  vi.stubGlobal(
    "fetch",
    vi
      .fn()
      .mockResolvedValue(new Response(JSON.stringify(value), { status: 201 })),
  );
  expect(
    await sendCreation({ base: scope.base, token: "" }, marker, () => true),
  ).toEqual(value);
  for (const changed of [
    { ...value, run_id: "b".repeat(32) },
    {
      ...value,
      original_actor: { ...value.original_actor, user_id: "1".repeat(32) },
    },
  ]) {
    vi.stubGlobal(
      "fetch",
      vi
        .fn()
        .mockResolvedValue(
          new Response(JSON.stringify(changed), { status: 200 }),
        ),
    );
    await expect(
      recoverCreation({ base: scope.base, token: "" }, marker, () => true),
    ).rejects.toThrow();
  }
});
it("matches Python strip and leaves literal inner whitespace and omitted fields intact", async () => {
  const marker = await makeCreationMarker(scope, {
    ...body,
    brief: "\u001c\u0085 Fictional\nstock \u0085\u001f",
  });
  expect(JSON.parse(marker.body).input).toEqual({
    ...body,
    brief: "Fictional\nstock",
  });
  expect(JSON.parse(marker.body).input).not.toHaveProperty("commands");
});

it("rejects unsuccessful durable write verification before any request", async () => {
  const original = await makeCreationMarker(scope, body);
  const disk = storage();
  disk.setItem = () => {};
  await expect(
    retainCreationMarker(disk, original, serial(), () => true),
  ).rejects.toThrow();
  expect(await readCreationMarker(disk, scope)).toBeNull();
});

it("keeps markers private to their server, owner, repository and component scope", async () => {
  const disk = storage();
  const scoped = { ...scope, project: "b".repeat(32), component: "stock-api" };
  const original = await makeCreationMarker(scoped, {
    ...body,
    project_id: scoped.project,
    component_id: scoped.component,
  });
  await retainCreationMarker(disk, original, serial(), () => true);
  for (const other of [
    { ...scoped, base: "https://other.example/api/v1" },
    { ...scoped, actor: "1".repeat(32) },
    { ...scoped, repository: "2".repeat(32) },
    { ...scoped, project: "3".repeat(32) },
    { ...scoped, component: "other-api" },
    scope,
  ])
    expect(await readCreationMarker(disk, other)).toBeNull();
  expect(await readCreationMarker(disk, scoped)).toEqual(original);
});

it("refuses a changed server or identity before HTTP and suppresses late private receipts", async () => {
  const original = await makeCreationMarker(scope, body);
  vi.stubGlobal("fetch", vi.fn());
  await expect(
    sendCreation(
      { base: "https://other.example/api/v1", token: "" },
      original,
      () => true,
    ),
  ).rejects.toThrow();
  await expect(
    sendCreation({ base: scope.base, token: "" }, original, () => false),
  ).rejects.toThrow();
  expect(fetch).not.toHaveBeenCalled();
  const value = await receipt(original);
  let current = true;
  vi.mocked(fetch).mockImplementation(async () => {
    current = false;
    return new Response(JSON.stringify(value));
  });
  await expect(
    recoverCreation({ base: scope.base, token: "" }, original, () => current),
  ).rejects.toThrow();
});

it("does not clear a different retained request or an inactive owner", async () => {
  const disk = storage(),
    locks = serial();
  const first = await makeCreationMarker(scope, body);
  const other = await makeCreationMarker(scope, body);
  await retainCreationMarker(disk, first, locks, () => true);
  await expect(
    clearCreationMarker(disk, other, locks, () => true),
  ).rejects.toThrow();
  await expect(
    clearCreationMarker(disk, first, locks, () => false),
  ).rejects.toThrow();
  expect(await readCreationMarker(disk, scope)).toEqual(first);
});
