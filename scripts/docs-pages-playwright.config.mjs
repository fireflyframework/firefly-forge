/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
 * https://www.apache.org/licenses/LICENSE-2.0 */
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";
const { defineConfig } = createRequire(
  new URL("../studio/package.json", import.meta.url),
)("@playwright/test");
export default defineConfig({
  testDir: "./docs-browser",
  testMatch: "pages-path.spec.mjs",
  workers: 1,
  timeout: 30000,
  use: { baseURL: "http://127.0.0.1:8796/firefly-forge/", headless: true },
  webServer: {
    command: `"${process.env.BRIDGE_PYTHON ?? "python3"}" -m http.server 8796 --bind 127.0.0.1 --directory build/docs-pages`,
    cwd: fileURLToPath(new URL("..", import.meta.url)),
    url: "http://127.0.0.1:8796/firefly-forge/",
    reuseExistingServer: false,
  },
});
