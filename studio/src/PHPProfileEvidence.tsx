/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { Gauge } from "lucide-react";
import { asRecord, fieldLabel } from "./ExecutionFormat";
import "./PHPProfileEvidence.css";

type RecordValue = Record<string, unknown>;
type Metrics = {
  wall_ns: number;
  cpu_user_us: number;
  cpu_system_us: number;
  peak_memory_bytes: number;
};
type Cost = {
  file: string;
  function: string;
  self_time: number;
  self_memory: number;
  calls: number;
};
type Profile = {
  complete: boolean;
  incomplete_reason: string | null;
  workload_exit_code: number | null;
  metrics: Metrics | null;
  callgraph: { rows: Cost[]; rows_truncated: boolean } | null;
};

export function isPHPProfile(value: RecordValue) {
  return (
    (value.recipe === "forge-php-runtime-v1" ||
      value.recipe === "forge-php-xdebug-v1") &&
    value.observation_only === true &&
    value.best_effort === true
  );
}

const boundedInteger = (value: unknown) =>
  typeof value === "number" && Number.isSafeInteger(value) && value >= 0;

function readReport(record: RecordValue): Profile | undefined {
  if (
    record.output_truncated !== false ||
    record.report_marker_observed !== true ||
    typeof record.output !== "string" ||
    record.output.length > 16000
  )
    return;
  const parts = record.output.split("\nFORGE_PHP_PROFILE_V1\n");
  if (parts.length !== 2) return;
  try {
    const report = asRecord(JSON.parse(parts[1]));
    if (
      !report ||
      report.recipe !== record.recipe ||
      report.observation_only !== true ||
      report.best_effort !== true ||
      typeof report.complete !== "boolean" ||
      (report.workload_exit_code !== null &&
        (!boundedInteger(report.workload_exit_code) ||
          (report.workload_exit_code as number) > 255)) ||
      (report.incomplete_reason !== null &&
        (typeof report.incomplete_reason !== "string" ||
          report.incomplete_reason.length > 80)) ||
      (report.complete &&
        (report.workload_exit_code === null ||
          report.incomplete_reason !== null))
    )
      return;
    if (report.metrics !== null) {
      const metrics = asRecord(report.metrics);
      if (
        !metrics ||
        !["wall_ns", "cpu_user_us", "cpu_system_us", "peak_memory_bytes"].every(
          (key) => boundedInteger(metrics[key]),
        )
      )
        return;
    } else if (report.complete) return;
    if (report.complete && record.exit_code !== report.workload_exit_code)
      return;
    if (report.callgraph !== null) {
      const graph = asRecord(report.callgraph);
      if (
        record.recipe !== "forge-php-xdebug-v1" ||
        !graph ||
        !Array.isArray(graph.events) ||
        graph.events.length !== 2 ||
        graph.events[0] !== "Time_(10ns)" ||
        graph.events[1] !== "Memory_(bytes)" ||
        typeof graph.rows_truncated !== "boolean" ||
        !Array.isArray(graph.rows) ||
        graph.rows.length > 100 ||
        !graph.rows.every((value) => {
          const row = asRecord(value);
          return (
            row &&
            typeof row.file === "string" &&
            row.file.length <= 1024 &&
            typeof row.function === "string" &&
            row.function.length <= 1024 &&
            boundedInteger(row.self_time) &&
            boundedInteger(row.calls) &&
            Number.isSafeInteger(row.self_memory)
          );
        })
      )
        return;
    } else if (record.recipe === "forge-php-xdebug-v1" && report.complete)
      return;
    return report as Profile;
  } catch {
    return;
  }
}

const number = (value: number) =>
  value.toLocaleString("en-US", { maximumFractionDigits: 3 });

export function PHPProfileEvidence({ record }: { record: RecordValue }) {
  const report = readReport(record);
  const metrics = report?.metrics;
  const status = !report
    ? "Report unavailable"
    : report.complete
      ? "Report complete"
      : "Report incomplete";
  return (
    <section className="php-profile" aria-label="PHP profiling observation">
      <div className="php-profile-heading">
        <h4>
          <Gauge size={18} aria-hidden="true" />
          {record.recipe === "forge-php-xdebug-v1"
            ? "PHP Xdebug profile"
            : "PHP runtime profile"}
        </h4>
        <span className="record-outcome">{status}</span>
      </div>
      <p className="subtle">
        Project code can change these diagnostics. Review them alongside
        workload output and checks.
      </p>
      <dl className="record-fields">
        <div>
          <dt>Source file</dt>
          <dd>
            {typeof record.script === "string"
              ? record.script.slice(0, 300)
              : "Not recorded"}
          </dd>
        </div>
        <div>
          <dt>Workload exit</dt>
          <dd>{report?.workload_exit_code ?? "Unknown"}</dd>
        </div>
        {report?.incomplete_reason && (
          <div>
            <dt>Missing evidence</dt>
            <dd>{fieldLabel(report.incomplete_reason)}</dd>
          </div>
        )}
      </dl>
      {metrics ? (
        <dl className="php-profile-metrics">
          <div>
            <dt>Wall time</dt>
            <dd>{number(metrics.wall_ns / 1000000)} ms</dd>
          </div>
          <div>
            <dt>CPU user</dt>
            <dd>{number(metrics.cpu_user_us / 1000)} ms</dd>
          </div>
          <div>
            <dt>CPU system</dt>
            <dd>{number(metrics.cpu_system_us / 1000)} ms</dd>
          </div>
          <div>
            <dt>Peak PHP memory</dt>
            <dd>{number(metrics.peak_memory_bytes / 1048576)} MiB</dd>
          </div>
        </dl>
      ) : (
        <p className="subtle">
          No complete metrics are available in the retained report. Inspect the
          original output and execution receipt.
        </p>
      )}
      {report?.callgraph && (
        <>
          <h5 className="php-profile-cost-heading">Exclusive function costs</h5>
          <p className="php-profile-scroll-hint subtle">
            Scroll horizontally to see every column.
          </p>
          <div
            className="php-profile-table"
            role="region"
            aria-label="Function costs"
            tabIndex={0}
          >
            <table>
              <caption className="sr-only">Exclusive function costs</caption>
              <thead>
                <tr>
                  <th scope="col">Function</th>
                  <th scope="col">Self time</th>
                  <th scope="col">Memory change</th>
                  <th scope="col">Calls</th>
                </tr>
              </thead>
              <tbody>
                {report.callgraph.rows.map((row, index) => (
                  <tr key={index}>
                    <th scope="row">
                      {row.function}
                      <span>{row.file}</span>
                    </th>
                    <td>{number(row.self_time / 100000)} ms</td>
                    <td>{number(row.self_memory)} B</td>
                    <td>{number(row.calls)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {report.callgraph.rows_truncated && (
            <p className="subtle">
              The recipe limited this function list. Review its configured row
              limit.
            </p>
          )}
        </>
      )}
    </section>
  );
}
