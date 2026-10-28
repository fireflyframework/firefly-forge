/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { describe, expect, it } from "vitest";
import {
  readRunnerSettings,
  runnerDraft,
  runnerPayload,
} from "./RunnerSettings";

const saved = {
  repository_id: "a".repeat(32),
  revision: "b".repeat(32),
  source: "repository",
  sandbox: {
    mode: "host",
    timeout: 300,
    max_output_bytes: 1024,
    command_limit: 22,
    internet_search: true,
  },
};
describe("repository runner settings", () => {
  it("preserves existing policy while explicitly opting in for future runs", () => {
    const current = readRunnerSettings(saved, saved.repository_id);
    const before = JSON.stringify(current);
    const draft = {
      ...runnerDraft(current),
      monitor: true,
      deadlineEnabled: true,
      deadline: "600",
    };
    const payload = runnerPayload(current, draft);
    expect(payload.sandbox).toMatchObject({
      mode: "host",
      process_monitor: true,
      timeout: 300,
      work_duration: {
        protocol: "forge-work-duration-v2",
        timeout_seconds: 600,
      },
      command_limit: 22,
      internet_search: true,
    });
    expect(JSON.stringify(current)).toBe(before);
  });
  it("requires disabling host monitoring before choosing Docker", () => {
    const current = readRunnerSettings(saved, saved.repository_id);
    const draft = {
      ...runnerDraft(current),
      mode: "docker" as const,
      monitor: true,
      context: "local",
      image: "example/runner@sha256:" + "f".repeat(64),
    };
    expect(() => runnerPayload(current, draft)).toThrow(/host/i);
    expect(
      runnerPayload(current, { ...draft, monitor: false }).sandbox,
    ).toMatchObject({ mode: "docker", process_monitor: false });
  });
  it("clears Docker-only controls only after an explicit change to host", () => {
    const current = readRunnerSettings(
      {
        ...saved,
        sandbox: {
          ...saved.sandbox,
          mode: "docker",
          context: "local",
          image: "example/runner@sha256:" + "f".repeat(64),
          browser: { image: "retained" },
          network: "none",
          workspace_transport: "archive",
        },
      },
      saved.repository_id,
    );
    const payload = runnerPayload(current, {
      ...runnerDraft(current),
      mode: "host",
    });
    expect(payload.sandbox).toMatchObject({
      mode: "host",
      image: null,
      context: null,
      browser: null,
      workspace_transport: "bind",
      network: "none",
    });
    expect(current.sandbox.browser).toEqual({ image: "retained" });
  });
  it("rejects foreign settings and malformed numbers before save", () => {
    expect(() => readRunnerSettings(saved, "c".repeat(32))).toThrow();
    const current = readRunnerSettings(saved, saved.repository_id);
    for (const deadline of ["", "3.5", "0", "1e3", "-1"])
      expect(() =>
        runnerPayload(current, {
          ...runnerDraft(current),
          deadlineEnabled: true,
          deadline,
        }),
      ).toThrow();
    expect(() =>
      readRunnerSettings(
        { ...saved, sandbox: { ...saved.sandbox, process_monitor: "true" } },
        saved.repository_id,
      ),
    ).toThrow();
  });
  it("defaults new work to completion or cancellation while preserving frozen inputs", () => {
    const current = readRunnerSettings(saved, saved.repository_id),
      before = JSON.stringify(current);
    const draft = runnerDraft(current);
    expect(draft.deadlineEnabled).toBe(false);
    expect(runnerPayload(current, draft).sandbox.work_duration).toEqual({
      protocol: "forge-work-duration-v2",
      timeout_seconds: null,
    });
    expect(JSON.stringify(current)).toBe(before);
  });
  it("accepts an explicit long project deadline without a one-hour ceiling", () => {
    const current = readRunnerSettings(saved, saved.repository_id);
    expect(
      runnerPayload(current, {
        ...runnerDraft(current),
        deadlineEnabled: true,
        deadline: "864000",
      }).sandbox.work_duration,
    ).toEqual({ protocol: "forge-work-duration-v2", timeout_seconds: 864000 });
  });
  it("keeps pool selections read-only instead of silently replacing their transport", () => {
    const current = readRunnerSettings(
      { ...saved, sandbox: { mode: "pool", profile_id: "retained" } },
      saved.repository_id,
    );
    expect(() => runnerDraft(current)).toThrow(/pool/i);
  });
});
