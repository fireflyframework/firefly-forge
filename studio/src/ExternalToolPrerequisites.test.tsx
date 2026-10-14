/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { ExternalToolPrerequisites } from "./ExternalToolPrerequisites";

for (const [id, name] of [
  ["figma", "Figma"],
  ["sentry", "Sentry"],
  ["grafana", "Grafana"],
  ["playwright", "Playwright"],
])
  it(`${name} preserves the actual prerequisite state without credential inputs or readiness claims`, () => {
    const source = Object.freeze({
      id,
      name,
      endpoint: null,
      prerequisite: "<img src=x onerror=alert(1)> Required adapter",
      enabled: false,
      credential_configured: false,
      readiness: "prerequisite_required",
      discovery: null,
      review: null,
    });
    const html = renderToStaticMarkup(
      <ExternalToolPrerequisites connector={source} onAgents={() => {}} />,
    );
    expect(html).toContain("Prerequisite required");
    expect(html).toContain("Not checked by this guide");
    expect(html).toContain("Unavailable");
    expect(html).not.toContain("<input");
    expect(html).not.toContain("<img");
    expect(html).toContain("&lt;img");
    expect(source.enabled).toBe(false);
  });
