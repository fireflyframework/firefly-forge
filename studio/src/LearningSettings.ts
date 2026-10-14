/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
export type LearningValues = {
  enabled: boolean;
  projected_retrieval: boolean;
  max_age_hours: number;
  max_candidates: number;
  max_lookup_calls: number;
  max_repeated_attempts: number;
};
export type LearningSettings = {
  version: 1;
  revision: string | null;
  digest: string;
  settings: LearningValues;
  projectedSupported: boolean;
};
const invalid = () =>
  new Error(
    "The server returned unsupported learning settings. Reload before making changes.",
  );
function values(value: unknown): LearningValues {
  if (!value || typeof value !== "object" || Array.isArray(value))
    throw invalid();
  const record = value as Record<string, unknown>;
  const keys = [
    "enabled",
    "projected_retrieval",
    "max_age_hours",
    "max_candidates",
    "max_lookup_calls",
    "max_repeated_attempts",
  ];
  if (
    Object.keys(record).some((key) => !keys.includes(key)) ||
    typeof record.enabled !== "boolean" ||
    (record.projected_retrieval !== undefined &&
      typeof record.projected_retrieval !== "boolean")
  )
    throw invalid();
  for (const [key, maximum] of [
    ["max_age_hours", 720],
    ["max_candidates", 16],
    ["max_lookup_calls", 100],
    ["max_repeated_attempts", 3],
  ] as const) {
    const number = record[key];
    if (
      typeof number !== "number" ||
      !Number.isInteger(number) ||
      number < 1 ||
      number > maximum
    )
      throw invalid();
  }
  return {
    enabled: record.enabled,
    projected_retrieval: record.projected_retrieval === true,
    max_age_hours: record.max_age_hours as number,
    max_candidates: record.max_candidates as number,
    max_lookup_calls: record.max_lookup_calls as number,
    max_repeated_attempts: record.max_repeated_attempts as number,
  };
}
export function readLearningSettings(value: unknown): LearningSettings {
  if (!value || typeof value !== "object" || Array.isArray(value))
    throw invalid();
  const record = value as Record<string, unknown>;
  if (
    record.version !== 1 ||
    !(
      record.revision === null ||
      (typeof record.revision === "string" &&
        /^[a-f0-9]{32}$/.test(record.revision))
    ) ||
    typeof record.digest !== "string" ||
    !/^[a-f0-9]{64}$/.test(record.digest)
  )
    throw invalid();
  const settings = values(record.settings);
  return {
    version: 1,
    revision: record.revision as string | null,
    digest: record.digest,
    settings,
    projectedSupported: Object.hasOwn(
      record.settings as object,
      "projected_retrieval",
    ),
  };
}
export function learningPayload(
  saved: LearningSettings,
  draft: LearningValues,
): Record<string, boolean | number> {
  const checked = values(draft);
  if (!saved.projectedSupported && checked.projected_retrieval)
    throw new Error(
      "Upgrade the server before enabling projected learning retrieval.",
    );
  const { projected_retrieval, ...base } = checked;
  return saved.projectedSupported ? { ...base, projected_retrieval } : base;
}
