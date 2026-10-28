/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { describe, expect, it } from "vitest";
import launcherFixture from "../tests/php-launcher-fixture.json";
import { renderToStaticMarkup } from "react-dom/server";
import {
  EvidenceView,
  OutputView,
  UnifiedDiff,
  journalDisposition,
  TimelineEventView,
} from "./ExecutionPresentation";
import { parseDiff, boundedText, tokenizeCode } from "./ExecutionFormat";
import { receipt } from "../tests/provider-recovery-allowance-fixture";

describe("execution evidence presentation", () => {
  const sanitationIntent = () => {
    const request = {
      version: 1,
      request_id: "a".repeat(32),
      original_intent_digest: "b".repeat(64),
      candidate_digest: "c".repeat(64),
      admission_contract_digest: "d".repeat(64),
      previous_catalog_path: "/public/package/source-only-model.json",
      valid_for_seconds: 900,
      acknowledge_index_only_generated_artifacts: true,
      acknowledge_preserve_files_source_and_budgets: true,
      acknowledge_original_scm_outcome_unknown: true,
      acknowledge_no_queue_or_provider_dispatch: true,
    };
    return {
      kind: "index_sanitation_intent",
      task_id: "implement",
      idempotency_key: request.request_id,
      digest: "e".repeat(64),
      payload: {
        version: 1,
        request,
        actor: {
          user_id: "f".repeat(32),
          global_admin: true,
          authentication_method: "local_bootstrap",
        },
        original_request_id: "1".repeat(32),
        original_intent_digest: request.original_intent_digest,
        candidate: {
          digest: request.candidate_digest,
          index_plan: {
            artifacts: [
              {
                path:
                  "frontend/.cache/vite/vitest/" +
                  "0".repeat(40) +
                  "/results.json",
              },
            ],
            public_entries: Object.fromEntries(
              Array.from({ length: 70 }, (_, n) => [
                "unshown-public-path-" + n,
                { mode: "100644" },
              ]),
            ),
          },
        },
        expires_at_ms: 1800000000000,
        public_patch_sha256: "2".repeat(64),
        no_queue_or_provider_dispatch: true,
      },
    };
  };
  const sanitationReceipt = (intent: ReturnType<typeof sanitationIntent>) => ({
    ...structuredClone(intent),
    kind: "index_sanitation_receipt",
    digest: "3".repeat(64),
    payload: {
      ...structuredClone(intent.payload),
      intent_digest: intent.digest,
      effect: { artifacts: [{ path: "retained-cache" }] },
    },
  });
  it("correlates an index-only receipt to its exact original admission without claiming a resumed run", () => {
    const intent = sanitationIntent();
    const receipt = sanitationReceipt(intent);
    expect(journalDisposition(intent, [intent])).toBe(
      "Outcome not recorded; review required",
    );
    expect(journalDisposition(intent, [intent, receipt])).toBe(
      "Index-only receipt recorded; run not resumed",
    );
    expect(journalDisposition(receipt, [intent, receipt])).toBe(
      "Index-only receipt recorded; run not resumed",
    );
    expect(journalDisposition(receipt, [receipt])).toBe(
      "Outcome not recorded; review required",
    );
  });
  it.each([
    "task",
    "key",
    "digest",
    "request",
    "actor",
    "original_request",
    "original_intent",
    "candidate",
    "expiry",
    "patch",
    "queue",
    "missing",
    "extra",
    "ack",
  ])("refuses a foreign or incomplete sanitation receipt: %s", (field) => {
    const intent = sanitationIntent();
    const receipt = sanitationReceipt(intent);
    switch (field) {
      case "task":
        receipt.task_id = "verify";
        break;
      case "key":
        receipt.idempotency_key = "4".repeat(32);
        break;
      case "digest":
        receipt.payload.intent_digest = "4".repeat(64);
        break;
      case "request":
        receipt.payload.request.valid_for_seconds = 901;
        break;
      case "actor":
        receipt.payload.actor.global_admin = false;
        break;
      case "original_request":
        receipt.payload.original_request_id = "4".repeat(32);
        break;
      case "original_intent":
        receipt.payload.original_intent_digest = "4".repeat(64);
        break;
      case "candidate":
        receipt.payload.candidate.index_plan.public_entries[
          "unshown-public-path-0"
        ].mode = "100755";
        break;
      case "expiry":
        receipt.payload.expires_at_ms += 1;
        break;
      case "patch":
        receipt.payload.public_patch_sha256 = "4".repeat(64);
        break;
      case "queue":
        receipt.payload.no_queue_or_provider_dispatch = false;
        break;
      case "extra":
        Object.assign(receipt.payload.request, { unexpected: true });
        break;
      case "ack":
        receipt.payload.request.acknowledge_original_scm_outcome_unknown = false;
        break;
      case "missing":
        delete (receipt.payload as Partial<typeof receipt.payload>).candidate;
        break;
    }
    expect(journalDisposition(intent, [intent, receipt])).toBe(
      "Outcome not recorded; review required",
    );
    expect(journalDisposition(receipt, [intent, receipt])).toBe(
      "Outcome not recorded; review required",
    );
  });
  it("compares exact sanitation evidence independently of JSON key order", () => {
    const intent = sanitationIntent();
    const receipt = sanitationReceipt(intent);
    receipt.payload.actor = Object.fromEntries(
      Object.entries(receipt.payload.actor).reverse(),
    ) as typeof receipt.payload.actor;
    expect(journalDisposition(intent, [intent, receipt])).toBe(
      "Index-only receipt recorded; run not resumed",
    );
  });
  it.each([
    "index_sanitation_intent",
    "index_sanitation_receipt",
    "workspace_observed_readmission",
  ])(
    "keeps %s human-readable and the full public-file inventory inside the closed advanced record",
    (kind) => {
      const intent = sanitationIntent();
      const payload =
        kind === "workspace_observed_readmission"
          ? {
              version: 1,
              request: intent.payload.request,
              actor: intent.payload.actor,
              original_intent_digest: intent.payload.original_intent_digest,
              no_provider_dispatch: true,
              observed_effect: intent.payload.candidate,
            }
          : kind === "index_sanitation_receipt"
            ? sanitationReceipt(intent).payload
            : intent.payload;
      const html = renderToStaticMarkup(
        <EvidenceView kind={kind} value={payload} />,
      );
      const visible = html.split('<details class="record-advanced">')[0];
      expect(visible).toContain(
        kind === "workspace_observed_readmission"
          ? "Observed-effect readmission"
          : "Index-only cache sanitation",
      );
      expect(visible).toContain(
        kind === "workspace_observed_readmission"
          ? "original source-control delivery remains unknown"
          : "does not resume the run",
      );
      expect(visible).not.toContain("unshown-public-path-");
      expect(visible.length).toBeLessThan(2500);
      expect(html).toContain("unshown-public-path-0");
      expect(html).toContain("Copy includes the full recorded output.");
      expect(html).toContain("<summary>Advanced record</summary>");
      expect(html).not.toContain("<details open");
      expect(visible).not.toContain(">Passed<");
    },
  );

  it("presents the final allowance WAL as labeled evidence with raw JSON closed and no verification claim", async () => {
    const value = await receipt({
      request_id: "a".repeat(32),
      mitigation_evidence_digest: "e".repeat(64),
    });
    const html = renderToStaticMarkup(
      <EvidenceView kind="provider_recovery_allowance" value={value} />,
    );
    expect(html).toContain("<dt>");
    expect(html).toContain("Mitigation evidence");
    expect(html).toContain("Provenance");
    expect(html).toContain("administrator_attestation");
    expect(html).toContain("Native process digest");
    expect(html).toContain("<summary>Advanced record</summary>");
    expect(html).not.toContain("<details open");
    expect(html).not.toContain(">Passed<");
    expect(html).not.toContain(">Verified<");
  });
  it("projects provider command events into a bounded command result instead of a JSON heading", () => {
    const html = renderToStaticMarkup(
      <TimelineEventView
        event={{
          stage: "stdout",
          message: JSON.stringify({
            type: "item.completed",
            item: {
              type: "command_execution",
              command: "python -m pytest backend/tests",
              aggregated_output:
                "<script>untrusted</script>\n" + "x".repeat(50000),
              exit_code: 1,
            },
            private_extension: "DO-NOT-PROJECT",
          }),
        }}
      />,
    );
    expect(html).toContain("<h3>Command finished</h3>");
    expect(html).toContain("Exit 1");
    expect(html).toContain("Failed");
    expect(html).toContain("&lt;script&gt;");
    expect(html).not.toContain("<script>");
    expect(html).not.toContain("DO-NOT-PROJECT");
    expect(html.length).toBeLessThan(15000);
  });
  it("does not infer a successful command result from an in-progress provider item", () => {
    const html = renderToStaticMarkup(
      <TimelineEventView
        event={{
          stage: "stdout",
          message: JSON.stringify({
            type: "item.started",
            item: {
              type: "command_execution",
              command: "npm run build",
              status: "in_progress",
            },
          }),
        }}
      />,
    );
    expect(html).toContain("<h3>Command started</h3>");
    expect(html).toContain("Outcome not recorded");
    expect(html).not.toContain(">Passed<");
  });
  it("keeps unknown structured provider data out of headings and escapes long plain output", () => {
    const unknown = renderToStaticMarkup(
      <TimelineEventView
        event={{
          stage: "stdout",
          message: JSON.stringify({
            type: "future.event",
            undocumented: { raw: "not a summary" },
          }),
        }}
      />,
    );
    expect(unknown).toContain("<h3>Provider event</h3>");
    const plain = renderToStaticMarkup(
      <TimelineEventView
        event={{
          stage: "stderr",
          message: "<img src=x>\n" + "x".repeat(50000),
        }}
      />,
    );
    expect(plain.match(/<h3>(.*?)<\/h3>/)?.[1].replace(/<[^>]+>/g, "")).toBe(
      "Provider error output",
    );
    expect(plain).not.toContain("<img");
    expect(plain.length).toBeLessThan(15000);
  });
  it("correlates tool outcomes by task and original invocation, with unknown superseded only by an observed receipt", () => {
    const intent = {
      task_id: "implement",
      kind: "tool_intent",
      idempotency_key: "1".repeat(32),
      payload: { correlation_id: "1".repeat(32) },
    };
    const unknown = {
      ...intent,
      kind: "tool_unknown",
      payload: { ...intent.payload, status: "unknown" },
    };
    const receipt = {
      ...intent,
      kind: "tool_receipt",
      payload: { ...intent.payload, status: "succeeded" },
    };
    expect(journalDisposition(intent, [intent])).toBe("Outcome not recorded");
    expect(journalDisposition(intent, [intent, unknown])).toBe(
      "Outcome unknown; reconciliation required",
    );
    expect(
      journalDisposition(intent, [
        intent,
        unknown,
        { ...receipt, task_id: "verify" },
      ]),
    ).toBe("Outcome unknown; reconciliation required");
    expect(
      journalDisposition(intent, [
        intent,
        unknown,
        { ...receipt, idempotency_key: "2".repeat(32) },
      ]),
    ).toBe("Outcome unknown; reconciliation required");
    expect(journalDisposition(intent, [intent, unknown, receipt])).toBe(
      "Succeeded receipt recorded",
    );
    expect(journalDisposition(unknown, [intent, unknown, receipt])).toBe(
      "Superseded by a succeeded receipt",
    );
    expect(journalDisposition(intent, [intent, receipt, unknown])).toBe(
      "Outcome unknown; reconciliation required",
    );
  });
  it("distinguishes rejected tool receipts and explicit abandonment from successful delivery", () => {
    const intent = {
      task_id: "implement",
      kind: "tool_intent",
      idempotency_key: "1".repeat(32),
      payload: { correlation_id: "1".repeat(32) },
    };
    const rejected = {
      ...intent,
      kind: "tool_receipt",
      payload: {
        ...intent.payload,
        status: "rejected",
        error: { kind: "quota" },
      },
    };
    const abandoned = {
      task_id: "implement",
      kind: "tool_reconciled",
      idempotency_key: "3".repeat(32),
      payload: {
        correlation_id: "1".repeat(32),
        decision: "abandoned_without_replay",
      },
    };
    expect(journalDisposition(intent, [intent, rejected])).toBe(
      "Rejected receipt recorded",
    );
    expect(journalDisposition(intent, [intent, abandoned])).toBe(
      "Abandoned without replay",
    );
    expect(
      journalDisposition(intent, [
        intent,
        {
          ...abandoned,
          payload: { ...abandoned.payload, decision: "delivered" },
        },
      ]),
    ).toBe("Outcome not recorded");
    expect(
      journalDisposition(intent, [
        intent,
        {
          ...abandoned,
          payload: { ...abandoned.payload, correlation_id: "4".repeat(32) },
        },
      ]),
    ).toBe("Outcome not recorded");
  });
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
  it("bounds long command headings, status values, and field labels while retaining original records", () => {
    const long = "x".repeat(200000);
    const records = [
      { command: long, status: long },
      { status: long, outcome: long, disposition: long },
      { [long]: "Recorded value" },
    ];
    for (const value of records) {
      const html = renderToStaticMarkup(<EvidenceView value={value} />);
      expect(html.length).toBeLessThan(25000);
      expect(html).toContain("more characters");
      expect(html).toContain("Advanced record");
      expect(html).toContain("Copy full");
    }
    expect(records[0].command).toHaveLength(200000);
  });
});

