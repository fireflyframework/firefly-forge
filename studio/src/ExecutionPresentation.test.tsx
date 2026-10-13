/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { describe, expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import {
  EvidenceView,
  OutputView,
  UnifiedDiff,
  journalDisposition,
} from "./ExecutionPresentation";
import { parseDiff, boundedText, tokenizeCode } from "./ExecutionFormat";

describe("execution evidence presentation", () => {
  it("keeps failed commands failed even if a contradictory passed flag is supplied", () => {
    const payload = Object.freeze({
      argv: ["npm", "test"],
      exit_code: 2,
      passed: true,
      stderr: "Reservation rejected",
      actor_id: "operator-7",
      duration_ms: 1530,
    });
    const html = renderToStaticMarkup(<EvidenceView value={payload} />);
    expect(html).toContain("Exit 2");
    expect(html).toContain("Failed");
    expect(html).toContain("Reservation rejected");
    expect(html).toContain("operator-7");
    expect(html).toContain("1.53 s");
    expect(payload.exit_code).toBe(2);
  });
  it("preserves unknown fields and renders malicious text without executable markup", () => {
    const html = renderToStaticMarkup(
      <EvidenceView
        value={{
          extension: { odd: [false, null, "<img src=x onerror=alert(1)>"] },
          outcome: "needs_reconciliation",
        }}
      />,
    );
    expect(html).toContain("Extension");
    expect(html).toContain("false");
    expect(html).toContain("null");
    expect(html).toContain("&lt;img");
    expect(html).not.toContain("<img");
    expect(html).toContain("needs reconciliation");
    expect(html).toContain("Advanced record");
  });
  it("bounds output by both lines and characters without changing the stored text", () => {
    const source = "x".repeat(50000) + "\n" + "line\n".repeat(3000);
    const bounded = boundedText(source, 10000, 120);
    expect(bounded.text.length).toBeLessThanOrEqual(10000);
    expect(bounded.omitted).toBeGreaterThan(0);
    const html = renderToStaticMarkup(
      <OutputView value={source} label="Command output" />,
    );
    expect(html.length).toBeLessThan(13000);
    expect(html).toContain("Show more");
    expect(html).toContain("Copy full output");
    expect(source.length).toBeGreaterThan(50000);
  });
  it("does not turn unknown verification into success or failure", () => {
    const html = renderToStaticMarkup(
      <EvidenceView
        value={{ argv: ["make", "check"], output: "Execution interrupted" }}
      />,
    );
    expect(html).toContain("Outcome not recorded");
    expect(html).not.toContain(">Passed<");
  });
  it("matches intent receipts by task and idempotency key rather than generic task completion", () => {
    const entries = [
      { task_id: "publish", kind: "intent", idempotency_key: "first" },
      { task_id: "publish", kind: "intent", idempotency_key: "second" },
      { task_id: "publish", kind: "receipt", idempotency_key: "second" },
    ];
    expect(journalDisposition(entries[0], entries)).toBe(
      "Outcome not recorded",
    );
    expect(journalDisposition(entries[1], entries)).toBe("Receipt recorded");
    expect(
      journalDisposition({ task_id: "verify", kind: "intent" }, entries),
    ).toBe("Outcome not recorded");
  });
  it("tracks sandbox intents independently and accepts their observed reconciliation record", () => {
    const intent = {
      task_id: "verify",
      kind: "sandbox_intent",
      idempotency_key: "execution:start",
    };
    expect(journalDisposition(intent, [intent])).toBe("Outcome not recorded");
    const records = [
      intent,
      {
        task_id: "verify",
        kind: "sandbox_receipt",
        idempotency_key: "execution:create",
      },
    ];
    expect(journalDisposition(intent, records)).toBe("Outcome not recorded");
    expect(
      journalDisposition(intent, [
        ...records,
        {
          task_id: "verify",
          kind: "sandbox_reconcile",
          idempotency_key: "execution:start",
        },
      ]),
    ).toBe("Reconciliation recorded");
  });
  it("presents code artifacts with safe tokens and a line gutter", () => {
    const html = renderToStaticMarkup(
      <EvidenceView
        value={{
          path: "src/check.ts",
          content: 'const value = "<img onerror=x>";\nreturn value;',
        }}
      />,
    );
    expect(html).toContain("record-code-number");
    expect(html).toContain("code-token-keyword");
    expect(html).toContain("&lt;img");
    expect(html).not.toContain("<img");
  });
  it("retains the original event in advanced details when redundant heading fields are omitted", () => {
    const html = renderToStaticMarkup(
      <EvidenceView
        value={{ message: "Fixture event", stage: "verify" }}
        omitFields={["message"]}
      />,
    );
    expect(html).not.toContain("<dt>Message</dt>");
    expect(html).toContain("Fixture event");
    expect(html).toContain("Stage");
  });
});

describe("unified code changes", () => {
  const patch =
    "diff --git a/src/check.py b/src/check.py\n--- a/src/check.py\n+++ b/src/check.py\n@@ -10,3 +10,4 @@ def check():\n unchanged\n-old\n+new\n+extra\n tail\n\\ No newline at end of file\n@@ -30 +31 @@\n-old2\n+new2\ndiff --git a/new.ts b/new.ts\nnew file mode 100644\n--- /dev/null\n+++ b/new.ts\n@@ -0,0 +1 @@\n+const active = true;";
  it("tracks old and new lines across hunks and files, excluding headers and no-newline markers", () => {
    const files = parseDiff(patch);
    expect(files.map((file) => file.path)).toEqual(["src/check.py", "new.ts"]);
    const changed = files[0].lines.filter((line) =>
      ["addition", "deletion", "context"].includes(line.kind),
    );
    expect(changed.map(({ oldLine, newLine }) => [oldLine, newLine])).toEqual([
      [10, 10],
      [11, undefined],
      [undefined, 11],
      [undefined, 12],
      [12, 13],
      [30, undefined],
      [undefined, 31],
    ]);
    expect(files[1].lines.at(-1)?.newLine).toBe(1);
    expect(files[0].additions).toBe(3);
    expect(files[0].deletions).toBe(2);
  });
  it("retains code text exactly while highlighting safe text tokens", () => {
    const code = 'const name = "<script>"; // actual code';
    const tokens = tokenizeCode(code, "example.ts");
    expect(tokens.map((token) => token.text).join("")).toBe(code);
    expect(tokens.some((token) => token.kind === "keyword")).toBe(true);
    const html = renderToStaticMarkup(
      <UnifiedDiff value={patch + "\n+<script>alert(1)</script>"} />,
    );
    expect(html).toContain("src/check.py");
    expect(html).toContain("Old line");
    expect(html).toContain("New line");
    expect(html).not.toContain("<script>");
    expect(html).toContain("Copy patch");
  });
  it("does not assign line numbers to binary metadata or malformed hunk headers", () => {
    const files = parseDiff(
      "diff --git a/icon.png b/icon.png\nBinary files a/icon.png and b/icon.png differ\n@@ malformed @@\n+metadata",
    );
    expect(
      files[0].lines.every(
        (line) => line.oldLine === undefined && line.newLine === undefined,
      ),
    ).toBe(true);
  });
});
