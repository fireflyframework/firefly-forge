// @vitest-environment happy-dom
/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { afterEach, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { FactoryProcessGuide } from "./FactoryProcessGuide";

afterEach(cleanup);

it("opens existing workflow destinations without creating or changing work", () => {
  const navigate = vi.fn();
  const { container } = render(
    <FactoryProcessGuide canConfigure onNavigate={navigate} />,
  );
  expect(container.querySelector("details")?.open).toBe(false);
  expect(screen.getAllByRole("listitem")).toHaveLength(5);
  for (const [name, destination] of [
    ["Connections", "settings"],
    ["Product definition", "product"],
    ["Product projects", "projects"],
    ["Start a run", "new"],
    ["Deployment", "delivery"],
    ["Repository workflow", "collaboration"],
  ]) {
    fireEvent.click(screen.getByRole("button", { name }));
    expect(navigate).toHaveBeenLastCalledWith(destination);
  }
  expect(navigate).toHaveBeenCalledTimes(6);
  expect(container.querySelector("form")).toBeNull();
  expect(container.querySelector('[role="progressbar"]')).toBeNull();
});

it("directs members to their source instead of factory configuration", () => {
  const navigate = vi.fn();
  render(<FactoryProcessGuide canConfigure={false} onNavigate={navigate} />);
  expect(screen.queryByRole("button", { name: "Connections" })).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "Repositories" }));
  expect(navigate).toHaveBeenCalledExactlyOnceWith("repositories");
  expect(screen.getByRole("link").getAttribute("href")).toBe(
    "https://fireflyframework.github.io/firefly-forge/factory-journeys/",
  );
});
