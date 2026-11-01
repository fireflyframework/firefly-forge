/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import type { SourceEntry } from "./SourceExplorerData";
export type SourceTreeNode = {
  path: string;
  name: string;
  entryIndex: number | null;
  children: SourceTreeNode[];
};
export function sourceTree(entries: SourceEntry[]): SourceTreeNode[] {
  const roots: SourceTreeNode[] = [],
    folders = new Map<string, SourceTreeNode>();
  entries.forEach((entry, entryIndex) => {
    const parts = entry.path.replace(/\/$/, "").split("/");
    let nodes = roots;
    parts.forEach((name, depth) => {
      const directory = depth < parts.length - 1 || entry.kind === "directory";
      const path = parts.slice(0, depth + 1).join("/") + (directory ? "/" : "");
      let node = directory ? folders.get(path) : undefined;
      if (!node) {
        node = {
          path,
          name,
          entryIndex: directory ? null : entryIndex,
          children: [],
        };
        nodes.push(node);
        if (directory) folders.set(path, node);
      }
      nodes = node.children;
    });
  });
  return roots;
}
export const sourceBasename = (path: string) =>
  path.replace(/\/$/, "").split("/").at(-1) || path;
export function sourceSize(bytes: number) {
  if (bytes < 1024) return `${bytes.toLocaleString("en-US")} B`;
  return `${(bytes / (bytes < 1048576 ? 1024 : 1048576)).toLocaleString("en-US", { maximumFractionDigits: 1 })} ${bytes < 1048576 ? "KiB" : "MiB"}`;
}
export function sourceLanguage(path: string) {
  const extension = path.split(".").at(-1)?.toLowerCase() ?? "";
  return (
    (
      {
        py: "python",
        ts: "typescript",
        tsx: "typescript",
        js: "javascript",
        jsx: "javascript",
        mjs: "javascript",
        cjs: "javascript",
        json: "json",
        yaml: "yaml",
        yml: "yaml",
        sh: "bash",
        bash: "bash",
        html: "html",
        xml: "html",
        css: "css",
        scss: "css",
        sql: "sql",
        java: "java",
        rs: "rust",
        go: "go",
        cs: "csharp",
        php: "php",
      } as Record<string, string>
    )[extension] ?? "text"
  );
}
