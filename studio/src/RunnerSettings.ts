/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
export type RunnerSettings = {
  repository_id: string;
  revision: string | null;
  source: string;
  sandbox: Record<string, unknown> & { mode: "host" | "docker" | "pool" };
};
export type RunnerDraft = {
  mode: "host" | "docker";
  monitor: boolean;
  deadlineEnabled: boolean;
  deadline: string;
  output: string;
  image: string;
  context: string;
};
const integer = (
  value: string,
  minimum: number,
  maximum: number,
  label: string,
) => {
  if (
    !/^[0-9]+$/.test(value) ||
    !Number.isSafeInteger(Number(value)) ||
    Number(value) < minimum ||
    Number(value) > maximum
  )
    throw new Error(
      `${label} must be a whole number from ${minimum.toLocaleString()} to ${maximum.toLocaleString()}.`,
    );
  return Number(value);
};
function workDeadline(value: unknown): number | null {
  if (value === undefined) return null;
  if (!value || typeof value !== "object" || Array.isArray(value))
    throw new Error("The saved work duration policy could not be read.");
  const policy = value as Record<string, unknown>;
  if (
    Object.keys(policy).length !== 2 ||
    policy.protocol !== "forge-work-duration-v2" ||
    !(
      policy.timeout_seconds === null ||
      (typeof policy.timeout_seconds === "number" &&
        Number.isSafeInteger(policy.timeout_seconds) &&
        policy.timeout_seconds > 0)
    )
  )
    throw new Error("The saved work duration policy could not be read.");
  return policy.timeout_seconds as number | null;
}
function chosenDeadline(value: string) {
  if (
    !/^[0-9]+$/.test(value) ||
    !Number.isSafeInteger(Number(value)) ||
    Number(value) < 1
  )
    throw new Error(
      "Enter a positive whole number of seconds, or leave the work deadline off.",
    );
  return Number(value);
}
export function readRunnerSettings(
  value: unknown,
  repository: string,
): RunnerSettings {
  if (!value || typeof value !== "object")
    throw new Error("Runner settings could not be read.");
  const v = value as RunnerSettings;
  if (
    v.repository_id !== repository ||
    !(
      v.revision === null ||
      (typeof v.revision === "string" && /^[a-f0-9]{32}$/.test(v.revision))
    ) ||
    typeof v.source !== "string" ||
    !v.sandbox ||
    typeof v.sandbox !== "object" ||
    !["host", "docker", "pool"].includes(v.sandbox.mode)
  )
    throw new Error(
      "The server returned unsupported runner settings. Reload before saving.",
    );
  if (v.sandbox.mode !== "pool") {
    if (
      typeof v.sandbox.timeout !== "number" ||
      typeof v.sandbox.max_output_bytes !== "number" ||
      (v.sandbox.process_monitor !== undefined &&
        typeof v.sandbox.process_monitor !== "boolean")
    )
      throw new Error(
        "The saved runner policy could not be read. Reload before saving.",
      );
    integer(String(v.sandbox.timeout), 1, 3600, "Legacy command timeout");
    integer(String(v.sandbox.max_output_bytes), 1024, 2_000_000, "Log size");
    workDeadline(v.sandbox.work_duration);
  }
  return structuredClone(v);
}
export function runnerDraft(settings: RunnerSettings): RunnerDraft {
  if (settings.sandbox.mode === "pool")
    throw new Error(
      "Manage the retained pool profile through the runner pool configuration.",
    );
  return {
    mode: settings.sandbox.mode,
    monitor: settings.sandbox.process_monitor === true,
    deadlineEnabled: workDeadline(settings.sandbox.work_duration) !== null,
    deadline: String(workDeadline(settings.sandbox.work_duration) ?? ""),
    output: String(settings.sandbox.max_output_bytes),
    image: String(settings.sandbox.image ?? ""),
    context: String(settings.sandbox.context ?? ""),
  };
}
export function runnerPayload(
  settings: RunnerSettings,
  draft: RunnerDraft,
): { sandbox: Record<string, unknown> } {
  if (settings.sandbox.mode === "pool")
    throw new Error("A pool selection cannot be replaced in this form.");
  if (!["host", "docker"].includes(draft.mode))
    throw new Error("Choose a supported runner.");
  if (draft.monitor && draft.mode !== "host")
    throw new Error(
      "Process monitoring currently requires a trusted host runner.",
    );
  if (
    draft.mode === "docker" &&
    (!/^[a-z0-9][a-z0-9._:/-]{0,255}@sha256:[a-f0-9]{64}$/.test(draft.image) ||
      !/^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,99}$/.test(draft.context))
  )
    throw new Error(
      "Enter an image pinned by its SHA256 digest and an explicit Docker context.",
    );
  const sandbox = {
    ...structuredClone(settings.sandbox),
    mode: draft.mode,
    process_monitor: draft.monitor,
    work_duration: {
      protocol: "forge-work-duration-v2",
      timeout_seconds: draft.deadlineEnabled
        ? chosenDeadline(draft.deadline)
        : null,
    },
    max_output_bytes: integer(draft.output, 1024, 2_000_000, "Log size"),
    ...(draft.mode === "host"
      ? {
          image: null,
          context: null,
          browser: null,
          network: "none",
          workspace_transport: "bind",
          pool_binding: null,
        }
      : { image: draft.image, context: draft.context }),
  };
  return { sandbox };
}
