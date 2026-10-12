/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";
export default defineConfig(({ mode }) => ({
  plugins: [react()],
  server: {
    proxy: {
      "/api":
        loadEnv(mode, ".", "FORGE_DEV_").FORGE_DEV_API_URL ||
        "http://127.0.0.1:8787",
    },
  },
  build: { outDir: "dist", sourcemap: false },
}));
