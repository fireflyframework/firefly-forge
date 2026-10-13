/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import type { CustomerProductDefinition } from "./api";
const titles = ["Context", "Journeys", "Features", "Integrations", "Review"];
export function buildStepPrompt(
  step: number,
  definition: CustomerProductDefinition,
) {
  const section = titles[step] ?? "Review";
  const parts = [
    `Help me review the ${section.toLowerCase()} step of this product definition. Ask concrete questions about gaps and suggest changes for me to review.`,
    "Current editor content below is not automatically saved or reviewed. It may differ from the saved product supplied by the server.",
    `Customer: ${definition.customer || "Not defined yet"}`,
    `Problem: ${definition.problem || "Not defined yet"}`,
  ];
  if (step === 0)
    parts.push(
      `Product owners: ${definition.owners.join(", ") || "Not assigned yet"}`,
    );
  if (step > 0)
    parts.push(
      "Customer journeys:",
      ...definition.journey.map((item, index) => `${index + 1}. ${item}`),
    );
  if (step === 1) parts.push("User stories:", ...definition.user_stories);
  if (step >= 2)
    parts.push(
      "Acceptance outcomes:",
      ...definition.acceptance,
      "Features:",
      ...(definition.features ?? []).map(
        (feature) =>
          `${feature.name}: ${feature.behaviors.map((behavior) => `Given ${behavior.given}; when ${behavior.when}; then ${behavior.then}. Failure handling: ${behavior.failure_paths.join("; ")}`).join(" ")}`,
      ),
    );
  if (step >= 3)
    parts.push(
      "Systems used:",
      ...(definition.integrations ?? []).map(
        (item) =>
          `${item.name} (${item.kind === "owned" ? "Our service" : "Third party"}, ${item.protocol}). Used by: ${item.journey_refs.join("; ")}. Failure handling: ${item.failure_paths.join("; ")}`,
      ),
    );
  const value = parts.join("\n\n");
  const note =
    "\n\nContext shortened to fit the message limit. Ask me about missing details rather than assuming them.";
  return value.length > 12000
    ? value.slice(0, 12000 - note.length) + note
    : value;
}
export function appendStepContext(message: string, context: string) {
  const remaining = 12000 - message.length - 2;
  if (remaining < 100)
    throw new Error(
      "Your message is too long to append this context. Shorten it or choose Replace message.",
    );
  const note = "\n\nContext shortened to fit the message limit.";
  return (
    message +
    "\n\n" +
    (context.length > remaining
      ? context.slice(0, remaining - note.length) + note
      : context)
  );
}
