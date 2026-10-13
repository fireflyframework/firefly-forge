/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
export function boundedText(value: string, characters = 10000, lines = 120) {
  const candidate = value.slice(0, characters);
  const parts = candidate.split("\n");
  const text = parts.slice(0, lines).join("\n");
  return { text, omitted: value.length - text.length };
}
export const fieldLabel = (value: string) => {
  const words = value.replaceAll("_", " ").replaceAll("-", " ");
  return words.charAt(0).toUpperCase() + words.slice(1);
};
export const asRecord = (
  value: unknown,
): Record<string, unknown> | undefined =>
  value !== null && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : undefined;

export type DiffLine = {
  kind: "addition" | "deletion" | "context" | "hunk" | "metadata";
  text: string;
  oldLine?: number;
  newLine?: number;
};
export type DiffFile = {
  path: string;
  lines: DiffLine[];
  additions: number;
  deletions: number;
};
function gitPath(value: string) {
  const trimmed = value.trim();
  let path = trimmed;
  if (trimmed.startsWith('"')) {
    try {
      path = JSON.parse(trimmed) as string;
    } catch {
      /* Keep Git's quoted path as text. */
    }
  }
  return path.replace(/^[ab]\//, "");
}
export function parseDiff(value: string): DiffFile[] {
  const files: DiffFile[] = [];
  let current: DiffFile | undefined;
  let oldLine: number | undefined;
  let newLine: number | undefined;
  let inHunk = false;
  let hasNewPath = false;
  for (const text of value.split("\n")) {
    if (text.startsWith("diff --git ")) {
      const match = text.match(
        /^diff --git ("(?:\\.|[^"\\])*"|\S+) ("(?:\\.|[^"\\])*"|\S+)$/,
      );
      current = {
        path: match ? gitPath(match[2]) : text.slice(11),
        lines: [],
        additions: 0,
        deletions: 0,
      };
      files.push(current);
      inHunk = false;
      hasNewPath = false;
    }
    if (!current) {
      current = { path: "Changes", lines: [], additions: 0, deletions: 0 };
      files.push(current);
    }
    if (text.startsWith("+++ ") && !inHunk && text.slice(4) !== "/dev/null") {
      current.path = gitPath(text.slice(4));
      hasNewPath = true;
    }
    if (
      text.startsWith("--- ") &&
      !inHunk &&
      !hasNewPath &&
      text.slice(4) !== "/dev/null"
    )
      current.path = gitPath(text.slice(4));
    if (text.startsWith("@@")) {
      const match = text.match(/^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@/);
      inHunk = Boolean(match);
      oldLine = match ? Number(match[1]) : undefined;
      newLine = match ? Number(match[2]) : undefined;
      current.lines.push({ kind: match ? "hunk" : "metadata", text });
    } else if (inHunk && text.startsWith("+")) {
      current.lines.push({ kind: "addition", text: text.slice(1), newLine });
      newLine = (newLine ?? 0) + 1;
      current.additions++;
    } else if (inHunk && text.startsWith("-")) {
      current.lines.push({ kind: "deletion", text: text.slice(1), oldLine });
      oldLine = (oldLine ?? 0) + 1;
      current.deletions++;
    } else if (inHunk && text.startsWith(" ")) {
      current.lines.push({
        kind: "context",
        text: text.slice(1),
        oldLine,
        newLine,
      });
      oldLine = (oldLine ?? 0) + 1;
      newLine = (newLine ?? 0) + 1;
    } else {
      current.lines.push({ kind: "metadata", text });
    }
  }
  return files;
}
export function tokenizeCode(value: string, path: string) {
  const known =
    /\.(?:[cm]?[jt]sx?|py|java|php|css|scss|json|ya?ml|sh|sql|rs|go)$/i.test(
      path,
    );
  if (!known || value.length > 4000) return [{ text: value, kind: "plain" }];
  const python = /\.(?:py|ya?ml|sh)$/i.test(path);
  const tokens: { text: string; kind: string }[] = [];
  const pattern = python
    ? /("(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|#[^\n]*|\b\d+(?:\.\d+)?\b|\b[A-Za-z_$][\w$]*\b)/g
    : /("(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|`(?:\\.|[^`\\])*`|\/\/[^\n]*|\/\*.*?\*\/|\b\d+(?:\.\d+)?\b|\b[A-Za-z_$][\w$]*\b)/g;
  const keywords = new Set(
    "const let var function return import export from class def async await if else elif for while try catch except finally throw raise new public private static void interface type extends implements null undefined true false True False None self this as in with package fn use select where create table".split(
      " ",
    ),
  );
  let position = 0;
  for (const match of value.matchAll(pattern)) {
    const start = match.index;
    if (start > position)
      tokens.push({ text: value.slice(position, start), kind: "plain" });
    const text = match[0];
    const kind = /^["'`]/.test(text)
      ? "string"
      : /^(?:#|\/\/|\/\*)/.test(text)
        ? "comment"
        : /^\d/.test(text)
          ? "number"
          : keywords.has(text)
            ? "keyword"
            : "plain";
    tokens.push({ text, kind });
    position = start + text.length;
  }
  if (position < value.length)
    tokens.push({ text: value.slice(position), kind: "plain" });
  return tokens;
}
