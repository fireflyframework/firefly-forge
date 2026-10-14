/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import "./StudioFeatures.css";

export function SetupIllustration({
  kind,
  step,
}: {
  kind:
    | "application"
    | "product"
    | "cloud"
    | "collaboration"
    | "knowledge"
    | "learning"
    | "onboarding";
  step: number;
}) {
  const labels = {
    learning: [
      ["Evidence", "Knowledge"],
      ["Age", "Limits"],
      ["Policy", "New run"],
    ],
    knowledge: [
      ["File", "Workspace"],
      ["Title", "Source"],
      ["Text", "Review"],
      ["Receipt", "Ingestion"],
    ],
    onboarding: [
      ["Application", "Actors"],
      ["Command", "Permission"],
      ["Webhook", "Signature"],
      ["Receipt", "Project"],
    ],
    application: [
      ["Project", "Target"],
      ["Endpoint", "HTTP check"],
      ["Metrics", "Limits"],
      ["Owner", "Checks"],
    ],
    product: [
      ["Customer", "Problem"],
      ["Journey", "Stories"],
      ["Acceptance", "Security"],
      ["Brand", "Theme"],
      ["Owners", "Definition"],
    ],
    cloud: [
      ["Provider", "Image"],
      ["Network", "State"],
      ["Template", "Checks"],
      ["Target", "Review"],
    ],
    collaboration: [
      ["Repository", "Actors"],
      ["Comment", "Command"],
      ["Signature", "Receipt"],
      ["Run", "Feedback"],
    ],
  }[kind][step] ?? ["Settings", "Review"];
  return (
    <figure className="setup-illustration">
      <svg
        viewBox="0 0 240 72"
        role="img"
        aria-label={`${labels[0]} and ${labels[1]} setup`}
      >
        <path
          d="M10 4h90v60H10zM140 4h90v60h-90z"
          fill="var(--surface-subtle)"
          stroke="var(--line-strong)"
        />
        <path
          d="M100 35h40m-8-5 8 5-8 5"
          fill="none"
          stroke="var(--accent-ink)"
          strokeWidth="2"
        />
        <path
          d="M26 17h58M156 17h58M26 54h36M156 54h36"
          stroke="var(--line)"
          strokeWidth="2"
        />
        <text x="55" y="39" textAnchor="middle" fill="var(--text)">
          {labels[0]}
        </text>
        <text x="185" y="39" textAnchor="middle" fill="var(--text)">
          {labels[1]}
        </text>
      </svg>
    </figure>
  );
}
