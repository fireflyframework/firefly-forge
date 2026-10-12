/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { useEffect, useState } from "react";

export type Appearance = "system" | "light" | "dark";
const key = "forge.appearance";

function savedAppearance(): Appearance {
  try {
    const saved = localStorage.getItem(key);
    return saved === "light" || saved === "dark" ? saved : "system";
  } catch {
    return "system";
  }
}

export function useAppearance() {
  const [appearance, setAppearance] = useState<Appearance>(savedAppearance);
  const [systemDark, setSystemDark] = useState(
    () => matchMedia("(prefers-color-scheme: dark)").matches,
  );
  const theme =
    appearance === "system" ? (systemDark ? "dark" : "light") : appearance;
  useEffect(() => {
    const query = matchMedia("(prefers-color-scheme: dark)");
    const update = () => setSystemDark(query.matches);
    query.addEventListener("change", update);
    return () => query.removeEventListener("change", update);
  }, []);
  useEffect(() => {
    document.documentElement.dataset.theme = theme;
    document
      .querySelector('meta[name="theme-color"]')
      ?.setAttribute("content", theme === "dark" ? "#10110f" : "#f3f1eb");
    document
      .querySelector('link[rel="icon"]')
      ?.setAttribute("href", `/assets/forge-favicon-${theme}.svg`);
    try {
      localStorage.setItem(key, appearance);
    } catch {
      // A blocked storage area still permits an appearance change for this tab.
    }
  }, [appearance, theme]);
  return { appearance, setAppearance, theme };
}
