/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { expect, it } from "vitest";
import {
  sourceTree,
  sourceBasename,
  sourceSize,
  sourceLanguage,
} from "./SourceTree";
import type { SourceEntry } from "./SourceExplorerData";
const entry = (path: string): SourceEntry => ({
  path,
  kind: path.endsWith("/") ? "directory" : "file",
  mode: path.endsWith("/") ? 16877 : 33188,
  byte_count: 2,
  sha256: path.endsWith("/") ? null : "a".repeat(64),
});
it("groups only this catalog page and preserves exact file indexes across shared parents and duplicate basenames", () => {
  const tree = sourceTree([
    entry("backend/"),
    entry("backend/api/app.py"),
    entry("frontend/api/app.py"),
  ]);
  expect(tree.map((node) => node.name)).toEqual(["backend", "frontend"]);
  expect(tree[0].children[0].children[0]).toEqual({
    name: "app.py",
    path: "backend/api/app.py",
    entryIndex: 1,
    children: [],
  });
  expect(tree[1].children[0].children[0]).toEqual({
    name: "app.py",
    path: "frontend/api/app.py",
    entryIndex: 2,
    children: [],
  });
  expect(sourceTree([entry("empty/")])[0].children).toEqual([]);
});
it("uses readable sizes and explicit file languages without guessing grammar for unknown extensions", () => {
  expect(sourceBasename("backend/api/stock.py")).toBe("stock.py");
  expect(sourceSize(4096)).toBe("4 KiB");
  expect(sourceSize(2)).toBe("2 B");
  expect(sourceLanguage("backend/api/stock.py")).toBe("python");
  expect(sourceLanguage("views/Stock.tsx")).toBe("typescript");
  expect(sourceLanguage("unknown.private-format")).toBe("text");
});
