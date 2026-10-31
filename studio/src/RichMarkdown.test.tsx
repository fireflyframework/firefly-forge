/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { describe, expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { RichMarkdown } from "./RichMarkdown";
describe("product Markdown safety", () => {
  it("uses a shared labeled snippet for fenced code while keeping inline code separate", () => {
    const html = renderToStaticMarkup(
      <RichMarkdown
        value={"Use `value` here.\n\n```py\n# Ready\nprint('España')\n```"}
      />,
    );
    expect(html).toContain('aria-label="Copy code"');
    expect(html).toContain("Python");
    expect(html).toContain("<code>value</code>");
    expect(html).toContain("España");
    expect(html.match(/aria-label="Copy code"/g)).toHaveLength(1);
  });
  it("renders formatted Markdown without interpreting rawHTML, unsafe links or loading remote images", () => {
    const html = renderToStaticMarkup(
      <RichMarkdown
        value={
          "**Inventory**\n\n<script>alert(1)</script>\n\n[unsafe](javascript:alert(1))\n\n[credential](https://user:secret@example.com)\n\n![tracking](https://example.com/private.png)\n\n[reference](https://example.com/docs)"
        }
      />,
    );
    expect(html).toContain("<strong>Inventory</strong>");
    expect(html).not.toContain("<script");
    expect(html).not.toContain("javascript:");
    expect(html).not.toContain("user:secret");
    expect(html).not.toContain("<img");
    expect(html).toContain('href="https://example.com/docs"');
    expect(html).toContain('rel="noopener noreferrer"');
  });
});
