// @vitest-environment happy-dom
/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { afterEach, expect, it, vi } from "vitest";
import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { CodeSnippet } from "./CodeSnippet";
import { RichMarkdown } from "./RichMarkdown";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});
const samples = [
  ["py", "def reserve(quantity):\n    return quantity + 1\n"],
  ["php", "<?php\nfunction reserve($quantity) { return $quantity + 1; }\n"],
  ["js", 'const reply = "Ready";\n'],
  ["ts", "interface Reply { ready: boolean }\n"],
  ["tsx", "const Reply = () => <div>Ready</div>;\n"],
  ["sql", "SELECT quantity FROM inventory WHERE active = TRUE;\n"],
  ["json", '{"ready": true, "count": 2}\n'],
  ["yaml", "ready: true\ncount: 2\n"],
  ["bash", '#!/bin/bash\necho "$HOME"\n'],
  ["html", '<div class="reply">Ready</div>\n'],
  ["css", ".reply { color: red; }\n"],
  ["java", "public class Reply { int count = 2; }\n"],
  ["rust", 'fn main() { println!("Ready"); }\n'],
  ["csharp", "public class Reply { public int Count = 2; }\n"],
  ["go", 'package main\nfunc main() { println("Ready") }\n'],
];
it.each(samples)(
  "loads the real common grammar for %s and preserves its complete source",
  async (language, value) => {
    const { container } = render(
      <CodeSnippet value={value} language={language} />,
    );
    await waitFor(() =>
      expect(
        container.querySelector("figure")?.getAttribute("data-highlight"),
      ).toBe("grammar"),
    );
    expect(container.querySelector("pre code")?.textContent).toBe(value);
    expect(container.querySelector("code [class^=hljs-]")).not.toBeNull();
  },
);
it("parses multiline strings as one grammar node", async () => {
  const value = '"""First line\nSecond line"""\nprint("Ready")\n';
  const { container } = render(<CodeSnippet value={value} language="py" />);
  await waitFor(() =>
    expect(
      [...container.querySelectorAll(".hljs-string")].some((node) =>
        node.textContent?.includes("First line\nSecond line"),
      ),
    ).toBe(true),
  );
  expect(container.querySelector("pre code")?.textContent).toBe(value);
});
it("copies exact Unicode/newlines and resets confirmation when the source changes", async () => {
  const writeText = vi.fn().mockResolvedValue(undefined);
  vi.stubGlobal("navigator", { clipboard: { writeText } });
  const value = "\tEspaña 😀\r\n\n";
  const view = render(<CodeSnippet value={value} language="text" />);
  fireEvent.click(screen.getByRole("button", { name: "Copy code" }));
  await waitFor(() =>
    expect(screen.getByRole("status").textContent).toBe("Code copied."),
  );
  expect(writeText).toHaveBeenCalledExactlyOnceWith(value);
  expect(screen.getByRole("status").getAttribute("data-copy-state")).toBe(
    "copied",
  );
  view.rerender(<CodeSnippet value={"Changed\n"} language="text" />);
  expect(screen.getByRole("button", { name: "Copy code" }).textContent).toBe(
    "Copy",
  );
});
it.each([false, true])(
  "does not confirm a pending copy after source changes (return to original: %s)",
  async (returnToOriginal) => {
    let release = () => {};
    vi.stubGlobal("navigator", {
      clipboard: {
        writeText: vi.fn(
          () =>
            new Promise<void>((resolve) => {
              release = resolve;
            }),
        ),
      },
    });
    const view = render(<CodeSnippet value="Original" />);
    fireEvent.click(screen.getByRole("button", { name: "Copy code" }));
    view.rerender(<CodeSnippet value="Changed" />);
    if (returnToOriginal) view.rerender(<CodeSnippet value="Original" />);
    await act(async () => release());
    expect(screen.getByRole("button", { name: "Copy code" }).textContent).toBe(
      "Copy",
    );
    expect(screen.getByRole("status").textContent).toBe("");
  },
);
it("preserves unknown languages and hostile markup as text", async () => {
  const value = '<script>alert("x")</script>\n<img src=x onerror=alert(1)>\n';
  const { container } = render(
    <CodeSnippet value={value} language="unknown-language" />,
  );
  await waitFor(() =>
    expect(container.querySelector("code")?.textContent).toBe(value),
  );
  expect(container.querySelector("script,img,[onerror],[onload]")).toBeNull();
  expect(
    container.querySelector("figure")?.getAttribute("data-highlight"),
  ).toBe("plain");
});
it("keeps large code complete when grammar work is skipped", () => {
  const value = 'print("😀")\n'.repeat(12000);
  const { container } = render(<CodeSnippet value={value} language="py" />);
  expect(container.querySelector("code")?.textContent).toBe(value);
  expect(
    container.querySelector("figure")?.getAttribute("data-highlight"),
  ).toBe("plain");
});
it("a late grammar load cannot render the previous snippet", async () => {
  const view = render(<CodeSnippet value="def old(): pass" language="py" />);
  view.rerender(
    <CodeSnippet value={"SELECT current_value;\n"} language="sql" />,
  );
  await waitFor(() =>
    expect(
      view.container.querySelector("figure")?.getAttribute("data-highlight"),
    ).toBe("grammar"),
  );
  expect(view.container.querySelector("code")?.textContent).toBe(
    "SELECT current_value;\n",
  );
});
it("keeps the Markdown code value without adding a display newline to the copied text", async () => {
  const writeText = vi.fn().mockResolvedValue(undefined);
  vi.stubGlobal("navigator", { clipboard: { writeText } });
  const source = 'print("España 😀")\n\n';
  const { container } = render(
    <RichMarkdown value={"Inline `value`.\n\n```py\n" + source + "\n```"} />,
  );
  await waitFor(() =>
    expect(container.querySelector(".code-snippet code")?.textContent).toBe(
      source,
    ),
  );
  fireEvent.click(screen.getByRole("button", { name: "Copy code" }));
  await waitFor(() =>
    expect(writeText).toHaveBeenCalledExactlyOnceWith(source),
  );
  expect(container.querySelectorAll(".code-snippet")).toHaveLength(1);
});
it("reports clipboard failure while keeping all source selectable", async () => {
  vi.stubGlobal("navigator", {
    clipboard: { writeText: vi.fn().mockRejectedValue(new Error("Denied")) },
  });
  const { container } = render(<CodeSnippet value={"Complete source\n"} />);
  fireEvent.click(screen.getByRole("button", { name: "Copy code" }));
  await waitFor(() =>
    expect(screen.getByRole("status").textContent).toContain(
      "Copy unavailable",
    ),
  );
  expect(screen.getByRole("status").getAttribute("data-copy-state")).toBe(
    "unavailable",
  );
  expect(container.querySelector("code")?.textContent).toBe(
    "Complete source\n",
  );
});
