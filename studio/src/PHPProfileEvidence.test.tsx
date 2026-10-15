/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { describe, expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { EvidenceView } from "./ExecutionPresentation";

function observation(report: Record<string, unknown>, outputTruncated = false) {
  return {
    recipe: "forge-php-runtime-v1",
    observation_only: true,
    best_effort: true,
    script: "bin/larafly-workload.php",
    source_sha256: "a".repeat(64),
    image: "example@sha256:" + "b".repeat(64),
    argv: ["php", "-r", "TRUSTED_LAUNCHER"],
    exit_code: 7,
    output:
      "Workload output\nFORGE_PHP_PROFILE_V1\n" + JSON.stringify(report) + "\n",
    output_truncated: outputTruncated,
    report_marker_observed: true,
    receipt: null,
  };
}
const report = {
  recipe: "forge-php-runtime-v1",
  observation_only: true,
  best_effort: true,
  complete: true,
  incomplete_reason: null,
  workload_exit_code: 7,
  metrics: {
    wall_ns: 1500000,
    cpu_user_us: 750,
    cpu_system_us: 250,
    peak_memory_bytes: 2097152,
    process_wall_ns: 9000000,
  },
  callgraph: null,
};

describe("PHP profile observations", () => {
  it("separates diagnostic completion from workload exit and hides the launcher", () => {
    const value = observation(report);
    const html = renderToStaticMarkup(<EvidenceView value={value} />);
    const visible = html.split("<details")[0];
    expect(visible).toContain("PHP runtime profile");
    expect(visible).toContain("Report complete");
    expect(visible).toContain("Workload exit");
    expect(visible).toContain("1.5 ms");
    expect(visible).toContain("0.75 ms");
    expect(visible).toContain("2 MiB");
    expect(visible).not.toContain("Passed");
    expect(visible).not.toContain("TRUSTED_LAUNCHER");
    expect(html).toContain("TRUSTED_LAUNCHER");
    expect(value.output).toContain("FORGE_PHP_PROFILE_V1");
  });
  it("retains a known workload failure when its report is incomplete", () => {
    const html = renderToStaticMarkup(
      <EvidenceView
        value={observation({
          ...report,
          complete: false,
          metrics: null,
          incomplete_reason: "collector_unavailable",
        })}
      />,
    );
    expect(html.split("<details")[0]).toContain("Report incomplete");
    expect(html.split("<details")[0]).toContain("Collector unavailable");
    expect(html.split("<details")[0]).toContain(">7<");
    expect(html.split("<details")[0]).not.toContain("1.5 ms");
  });
  it.each([
    ["truncated output", observation(report, true)],
    [
      "negative wall time",
      observation({ ...report, metrics: { ...report.metrics, wall_ns: -1 } }),
    ],
    [
      "mismatched recipe",
      observation({ ...report, recipe: "forge-php-xdebug-v1" }),
    ],
    ["fake completion", observation({ ...report, complete: "true" })],
    [
      "multiple trailing reports",
      {
        ...observation(report),
        output: observation(report).output + "FORGE_PHP_PROFILE_V1\n{}\n",
      },
    ],
  ])("does not display measured values from %s", (_, value) => {
    const html = renderToStaticMarkup(<EvidenceView value={value} />);
    expect(html.split("<details")[0]).toContain("Report unavailable");
    expect(html.split("<details")[0]).not.toContain("1.5 ms");
    expect(html).toContain("Advanced record");
  });
  it("shows bounded Xdebug exclusive costs with their declared units and escaped names", () => {
    const value = observation({
      ...report,
      recipe: "forge-php-xdebug-v1",
      callgraph: {
        events: ["Time_(10ns)", "Memory_(bytes)"],
        rows_truncated: false,
        rows: [
          {
            file: "src/Example.php",
            function: "<script>unsafe()</script>",
            self_time: 100000,
            self_memory: -32,
            calls: 2,
          },
        ],
      },
    });
    value.recipe = "forge-php-xdebug-v1";
    const html = renderToStaticMarkup(<EvidenceView value={value} />);
    const visible = html.split("<details")[0];
    expect(visible).toContain("Exclusive function costs");
    expect(visible).toContain("1 ms");
    expect(visible).toContain("-32 B");
    expect(visible).toContain("&lt;script&gt;");
    expect(visible).not.toContain("<script>");
  });
});
