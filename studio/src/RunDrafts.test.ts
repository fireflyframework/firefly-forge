/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { expect, it } from "vitest";
import { ProductDraftBook } from "./ProductDrafts";
import { selectStandaloneRunSource } from "./RunDrafts";

it("an explicit source launch reconciles its destination without deleting retained inputs or step", () => {
  const book = new ProductDraftBook(),
    draft = book.repository("run:standalone:source-a");
  draft.write("repositoryId", "source-b", true);
  draft.write("brief", "  Keep these run instructions\n", true);
  draft.write("commands", ' [["pytest"]]\n', true);
  draft.write("step", 3);
  expect(selectStandaloneRunSource(book, "source-a")).toBe(draft);
  expect(draft.read("repositoryId", "")).toBe("source-a");
  expect(draft.read("brief", "")).toBe("  Keep these run instructions\n");
  expect(draft.read("commands", "")).toBe(' [["pytest"]]\n');
  expect(draft.read("step", 0)).toBe(3);
});

it("retargeting advances the edit version without changing another source or component draft", () => {
  const book = new ProductDraftBook(),
    a = book.repository("run:standalone:source-a"),
    b = book.repository("run:standalone:source-b"),
    component = book.repository("run:component:project-a/2/web");
  a.write("repositoryId", "source-b", true);
  const submittedVersion = a.editVersion;
  b.write("brief", "Keep B instructions", true);
  component.write("brief", "Keep component instructions", true);
  selectStandaloneRunSource(book, "source-a");
  expect(a.editVersion).toBeGreaterThan(submittedVersion);
  expect(b.read("brief", "")).toBe("Keep B instructions");
  expect(component.read("brief", "")).toBe("Keep component instructions");
});
