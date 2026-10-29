/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { defineConfig } from "@playwright/test";
const port = Number(process.env.FORGE_DOCS_PORT ?? 5227);
export default defineConfig({
  testDir: "./tests",
  testMatch: [
    "docs-walkthrough.spec.ts",
    "external-tools.spec.ts",
    "docs-provider-allowance.spec.ts",
    "docs-coding-check-recovery.spec.ts",
    "development-workflow.spec.ts",
    "framework-knowledge.spec.ts",
    "invalid-read-recovery.spec.ts",
    "runner-setup.spec.ts",
    "final-outcome-recovery.spec.ts",
    "verification-reuse.spec.ts",
  ],
  grep: /guided fixture journey|actual fixture review frames|actual fixture final allowance frames|actual fixture configured-check recovery frames|actual fixture development workflow frames|generation activation keeps exact corpus|review uses four acknowledgements|guided checkpoint continuation frames|guided runner process setup frames|saved final-result wizard fits|reused verification fits/,
  workers: 1,
  timeout: 90000,
  use: {
    baseURL: `http://127.0.0.1:${port}`,
    headless: true,
    launchOptions: { ignoreDefaultArgs: ["--hide-scrollbars"] },
  },
  webServer: {
    command: `npm run dev -- --port ${port} --strictPort --configLoader runner`,
    url: `http://127.0.0.1:${port}`,
    reuseExistingServer: false,
  },
});
