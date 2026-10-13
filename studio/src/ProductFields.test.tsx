/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { ProductList } from "./ProductFields";

it("names add actions using the same singular item as remove actions", () => {
  for (const [label, item] of [
    ["User stories", "user story"],
    ["Acceptance criteria", "acceptance criterion"],
  ]) {
    const html = renderToStaticMarkup(
      <ProductList
        label={label}
        id="items"
        value="Existing item"
        onChange={() => {}}
        hint="Add one observable item."
        compact
      />,
    );
    expect(html).toContain(`aria-label="Add ${item}"`);
    expect(html).toContain(`title="Add ${item}"`);
    expect(html).toContain(`aria-label="Remove ${item} 1"`);
  }
});

it("wraps long product statements while keeping owner names compact", () => {
  const render = (label: string) =>
    renderToStaticMarkup(
      <ProductList
        label={label}
        id="items"
        value="An operator needs a long observable statement to remain readable."
        onChange={() => {}}
        hint="One item per entry."
      />,
    );
  expect(render("User stories")).toContain("<textarea");
  expect(render("Acceptance criteria")).toContain("<textarea");
  expect(render("Product owners")).toContain("<input");
  expect(render("Product owners")).not.toContain("<textarea");
});
