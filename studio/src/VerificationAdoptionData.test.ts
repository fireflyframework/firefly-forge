/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { expect, it } from "vitest";
import {
  adoptionCandidate,
  confirmAdoption,
  adoptionInput,
  adoptionKey,
  readAdoption,
  sendAdoptionOnce,
} from "./VerificationAdoptionData";
const context = {
  base: "https://factory.example/api/v1",
  actor: "a".repeat(32),
  authentication: "local_bootstrap",
  project: null,
  repository: "b".repeat(32),
  run: "c".repeat(32),
  request: "d".repeat(32),
};
const candidate = {
  version: 1,
  run_id: context.run,
  worker_request_id: context.request,
  agent: "bridge",
  pending_request_digest: "1".repeat(64),
  source_digest: "2".repeat(64),
  commands_digest: "3".repeat(64),
  policy_digest: "4".repeat(64),
  provider_digest: "5".repeat(64),
  commands_count: 7,
  max_continuations: 1,
  reusable_build_candidates: [
    {
      command_index: 1,
      command_digest: "6".repeat(64),
      argv: ["python", "-m", "ruff", "check"],
      purpose: "source_build",
      requires_declaration: true,
    },
  ],
};
it("copies only selected backend candidates and keeps omitted declarations absent", () => {
  const c = adoptionCandidate(candidate, context, "bridge");
  const empty = adoptionInput(c, "e".repeat(32), []);
  expect(empty).not.toHaveProperty("reusable_build_checks");
  expect(
    adoptionInput(c, "e".repeat(32), ["6".repeat(64)]).reusable_build_checks,
  ).toEqual([
    {
      command_digest: "6".repeat(64),
      purpose: "source_build",
      independent_of_time_and_external_state: true,
    },
  ]);
  expect(() => adoptionInput(c, "e".repeat(32), ["7".repeat(64)])).toThrow();
});
it.each([
  { run_id: "f".repeat(32) },
  { worker_request_id: "f".repeat(32) },
  { agent: "claude" },
  { commands_count: true },
  {
    reusable_build_candidates: [
      {
        ...candidate.reusable_build_candidates[0],
        requires_declaration: false,
      },
    ],
  },
  {
    reusable_build_candidates: [
      candidate.reusable_build_candidates[0],
      candidate.reusable_build_candidates[0],
    ],
  },
  {
    reusable_build_candidates: [
      { ...candidate.reusable_build_candidates[0], command_index: 8 },
    ],
  },
  {
    reusable_build_candidates: [
      { ...candidate.reusable_build_candidates[0], command_index: 0 },
    ],
  },
])("refuses malformed or foreign candidates %j", (change) =>
  expect(() =>
    adoptionCandidate({ ...candidate, ...change }, context, "bridge"),
  ).toThrow(),
);
it("accepts the last frozen command's one-based index", () => {
  expect(
    adoptionCandidate(
      {
        ...candidate,
        reusable_build_candidates: [
          {
            ...candidate.reusable_build_candidates[0],
            command_index: candidate.commands_count,
          },
        ],
      },
      context,
      "bridge",
    ).reusable_build_candidates?.[0].command_index,
  ).toBe(7);
});
it("retains one exact submission before transport, including after a lost reply", async () => {
  const rows = new Map<string, string>();
  const storage = {
    getItem: (key: string) => rows.get(key) ?? null,
    setItem: (key: string, value: string) => {
      rows.set(key, value);
    },
  };
  const marker = {
    version: 1 as const,
    context,
    body: adoptionInput(
      adoptionCandidate(candidate, context, "bridge"),
      "e".repeat(32),
      [],
    ),
  };
  let calls = 0;
  const send = async () => {
    calls++;
    expect(
      readAdoption(storage.getItem(adoptionKey(context)), context),
    ).toEqual(marker);
    throw Error("lost reply");
  };
  await expect(sendAdoptionOnce(storage, marker, send)).rejects.toThrow(
    "lost reply",
  );
  await expect(sendAdoptionOnce(storage, marker, send)).rejects.toThrow();
  expect(calls).toBe(1);
  expect(() =>
    readAdoption(storage.getItem(adoptionKey(context)), {
      ...context,
      actor: "f".repeat(32),
    }),
  ).toThrow();
});

it("never sends when the browser cannot durably retain the exact approval", async () => {
  const marker = {
    version: 1 as const,
    context,
    body: adoptionInput(
      adoptionCandidate(candidate, context, "bridge"),
      "e".repeat(32),
      [],
    ),
  };
  let sent = false;
  await expect(
    sendAdoptionOnce(
      {
        getItem: () => null,
        setItem: () => {
          throw Error("storage blocked");
        },
      },
      marker,
      async () => {
        sent = true;
      },
    ),
  ).rejects.toThrow();
  expect(sent).toBe(false);
});
it("checks receipt bytes, current actor and policy before confirming approval", async () => {
  const { attemptDigest } = await import("./LearningObservationAttemptData");
  const body = adoptionInput(
    adoptionCandidate(candidate, context, "bridge"),
    "e".repeat(32),
    ["6".repeat(64)],
  );
  const marker = { version: 1 as const, context, body };
  const receipt = {
    version: 1,
    request: body,
    body_digest: await attemptDigest(body),
    actor: {
      user_id: context.actor,
      authentication_method: context.authentication,
    },
    policy_snapshot: { digest: body.policy_digest },
  };
  await expect(confirmAdoption(receipt, marker)).resolves.toBeUndefined();
  for (const wrong of [
    { ...receipt, request: { ...body, reusable_build_checks: [] } },
    { ...receipt, actor: { ...receipt.actor, user_id: "f".repeat(32) } },
    { ...receipt, policy_snapshot: { digest: "7".repeat(64) } },
  ])
    await expect(confirmAdoption(wrong, marker)).rejects.toThrow();
});
