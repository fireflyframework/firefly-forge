/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { useEffect, useRef, useState, type ReactNode } from "react";
import { Check, Copy } from "lucide-react";
import type { RootContent } from "hast";
import "./CodeSnippet.css";

const aliases: Record<string, string> = {
  py: "python",
  js: "javascript",
  jsx: "javascript",
  ts: "typescript",
  tsx: "typescript",
  yml: "yaml",
  sh: "bash",
  shell: "bash",
  html: "xml",
  cs: "csharp",
  "c#": "csharp",
};
const labels: Record<string, string> = {
  python: "Python",
  php: "PHP",
  javascript: "JavaScript",
  typescript: "TypeScript",
  sql: "SQL",
  json: "JSON",
  yaml: "YAML",
  bash: "Bash",
  xml: "HTML / XML",
  css: "CSS",
  java: "Java",
  rust: "Rust",
  csharp: "C#",
  go: "Go",
  text: "Text",
  plaintext: "Text",
  txt: "Text",
};
let highlighter:
  | Promise<ReturnType<(typeof import("lowlight"))["createLowlight"]>>
  | undefined;
function loadHighlighter() {
  highlighter ??= import("lowlight").then(({ common, createLowlight }) =>
    createLowlight(common),
  );
  return highlighter;
}
function sourceText(nodes: RootContent[]): string {
  return nodes
    .map((node) =>
      node.type === "text"
        ? node.value
        : node.type === "element"
          ? sourceText(node.children)
          : "",
    )
    .join("");
}
function renderNodes(nodes: RootContent[]): ReactNode[] {
  return nodes.map((node, index) => {
    if (node.type === "text") return node.value;
    if (node.type !== "element") return null;
    const children = renderNodes(node.children);
    if (node.tagName !== "span") return children;
    const classes = Array.isArray(node.properties.className)
      ? node.properties.className.filter(
          (name): name is string =>
            typeof name === "string" && /^hljs-[a-z0-9_-]+$/.test(name),
        )
      : [];
    return (
      <span key={index} className={classes.join(" ") || undefined}>
        {children}
      </span>
    );
  });
}
export function CodeSnippet({
  value,
  language,
}: {
  value: string;
  language?: string;
}) {
  return <SnippetContent key={value} value={value} language={language} />;
}
function SnippetContent({
  value,
  language,
}: {
  value: string;
  language?: string;
}) {
  const normalized = language?.trim().toLowerCase() || "text";
  const syntax = aliases[normalized] ?? normalized;
  const label = labels[syntax] ?? language ?? "Text";
  const [result, setResult] = useState<{
    value: string;
    syntax: string;
    nodes: RootContent[];
  } | null>(null);
  const [copied, setCopied] = useState<"" | "copied" | "unavailable">("");
  const copyAlive = useRef(true);
  useEffect(() => {
    copyAlive.current = true;
    return () => {
      copyAlive.current = false;
    };
  }, []);
  useEffect(() => {
    let alive = true;
    // Large snippets keep every character without expensive grammar parsing on the UI thread.
    if (
      !value ||
      value.length > 100_000 ||
      ["text", "txt", "plaintext"].includes(syntax)
    )
      return;
    void loadHighlighter()
      .then((parser) => {
        if (!alive || !parser.registered(syntax)) return;
        const nodes = parser.highlight(syntax, value).children;
        if (alive && sourceText(nodes) === value)
          setResult({ value, syntax, nodes });
      })
      .catch(() => {
        /* Plain source remains available if the grammar chunk cannot load. */
      });
    return () => {
      alive = false;
    };
  }, [value, syntax]);
  const current =
    result?.value === value && result.syntax === syntax ? result : null;
  async function copy() {
    try {
      await navigator.clipboard.writeText(value);
      if (copyAlive.current) setCopied("copied");
    } catch {
      if (copyAlive.current) setCopied("unavailable");
    }
  }
  return (
    <figure
      className="code-snippet"
      data-highlight={current ? "grammar" : "plain"}
    >
      <figcaption>
        <span title={label}>{label}</span>
        <button
          type="button"
          className="code-snippet-copy"
          aria-label="Copy code"
          onClick={() => void copy()}
        >
          {copied === "copied" ? (
            <Check size={14} aria-hidden="true" />
          ) : (
            <Copy size={14} aria-hidden="true" />
          )}
          {copied === "copied" ? "Copied" : "Copy"}
        </button>
      </figcaption>
      <pre tabIndex={0} aria-label={`${label} code`}>
        <code>{current ? renderNodes(current.nodes) : value}</code>
      </pre>
      <span
        className="code-snippet-status"
        data-copy-state={copied}
        role="status"
      >
        {copied === "unavailable"
          ? "Copy unavailable. Select the code to copy it."
          : copied === "copied"
            ? "Code copied."
            : ""}
      </span>
    </figure>
  );
}
