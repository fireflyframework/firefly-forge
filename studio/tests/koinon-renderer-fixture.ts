/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { createElement } from "react";
import { createRoot } from "react-dom/client";
import { Knowledge } from "../src/Knowledge";
import "../src/styles.css";
import "../src/Integrations.css";
const theme = new URLSearchParams(location.search).get("theme") ?? "light";
document.documentElement.dataset.theme = theme;
const mount = document.querySelector("#root") as HTMLElement;
mount.style.cssText = "padding:24px;max-width:1120px;margin:auto";
createRoot(mount).render(
  createElement(Knowledge, {
    connection: {
      base: location.origin + "/api/v1",
      token: "controlled-browser-fixture",
    },
    repositories: [
      {
        id: "a".repeat(32),
        name: "Controlled inventory fixture",
        url: "/fixture",
        provider: "git",
        default_branch: "main",
      },
    ],
  }),
);
