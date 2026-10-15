/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
 * https://www.apache.org/licenses/LICENSE-2.0 */
import { createRequire } from "node:module";
import { renderedDiagrams } from "./mermaid-runtime.mjs";
const { test, expect } = createRequire(
  new URL("../../studio/package.json", import.meta.url),
)("@playwright/test");

const diagram =
  '<div class="mermaid"><svg xmlns="http://www.w3.org/2000/svg" width="200" height="80"><rect width="200" height="80" /></svg></div>';

test("a diagram detached during the CDP read cannot satisfy the current diagram count", async ({
  page,
}) => {
  await page.setContent(diagram);
  const context = page.context();
  const original = context.newCDPSession;
  let replaced = false;
  context.newCDPSession = async (...args) => {
    const session = await original.apply(context, args);
    const send = session.send.bind(session);
    session.send = async (method, params) => {
      if (method === "Runtime.callFunctionOn" && !replaced) {
        replaced = true;
        await page.locator(".mermaid svg").evaluate((svg) => {
          svg.replaceWith(svg.cloneNode(true));
        });
      }
      return send(method, params);
    };
    return session;
  };
  try {
    expect(await renderedDiagrams(page)).toEqual([]);
    expect(replaced).toBe(true);
    const current = await renderedDiagrams(page);
    expect(current).toHaveLength(1);
    expect(current[0].width).toBe(200);
    expect(current[0].height).toBe(80);
  } finally {
    context.newCDPSession = original;
  }
});

test("a connected zero-sized SVG remains visible to the strict geometry assertions", async ({
  page,
}) => {
  await page.setContent(diagram);
  await page.locator(".mermaid svg").evaluate((svg) => {
    svg.setAttribute("width", "0");
    svg.setAttribute("height", "0");
  });
  const current = await renderedDiagrams(page);
  expect(current).toHaveLength(1);
  expect(current[0].width).toBe(0);
  expect(current[0].height).toBe(0);
});
