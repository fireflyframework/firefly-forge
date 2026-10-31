/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { useRef, useState } from "react";
import Markdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { Bold, Italic, List, Code2, Heading2, Eye, Pencil } from "lucide-react";
import type { Nodes, Root } from "mdast";
import { CodeSnippet } from "./CodeSnippet";

function retainCodeSource() {
  return (tree: Root) => {
    const visit = (node: Nodes) => {
      // Keep the logical source before HAST serialization adds its display newline.
      if (node.type === "code")
        node.data = {
          ...node.data,
          hProperties: { "data-snippet-source": node.value },
        };
      if ("children" in node) node.children.forEach(visit);
    };
    visit(tree);
  };
}

export function markdownUrl(value: string): string | undefined {
  if (
    value.trim() !== value ||
    [...value].some(
      (char) => char.charCodeAt(0) < 32 || char.charCodeAt(0) === 127,
    )
  )
    return undefined;
  if (
    value.startsWith("#") ||
    (value.startsWith("/") && !value.startsWith("//"))
  )
    return value;
  try {
    const url = new URL(value);
    return url.protocol === "https:" && !url.username && !url.password
      ? url.href
      : undefined;
  } catch {
    return undefined;
  }
}
export function RichMarkdown({ value }: { value: string }) {
  return (
    <div className="rich-markdown">
      <Markdown
        remarkPlugins={[remarkGfm, retainCodeSource]}
        skipHtml
        urlTransform={markdownUrl}
        components={{
          pre: ({ node, children }) => {
            const code = node?.children.find(
              (child) => child.type === "element" && child.tagName === "code",
            );
            if (
              code?.type !== "element" ||
              typeof code.properties["data-snippet-source"] !== "string"
            )
              return <pre>{children}</pre>;
            const names = code.properties.className;
            const language = Array.isArray(names)
              ? names.find(
                  (name) =>
                    typeof name === "string" && name.startsWith("language-"),
                )
              : undefined;
            return (
              <CodeSnippet
                value={code.properties["data-snippet-source"]}
                language={
                  typeof language === "string" ? language.slice(9) : undefined
                }
              />
            );
          },
          a: ({ href, children }) =>
            href ? (
              <a
                href={href}
                target={href.startsWith("https:") ? "_blank" : undefined}
                rel="noopener noreferrer"
              >
                {children}
              </a>
            ) : (
              <span>{children}</span>
            ),
          img: () => (
            <span className="markdown-image-note">
              Image references need an authorized upload.
            </span>
          ),
        }}
      >
        {value}
      </Markdown>
    </div>
  );
}
export function MarkdownEditor({
  id,
  label,
  value,
  onChange,
  required = false,
  hint,
}: {
  id: string;
  label: string;
  value: string;
  onChange: (value: string) => void;
  required?: boolean;
  hint?: string;
}) {
  const input = useRef<HTMLTextAreaElement>(null);
  const [preview, setPreview] = useState(false);
  function format(before: string, after = "") {
    const element = input.current;
    if (!element) return;
    const start = element.selectionStart,
      end = element.selectionEnd;
    const selected = value.slice(start, end);
    onChange(
      value.slice(0, start) + before + selected + after + value.slice(end),
    );
    requestAnimationFrame(() => {
      element.focus();
      element.setSelectionRange(start + before.length, end + before.length);
    });
  }
  return (
    <div className="field markdown-editor">
      <label htmlFor={id}>
        {label}
        {!required && <span className="field-optional">Optional</span>}
      </label>
      {hint && <small>{hint}</small>}
      <div
        className="markdown-toolbar"
        role="toolbar"
        aria-label="Markdown formatting"
      >
        <button
          type="button"
          aria-label="Bold text"
          title="Bold"
          disabled={preview}
          onClick={() => format("**", "**")}
        >
          <Bold size={20} />
        </button>
        <button
          type="button"
          aria-label="Italic text"
          title="Italic"
          disabled={preview}
          onClick={() => format("_", "_")}
        >
          <Italic size={20} />
        </button>
        <button
          type="button"
          aria-label="Add heading"
          title="Heading"
          disabled={preview}
          onClick={() => format("\n## ")}
        >
          <Heading2 size={20} />
        </button>
        <button
          type="button"
          aria-label="Add list item"
          title="List"
          disabled={preview}
          onClick={() => format("\n- ")}
        >
          <List size={20} />
        </button>
        <button
          type="button"
          aria-label="Inline code"
          title="Code"
          disabled={preview}
          onClick={() => format("`", "`")}
        >
          <Code2 size={20} />
        </button>
        <button
          type="button"
          className="markdown-preview-toggle"
          aria-pressed={preview}
          aria-label={preview ? "Edit description" : "Preview description"}
          onClick={() => setPreview((value) => !value)}
        >
          {preview ? <Pencil size={18} /> : <Eye size={18} />}{" "}
          {preview ? "Edit" : "Preview"}
        </button>
      </div>
      <div className="markdown-editor-columns single-pane">
        {!preview && (
          <textarea
            ref={input}
            id={id}
            value={value}
            required={required}
            maxLength={8000}
            rows={5}
            onChange={(event) => onChange(event.target.value)}
          />
        )}
        {preview && (
          <section
            className="markdown-live-preview"
            aria-label="Formatted description preview"
          >
            <small>Preview</small>
            {value ? (
              <RichMarkdown value={value} />
            ) : (
              <p className="subtle">Your formatted description appears here.</p>
            )}
          </section>
        )}
      </div>
    </div>
  );
}
