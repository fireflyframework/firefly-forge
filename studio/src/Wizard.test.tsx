/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { renderToStaticMarkup } from "react-dom/server";
import { expect, it } from "vitest";
import { Wizard } from "./Wizard";

const props = {
  steps: ["Connection", "Review"],
  active: 0,
  onStep: () => {},
  title: "Configure connection",
  description: "Keep fields and actions in the page flow.",
  children: (
    <form>
      <input aria-label="Scoped token" />
      <div className="wizard-actions">
        <button>Continue</button>
      </div>
    </form>
  ),
};

it("renders page fields and actions in natural flow", () => {
  const html = renderToStaticMarkup(<Wizard {...props} />);
  expect(html).toContain('data-layout="flow"');
  expect(html).toContain('aria-label="Scoped token"');
  expect(html).toContain("Continue");
});

it("allows a viewport container to reserve a field region before layout effects", () => {
  const html = renderToStaticMarkup(<Wizard {...props} layout="bounded" />);
  expect(html).toContain('data-layout="bounded"');
  expect(html).toContain('aria-label="Configure connection fields"');
  expect(html).toContain('tabindex="0"');
  expect(html).not.toContain("form-scroll-status");
});

it("keeps ordinary full-page fields in natural flow without a measured frame height", () => {
  const html = renderToStaticMarkup(<Wizard {...props} />);
  expect(html).toContain('data-layout="flow"');
  expect(html).not.toContain("--wizard-frame-height");
  expect(html).not.toContain("form-scroll-status");
});

it("keeps an ordinal and a separately named completion state for a completed step", () => {
  const html = renderToStaticMarkup(<Wizard {...props} active={1} />);
  expect(html).toContain('aria-label="Step 1, completed"');
  expect(html).toContain(">1<");
  expect(html).toContain('aria-current="step"');
});
it("does not describe a visited step as validated", () => {
  const html = renderToStaticMarkup(
    <Wizard {...props} active={1} progressMode="visited" />,
  );
  expect(html).toContain('aria-label="Step 1, visited"');
  expect(html).not.toContain('aria-label="Step 1, completed"');
});
it("offers descriptive rail steps and an accessible native selector without enabling future steps", () => {
  const html = renderToStaticMarkup(
    <Wizard
      {...props}
      variant="rail"
      stepDescriptions={["Identity and access", "Review settings"]}
    />,
  );
  expect(html).toContain("Identity and access");
  expect(html).toContain('aria-label="Setup step"');
  expect(html).toContain('<option value="1" disabled="">2. Review</option>');
});
