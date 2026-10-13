/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { defineConfig } from "@playwright/test";
const port = Number(process.env.FORGE_DOCS_PORT ?? 5227);
export default defineConfig({
  testDir: "./tests",
  testMatch: "docs-walkthrough.spec.ts",
  workers: 1,
  timeout: 90000,
  use: { baseURL: `http://127.0.0.1:${port}`, headless: true },
  webServer: {
    command: `npm run dev -- --port ${port} --strictPort`,
    url: `http://127.0.0.1:${port}`,
    reuseExistingServer: false,
  },
});