describe("long nested command disclosure", () => {
  it("shows real argument line breaks as readable spacing while keeping the original available", () => {
    const argv = ["php", "-r", "\n$payload = 1;\nprint($payload);\t"];
    for (const value of [
      { argv },
      { command: "php -r\n$payload = 1;\nprint($payload);\t" },
    ]) {
      const html = renderToStaticMarkup(<EvidenceView value={value} />);
      const heading = html.match(
        /class="record-command-heading"[\s\S]*?<\/code>/,
      )?.[0];
      expect(heading).toContain("php -r $payload = 1; print($payload);");
      expect(heading).not.toContain("\\n");
      expect(html).toContain("<summary>Full command</summary>");
      expect(html).toContain("Copy full command");
    }
    expect(argv).toEqual(["php", "-r", "\n$payload = 1;\nprint($payload);\t"]);
  });
  it("preserves an intentional literal backslash-n argument", () => {
    const html = renderToStaticMarkup(
      <EvidenceView value={{ argv: ["printf", "\\n"] }} />,
    );
    expect(html).toContain("printf \\n");
    expect(html).not.toContain("<summary>Full command</summary>");
  });
  it("keeps the actual escaped PHP launcher outside the compact heading and retains its original", () => {
    const value = {
      execution_id: "1".repeat(32),
      effect: "start",
      request: { request: { argv: launcherFixture.argv } },
    };
    const html = renderToStaticMarkup(<EvidenceView value={value} />);
    const heading = html.match(
      /class="record-command-heading"[\s\S]*?<\/code>/,
    )?.[0];
    expect(heading).toBeDefined();
    expect(heading!.match(/<code>([\s\S]*)<\/code>/)?.[1].length).toBeLessThan(
      250,
    );
    expect(heading).not.toContain("Copy full text");
    expect(html).toContain("<summary>Full command</summary>");
    expect(html).toContain("Copy full command");
    expect(html).toContain("Advanced record");
    expect(value.request.request.argv).toEqual(launcherFixture.argv);
  });
  it("keeps a short ordinary command readable without a redundant disclosure", () => {
    const html = renderToStaticMarkup(
      <EvidenceView value={{ argv: ["npm", "test"], exit_code: 7 }} />,
    );
    expect(html).toContain("npm test");
    expect(html).toContain("Exit 7");
    expect(html).toContain("Failed");
    expect(html).not.toContain("<summary>Full command</summary>");
    const boundary = renderToStaticMarkup(
      <EvidenceView value={{ command: "x".repeat(80) }} />,
    );
    expect(boundary).toContain("x".repeat(80));
    expect(boundary).not.toContain("<summary>Full command</summary>");
    const longer = renderToStaticMarkup(
      <EvidenceView value={{ command: "x".repeat(81) }} />,
    );
    expect(longer).toContain("<summary>Full command</summary>");
    expect(longer).toContain("Copy full command");
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
  it("uses the old path for deleted files with spaces and the new path for renamed files", () => {
    const deleted = parseDiff(
      "diff --git a/old file.txt b/old file.txt\ndeleted file mode 100644\n--- a/old file.txt\n+++ /dev/null\n@@ -1 +0,0 @@\n-old",
    );
    expect(deleted[0].path).toBe("old file.txt");
    expect(deleted[0].lines.at(-1)?.oldLine).toBe(1);
    expect(deleted[0].lines.at(-1)?.newLine).toBeUndefined();
    const renamed = parseDiff(
      "diff --git a/old file.txt b/new file.txt\n--- a/old file.txt\n+++ b/new file.txt\n@@ -1 +1 @@\n-old\n+new",
    );
    expect(renamed[0].path).toBe("new file.txt");
  });
});

it("keeps a failed run explanation readable without turning it into a console block", () => {
  const html = renderToStaticMarkup(
    <TimelineEventView
      event={{
        stage: "failed",
        message:
          "The implementation worker reported failure: Migration preserves records.\nIndependent acceptance remains outstanding. <img src=x>",
      }}
    />,
  );
  expect(html).toContain("Migration preserves records");
  expect(html).toContain("Independent acceptance remains outstanding");
  expect(html).not.toContain("<pre");
  expect(html).not.toContain("<img");
  expect(html).toContain("&lt;img");
});

it("summarizes structured worker evidence in WAL strings without exposing default JSON", async () => {
  const { default: fixture } =
    await import("../tests/provider-turn-completed-fixture.json");
  const html = renderToStaticMarkup(
    <EvidenceView
      value={{ retained_report: JSON.stringify(fixture.worker_report.report) }}
      advanced={false}
    />,
  );
  const initial = html.split("<details")[0];
  expect(initial).toContain("Migration preserves existing records");
  expect(initial).toContain("Failed");
  expect(initial).not.toContain("<pre");
  expect(initial).not.toContain("&quot;feedback&quot;");
  expect(html).toContain("Advanced details");
  expect(html).not.toContain("<details open");
});

it("retains an unknown legacy structured event in closed accessible details", () => {
  const html = renderToStaticMarkup(
    <TimelineEventView
      event={{
        stage: "stdout",
        message: '{"type":"future.event","raw":"<script>untrusted</script>"}',
      }}
    />,
  );
  const initial = html.split("<details")[0];
  expect(initial).not.toContain("<pre");
  expect(html).toContain("Advanced details");
  expect(html).toContain("&lt;script&gt;");
  expect(html).not.toContain("<script>");
  expect(html).not.toContain("<details open");
});

describe("reviewed provider refusal", () => {
  const witness = () => ({
    version: 1,
    kind: "reviewed_pre_admission_continuation",
    request_id: "a".repeat(32),
    request: {
      version: 1,
      valid_for_seconds: 120,
      acknowledge_reviewed_provider_epoch: true,
      acknowledge_no_original_response_or_tool_replay: true,
      acknowledge_same_request_attempt_and_budgets: true,
      acknowledge_finite_ownership_lease: true,
      evidence: {
        version: 1,
        provider: "bridge",
        classification: "request_refused_before_admission",
        model: "bridge/default",
        http_status: 400,
        admissions: 0,
        jobs: 0,
        provider_dispatches: 0,
        tool_callbacks: 0,
      },
    },
    candidate: {
      version: 1,
      kind: "claimed_pre_admission_recovery_candidate",
      request_id: "a".repeat(32),
      admission: "READY",
      execution_authorized: false,
      review_basis: "administrator_reviewed_exact_provider_epoch",
      usage: {
        requests: 1,
        request_limit: 100,
        tool_calls: 2,
        tool_calls_limit: 300,
        commands: 11,
        command_limit: 22,
      },
    },
    failed_ticket: { checkpoint: "SEALED_CHECKPOINT_BYTES" },
  });

  it("summarizes consent and charged usage while keeping checkpoint data in closed details", () => {
    const html = renderToStaticMarkup(
      <EvidenceView kind="provider_rejection_recovery" value={witness()} />,
    );
    const initial = html.split('<details class="record-advanced">')[0];
    expect(initial).toContain("Provider refusal reviewed");
    expect(initial).toContain("Worker receipts show subsequent execution");
    expect(initial).toContain("bridge/default");
    expect(initial).toContain("120 seconds");
    expect(initial).toContain("1 of 100");
    expect(initial).toContain("2 of 300");
    expect(initial).toContain("11 of 22");
    expect(initial).not.toContain("SEALED_CHECKPOINT_BYTES");
    expect(initial).not.toContain("Outcome not recorded");
    expect(html).toContain("Advanced record");
    expect(html).toContain("SEALED_CHECKPOINT_BYTES");
    expect(html).not.toContain("<details open");
  });

  it.each([
    "unknown refusal",
    "missing acknowledgement",
    "unreviewed candidate",
  ])("does not present %s as approved", (mutation) => {
    const value = witness();
    if (mutation === "unknown refusal")
      value.request.evidence.classification = "outcome_unknown";
    else if (mutation === "missing acknowledgement")
      value.request.acknowledge_same_request_attempt_and_budgets = false;
    else value.candidate.review_basis = "inferred_from_error";
    const html = renderToStaticMarkup(
      <EvidenceView kind="provider_rejection_recovery" value={value} />,
    );
    const initial = html.split('<details class="record-advanced">')[0];
    expect(initial).toContain("Provider refusal review is incomplete");
    expect(initial).not.toContain("Provider refusal reviewed");
    expect(initial).not.toContain("SEALED_CHECKPOINT_BYTES");
    expect(html).toContain("Advanced record");
  });
});
