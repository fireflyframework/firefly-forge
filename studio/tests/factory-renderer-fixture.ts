/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { createElement } from "react";
import { createRoot } from "react-dom/client";
import { FactoryScene, type FactoryStation } from "../src/FactoryScene";
import "../src/styles.css";

(document.querySelector("#root") as HTMLElement).style.display = "none";
const mount = document.createElement("div");
mount.className = "factory-fixture";
mount.style.cssText = "width:1100px;margin:24px auto";
document.body.appendChild(mount);
const root = createRoot(mount);
const ids = [
  "prepare",
  "implement",
  "verify",
  "checkpoint",
  "publish",
  "deploy",
  "health",
];
let state: FactoryStation["state"] = "waiting",
  selectedId = "implement",
  suspended = false;
const render = () =>
  root.render(
    createElement(FactoryScene, {
      stations: ids.map((id) => ({
        id,
        label: id,
        state:
          id === "implement" ? state : id === "prepare" ? "done" : "waiting",
        detail: "Renderer fixture; no server production is inferred.",
      })),
      theme: "dark",
      selectedId,
      suspended,
      onSelect: (id: string) => {
        selectedId = id;
        render();
      },
    }),
  );
document.addEventListener("factory-fixture-state", (event) => {
  const detail = (
    event as CustomEvent<{ state: FactoryStation["state"]; suspended: boolean }>
  ).detail;
  state = detail.state;
  suspended = detail.suspended;
  render();
});
document.addEventListener("factory-fixture-visibility", (event) => {
  Object.defineProperty(document, "hidden", {
    configurable: true,
    value: (event as CustomEvent<boolean>).detail,
  });
  document.dispatchEvent(new Event("visibilitychange"));
});
document.addEventListener("factory-fixture-unmount", () => root.unmount(), {
  once: true,
});
render();
