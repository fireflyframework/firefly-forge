/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
 * https://www.apache.org/licenses/LICENSE-2.0 */
import { createRequire } from "node:module";
import { readFileSync, mkdirSync, existsSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const require = createRequire(
  join(
    root,
    existsSync(join(root, "studio/package.json"))
      ? "studio/package.json"
      : "package.json",
  ),
);
const { chromium } = require("@playwright/test");
const input = JSON.parse(readFileSync(process.argv[2], "utf8"));
const font = readFileSync(input.font).toString("base64");
const logo = readFileSync(input.logo).toString("base64");
const escape = (value) =>
  String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
const browser = await chromium.launch({ headless: true });
const page = await browser.newPage({
  viewport: { width: 1200, height: 630 },
  deviceScaleFactor: 1,
});
await page.route(/^https?:/, (route) => route.abort("blockedbyclient"));
const requests = [];
page.on("request", (request) => {
  if (/^https?:/.test(request.url())) requests.push(request.url());
});
try {
  for (const card of input.cards) {
    await page.setContent(`<!doctype html><html lang="en"><head><meta charset="utf-8"><style>
      @font-face{font-family:Manrope;src:url(data:font/woff2;base64,${font}) format('woff2');font-weight:200 800;font-display:block}
      *{box-sizing:border-box}html,body{margin:0;width:1200px;height:630px;background:#f3f1eb;color:#272820;font-family:Manrope,sans-serif}
      .grid{position:absolute;inset:0;background-image:linear-gradient(#d8d4ca55 1px,transparent 1px),linear-gradient(90deg,#d8d4ca55 1px,transparent 1px);background-size:48px 48px;mask-image:linear-gradient(90deg,transparent 55%,#000)}
      main{position:relative;padding:58px 64px;height:100%;border-top:8px solid #ffb34a}
      .brand{width:580px;height:74px;object-fit:contain;object-position:left center}
      .kind{margin:20px 0 18px;font-size:18px;font-weight:700;letter-spacing:.1em;color:#865515}
      h1{font-size:66px;font-weight:650;line-height:1.12;letter-spacing:-.035em;margin:0;max-width:1064px;height:224px;display:flex;align-items:flex-start}
      .description{position:absolute;left:64px;top:433px;width:1024px;font-size:25px;line-height:1.45;color:#60645b;margin:0}
      footer{position:absolute;left:64px;right:64px;bottom:38px;border-top:1px solid #d8d4ca;padding-top:20px;display:flex;justify-content:space-between;align-items:center;gap:24px;font-size:17px;color:#60645b}
      .path{font-weight:600}.mark{display:flex;align-items:center;gap:10px}.dot{height:9px;width:9px;background:#b57921;border-radius:50%}
    </style></head><body><div class="grid"></div><main>
      <img class="brand" alt="${escape(card.site_name)}" src="data:image/svg+xml;base64,${logo}">
      <p class="kind">DOCUMENTATION</p><h1>${escape(card.page_title)}</h1>
      <p class="description">${escape(card.description)}</p>
      <footer><span class="path">${escape(new URL(input.site_url).hostname + new URL(input.site_url).pathname)}</span><span class="mark"><span class="dot"></span>Firefly Software Foundation</span></footer>
    </main></body></html>`);
    await page.evaluate(async () => {
      await document.fonts.ready;
      await Promise.all([...document.images].map((image) => image.decode()));
      const title = document.querySelector("h1");
      let size = 66;
      while (title.scrollHeight > title.clientHeight && size > 44)
        title.style.fontSize = `${--size}px`;
    });
    const errors = await page.evaluate(() => {
      const errors = [];
      const boxes = [".brand", ".kind", "h1", ".description", "footer"].map(
        (selector) => ({ selector, element: document.querySelector(selector) }),
      );
      for (const { selector, element } of boxes) {
        const box = element.getBoundingClientRect();
        if (
          box.left < 0 ||
          box.right > 1200 ||
          box.top < 0 ||
          box.bottom > 630 ||
          element.scrollHeight > element.clientHeight + 1 ||
          element.scrollWidth > element.clientWidth + 1
        )
          errors.push(selector + " exceeds its card bounds");
      }
      const description = document
        .querySelector(".description")
        .getBoundingClientRect();
      const footer = document.querySelector("footer").getBoundingClientRect();
      if (description.bottom + 12 > footer.top)
        errors.push("Description overlaps footer");
      if (!document.fonts.check("25px Manrope"))
        errors.push("Brand font is not ready");
      return errors;
    });
    if (errors.length || requests.length)
      throw new Error(
        `${card.page_title}: ${JSON.stringify({ errors, requests })}`,
      );
    const destination = resolve(input.site_dir, card.image_path);
    if (
      !destination.startsWith(resolve(input.site_dir) + "/") &&
      !destination.startsWith(resolve(input.site_dir) + "\\")
    )
      throw new Error("Card output escaped site directory");
    mkdirSync(dirname(destination), { recursive: true });
    await page.screenshot({ path: destination, animations: "disabled" });
  }
} finally {
  await browser.close();
}
console.log(
  `Generated ${input.cards.length} local Manrope social cards with checked text, image and footer bounds.`,
);
