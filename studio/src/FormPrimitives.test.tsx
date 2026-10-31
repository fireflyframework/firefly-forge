/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { renderToStaticMarkup } from "react-dom/server";
import { expect, it } from "vitest";
import { FormField, ScrollRegion, StepIndicator } from "./FormPrimitives";
it("preserves the native input and binds its label and combined hint", () => {
  const html = renderToStaticMarkup(
    <FormField id="endpoint" label="Endpoint" hint="Use the configured API.">
      <input id="endpoint" aria-describedby="existing-help" required />
    </FormField>,
  );
  expect(html).toContain('for="endpoint"');
  expect(html).toContain('aria-describedby="existing-help endpoint-hint"');
  expect(html).toContain("<input");
  expect(html).toContain('required=""');
});
it("does not invent completed validation for a visited step", () => {
  const html = renderToStaticMarkup(
    <StepIndicator ordinal={2} state="visited" />,
  );
  expect(html).toContain('aria-label="Step 2, visited"');
  expect(html).toContain(">2<");
  expect(html).not.toContain("<svg");
});
it("exposes one named native field region without inventing a scroll state before layout", () => {
  const html = renderToStaticMarkup(
    <ScrollRegion label="Provider fields" bounded>
      <input aria-label="API endpoint" />
    </ScrollRegion>,
  );
  expect(html).toContain('aria-label="Provider fields"');
  expect(html.match(/class="wizard-field-scroll"/g)).toHaveLength(1);
  expect(html).not.toContain('tabindex="0"');
});
it("keeps page fields in native document flow without a second scroll region", () => {
  const html = renderToStaticMarkup(
    <ScrollRegion>
      <input aria-label="Model" />
    </ScrollRegion>,
  );
  expect(html).not.toContain('role="region"');
  expect(html).not.toContain("form-scroll-region");
  expect(html).not.toContain("form-scroll-status");
});
