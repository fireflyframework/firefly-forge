// @vitest-environment happy-dom
/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { useState } from "react";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, it } from "vitest";
import type { ProjectComponent, Repository } from "./api";
import {
  SourceComponentSelector,
  type SourceComponentFilters,
} from "./SourceComponentSelector";

afterEach(cleanup);
const repositories: Repository[] = [
  {
    id: "repo-a",
    name: "Inventory",
    url: "https://user:secretToken@example.test/team/api?token=secretToken",
    provider: "git",
    default_branch: "main",
  },
  {
    id: "repo-b",
    name: "Inventory",
    url: "https://example.test/team/workers",
    provider: "git",
    default_branch: "main",
  },
];
const components: ProjectComponent[] = Array.from(
  { length: 60 },
  (_, index) => ({
    id: `component-${index + 1}`,
    name: `Component ${String(index + 1).padStart(2, "0")}`,
    path: `apps/component-${index + 1}`,
    repository_id: index < 30 ? "repo-a" : "repo-b",
    kind: index < 30 ? "api" : "worker",
    backend: "pyfly",
    frontend: "none",
    responsibility: "Recorded component",
    depends_on: [],
    verification: [],
  }),
);
function Harness({
  items = components,
  initial = components.map((item) => item.id),
  disabled = false,
}: {
  items?: ProjectComponent[];
  initial?: string[];
  disabled?: boolean;
}) {
  const [selected, setSelected] = useState(initial);
  const [filters, setFilters] = useState<SourceComponentFilters>({
    query: "",
    kind: "",
    repository: "",
    selectedOnly: false,
    page: 0,
  });
  return (
    <>
      <SourceComponentSelector
        components={items}
        repositories={repositories}
        selected={selected}
        onSelectedChange={setSelected}
        filters={filters}
        onFiltersChange={setFilters}
        disabled={disabled}
      />
      <output aria-label="Selected IDs">{JSON.stringify(selected)}</output>
    </>
  );
}
const selectedIds = () =>
  JSON.parse(screen.getByLabelText("Selected IDs").textContent!);

it("applies bulk actions to all filtered pages while preserving selections outside that filter", () => {
  render(<Harness />);
  expect(screen.getAllByRole("checkbox", { name: /^Component/ })).toHaveLength(
    25,
  );
  fireEvent.change(screen.getByLabelText("Component type"), {
    target: { value: "api" },
  });
  expect(
    screen.getByRole("status", { name: "Component selection" }).textContent,
  ).toContain("30 results");
  fireEvent.click(screen.getByRole("button", { name: "Clear matching" }));
  expect(selectedIds()).toEqual(components.slice(30).map((item) => item.id));
  fireEvent.click(screen.getByRole("button", { name: "Select matching" }));
  expect(new Set(selectedIds())).toEqual(
    new Set(components.map((item) => item.id)),
  );
  fireEvent.click(
    screen.getByRole("button", { name: "Next component results" }),
  );
  expect(screen.getAllByRole("checkbox", { name: /^Component/ })).toHaveLength(
    5,
  );
  expect(screen.getByRole("checkbox", { name: /Component 30/ })).toHaveProperty(
    "checked",
    true,
  );
});

it("recovers an empty selected-only result without changing the selection or losing controls", () => {
  render(<Harness initial={[]} />);
  fireEvent.click(screen.getByLabelText("Selected only"));
  expect(screen.getByText("No components match these filters.")).toBeTruthy();
  expect(screen.getByRole("button", { name: "Clear matching" })).toHaveProperty(
    "disabled",
    true,
  );
  fireEvent.click(screen.getByRole("button", { name: "Reset filters" }));
  expect(screen.getAllByRole("checkbox", { name: /^Component/ })).toHaveLength(
    25,
  );
  expect(selectedIds()).toEqual([]);
});

it("searches name and path without hiding or replacing safe repository identities", () => {
  const { container } = render(<Harness />);
  fireEvent.change(screen.getByLabelText("Search components"), {
    target: { value: "APPS/COMPONENT-60" },
  });
  expect(screen.getByRole("checkbox", { name: /Component 60/ })).toBeTruthy();
  expect(
    screen.getByRole("status", { name: "Component selection" }).textContent,
  ).toContain("1 result");
  expect(container.textContent).toContain(
    "Inventory · example.test/team/workers · git · main",
  );
  expect(container.textContent).not.toContain("secretToken");
  expect(container.textContent).not.toContain("user:");
  fireEvent.change(screen.getByLabelText("Repository"), {
    target: { value: "repo-a" },
  });
  expect(screen.getByText("No components match these filters.")).toBeTruthy();
  expect(selectedIds()).toHaveLength(60);
});

it("does not cap bulk selection at a page or an arbitrary total", () => {
  const items = Array.from({ length: 5000 }, (_, index) => ({
    ...components[0],
    id: `large-${index}`,
    name: `Service ${index}`,
    path: `services/${index}`,
  }));
  render(<Harness items={items} initial={[]} />);
  fireEvent.click(screen.getByRole("button", { name: "Select matching" }));
  expect(selectedIds()).toEqual(items.map((item) => item.id));
  expect(screen.getAllByRole("checkbox", { name: /^Service/ })).toHaveLength(
    25,
  );
});

it("labels an unavailable repository explicitly and disables scope editing while busy", () => {
  render(
    <Harness
      items={[{ ...components[0], repository_id: "missing-repository" }]}
      disabled
    />,
  );
  expect(screen.getAllByText(/Unavailable repository/).length).toBeGreaterThan(
    0,
  );
  expect(screen.getByLabelText("Search components")).toHaveProperty(
    "disabled",
    true,
  );
  expect(screen.getByRole("checkbox", { name: /^Component/ })).toHaveProperty(
    "disabled",
    true,
  );
  expect(screen.getByRole("button", { name: "Clear matching" })).toHaveProperty(
    "disabled",
    true,
  );
});
