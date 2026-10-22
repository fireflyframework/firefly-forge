/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
const tasks = new Set([
  "implementation",
  "product",
  "architecture",
  "design",
  "review",
  "functions",
  "multimodal_qa",
  "communication",
]);
export type CommunicationConfiguration = {
  revision: number;
  digest: string;
  bindings: Record<string, string>;
  profiles: { id: string; name: string; provider: string }[];
};
function object(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== "object" || Array.isArray(value))
    throw new Error(
      "The current communication settings could not be verified.",
    );
  return value as Record<string, unknown>;
}
export function communicationConfiguration(
  profileValue: unknown,
  routeValue: unknown,
): CommunicationConfiguration {
  const route = object(routeValue),
    bindings = object(route.bindings);
  if (
    !Number.isSafeInteger(route.revision) ||
    Number(route.revision) < 0 ||
    typeof route.digest !== "string" ||
    !/^[a-f0-9]{64}$/.test(route.digest) ||
    !Array.isArray(profileValue)
  )
    throw new Error(
      "The current communication settings could not be verified.",
    );
  const profiles = profileValue
    .map(object)
    .filter(
      (profile) =>
        ["agentic", "bridge"].includes(String(profile.provider)) &&
        Array.isArray(profile.tasks) &&
        profile.tasks.includes("communication"),
    )
    .map((profile) => {
      if (
        typeof profile.id !== "string" ||
        !/^[a-z][a-z0-9-]{0,63}$/.test(profile.id) ||
        typeof profile.name !== "string" ||
        !profile.name ||
        profile.name.length > 120
      )
        throw new Error("The communication profile could not be verified.");
      return {
        id: profile.id,
        name: profile.name,
        provider: String(profile.provider),
      };
    });
  if (new Set(profiles.map((profile) => profile.id)).size !== profiles.length)
    throw new Error("Duplicate communication profiles are not supported.");
  const preserved: Record<string, string> = {};
  for (const [task, value] of Object.entries(bindings)) {
    if (
      !tasks.has(task) ||
      typeof value !== "string" ||
      !/^[a-z][a-z0-9-]{0,63}$/.test(value)
    )
      throw new Error("The current task routing could not be verified.");
    preserved[task] = value;
  }
  if (
    preserved.communication &&
    !profiles.some((profile) => profile.id === preserved.communication)
  )
    throw new Error(
      "The selected communication profile is no longer available. Review model profiles before changing routing.",
    );
  return {
    revision: Number(route.revision),
    digest: route.digest,
    bindings: preserved,
    profiles,
  };
}
export function communicationBindings(
  configuration: CommunicationConfiguration,
  selected: string,
) {
  if (
    selected &&
    !configuration.profiles.some((profile) => profile.id === selected)
  )
    throw new Error("Choose an available communication profile.");
  const bindings = { ...configuration.bindings };
  if (selected) bindings.communication = selected;
  else delete bindings.communication;
  return bindings;
}
export function communicationSaved(
  value: unknown,
  previous: CommunicationConfiguration,
  desired: Record<string, string>,
) {
  const profiles = previous.profiles.map((profile) => ({
    ...profile,
    tasks: ["communication"],
  }));
  const current = communicationConfiguration(profiles, value);
  if (
    current.revision <= previous.revision ||
    current.digest === previous.digest ||
    Object.keys(current.bindings).length !== Object.keys(desired).length ||
    Object.entries(desired).some(
      ([key, profile]) => current.bindings[key] !== profile,
    )
  )
    throw new Error(
      "The saved communication routing could not be verified. Read the current selection before deciding what to do next.",
    );
  return current;
}
export function communicationStatus(value: unknown): string {
  if (!value || typeof value !== "object" || Array.isArray(value)) return "";
  const evidence = value as Record<string, unknown>;
  if (evidence.mode === "ai" && evidence.reason === "generated")
    return "AI wording · recorded facts";
  const held = evidence.mode === "held";
  if (!held && evidence.mode !== "deterministic") return "";
  const reasons: Record<string, string> = {
    unconfigured: "no communication profile selected",
    quota: "provider usage limit reached",
    timeout: "provider did not respond in time",
    unavailable: "provider unavailable",
    invalid_output: "draft did not match the recorded facts",
    state_changed: "run state changed while drafting",
    channel_changed: "repository reply permissions changed while drafting",
  };
  if (held)
    Object.assign(reasons, {
      invalid_output: "AI message format could not be admitted",
      invalid_review: "AI review format could not be admitted",
      decision_too_large: "recorded question exceeds the public message limit",
      state_changed: "recorded facts changed while drafting",
      channel_changed:
        "reply destination or permissions changed while drafting",
    });
  const reason =
    typeof evidence.reason === "string" &&
    Object.hasOwn(reasons, evidence.reason)
      ? reasons[evidence.reason]
      : "";
  return `${held ? "AI update held" : "Recorded update"}${reason ? ` · ${reason}` : ""}`;
}
