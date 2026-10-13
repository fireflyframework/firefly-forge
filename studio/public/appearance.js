/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
try {
  const choice = localStorage.getItem("forge.appearance");
  document.documentElement.dataset.theme = choice === "light" || choice === "dark" ? choice : matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
} catch {
  document.documentElement.dataset.theme = matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}
