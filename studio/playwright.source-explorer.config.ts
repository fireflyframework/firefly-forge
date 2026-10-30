/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { defineConfig } from "@playwright/test";
export default defineConfig({
  testDir: "./tests",
  testMatch: "source-explorer.spec.ts",
  workers: 1,
  timeout: 30000,
  globalTimeout: 150000,
  use: {
    baseURL: "http://127.0.0.1:5197",
    trace: "off",
    screenshot: "only-on-failure",
  },
  webServer: {
    command: "node tests/source-explorer-server.mjs",
    url: "http://127.0.0.1:5197/__source_explorer__",
    reuseExistingServer: false,
    timeout: 20000,
    gracefulShutdown: { signal: "SIGTERM", timeout: 2000 },
  },
});
