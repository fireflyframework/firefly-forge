/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { defineConfig } from "@playwright/test";
export default defineConfig({
  testDir: "./tests",
  testMatch: "git-diff-browser.spec.ts",
  workers: 1,
  timeout: 30000,
  globalTimeout: 150000,
  outputDir: "./test-results/git-diff",
  use: {
    baseURL: "http://127.0.0.1:5181",
    trace: "off",
    screenshot: "only-on-failure",
  },
  webServer: {
    command:
      "node node_modules/vite/bin/vite.js --host 127.0.0.1 --port 5181 --strictPort",
    url: "http://127.0.0.1:5181",
    reuseExistingServer: false,
    timeout: 20000,
    gracefulShutdown: { signal: "SIGTERM", timeout: 2000 },
  },
});
