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

it("activates the flow layout contract when pageFlow is explicit", () => {
  const html = renderToStaticMarkup(<Wizard {...props} pageFlow />);
  expect(html).toContain('data-layout="flow"');
  expect(html).toContain('aria-label="Scoped token"');
  expect(html).toContain("Continue");
});

it("leaves measured wizards to choose their layout after mounting", () => {
  const html = renderToStaticMarkup(<Wizard {...props} />);
  expect(html).not.toContain('data-layout="flow"');
});

it("leaves a parent-managed wizard outside the global flow layout", () => {
  const html = renderToStaticMarkup(<Wizard {...props} parentLayout />);
  expect(html).toContain('data-layout="parent"');
  expect(html).not.toContain('data-layout="flow"');
  expect(html).toContain('aria-label="Scoped token"');
  expect(html).toContain("Continue");
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
