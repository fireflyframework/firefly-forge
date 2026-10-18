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
