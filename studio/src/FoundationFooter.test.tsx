/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { renderToStaticMarkup } from "react-dom/server";
import { expect, it } from "vitest";
import { FoundationFooter } from "./FoundationFooter";
it("renders the foundation credit, accessible heart and exact Apache license destination", () => {
  const html = renderToStaticMarkup(<FoundationFooter />);
  expect(html).toContain("© 2026 Firefly Software Foundation");
  expect(html).toContain("Made with");
  expect(html).toContain('role="img" aria-label="love"');
  expect(html).toContain('href="https://www.apache.org/licenses/LICENSE-2.0"');
  expect(html).toContain('rel="noopener noreferrer"');
  expect(html.match(/<footer/g)).toHaveLength(1);
});
