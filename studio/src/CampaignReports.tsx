/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { useEffect, useRef, useState } from "react";
import { FileSearch, RefreshCw } from "lucide-react";
import {
  ApiError,
  request,
  type Connection,
  type ProjectSnapshot,
} from "./api";
import type { IdentitySession } from "./Identity";
import { EvidenceView } from "./ExecutionPresentation";
import {
  campaignStates,
  readCampaignList,
  readCampaignReport,
  type CampaignComponent,
  type CampaignReport,
  type CampaignSummary,
} from "./CampaignReportData";
import "./CampaignReports.css";
import { CampaignReportHistory } from "./CampaignReportHistory";
import { CampaignDeliveryPlans } from "./CampaignDeliveryPlans";
import type { CampaignArchiveReceipt } from "./CampaignReportArchiveData";
const label = (value: string) => {
  const words = value.replaceAll("_", " ");
  return words.charAt(0).toUpperCase() + words.slice(1);
};
const PAGE = 20;
export function CampaignReports({
  connection,
  project,
  session,
}: {
  connection: Connection;
  project: ProjectSnapshot;
  session: IdentitySession;
}) {
  const [archive, setArchive] = useState<CampaignArchiveReceipt | null>(null);
  const [items, setItems] = useState<CampaignSummary[] | null>(null),
    [selected, setSelected] = useState(""),
    [report, setReport] = useState<CampaignReport | null>(null),
    [busy, setBusy] = useState(false),
    [error, setError] = useState("");
  const active = useRef<AbortController | null>(null),
    live = useRef(true),
    operation = useRef(0),
    selection = useRef(""),
    result = useRef<HTMLHeadingElement>(null),
    historicalHeading = useRef<HTMLHeadingElement>(null);
  const allowed =
    session.global_admin ||
    session.memberships.some(
      (m) => m.project_id === project.id && m.permissions.includes("read"),
    );
  useEffect(() => {
    live.current = true;
    return () => {
      live.current = false;
      active.current?.abort();
    };
  }, []);
  useEffect(() => {
    if (report) {
      result.current?.focus({ preventScroll: true });
      result.current?.scrollIntoView({ block: "nearest" });
    }
  }, [report]);
  function clear() {
    setReport(null);
  }
  function failure(cause: unknown) {
    clear();
    if (cause instanceof ApiError && [401, 403].includes(cause.status)) {
      setItems(null);
      setSelected("");
      selection.current = "";
      setError(
        "Access to campaign evidence could not be confirmed. Sign in or ask an authorized project reader.",
      );
    } else if (cause instanceof ApiError && cause.status === 404)
      setError("Campaign reading is unavailable for this product or server.");
    else
      setError(
        "A matching campaign response could not be read. No report state has been established.",
      );
  }
  function begin() {
    active.current?.abort();
    const controller = new AbortController();
    active.current = controller;
    const version = ++operation.current;
    setBusy(true);
    setError("");
    clear();
    return { controller, version };
  }
  function current(controller: AbortController, version: number) {
    return (
      live.current &&
      !controller.signal.aborted &&
      operation.current === version
    );
  }
  async function list() {
    if (!allowed) return;
    const { controller, version } = begin();
    setItems(null);
    selection.current = "";
    setSelected("");
    try {
      const items = readCampaignList(
        await request<unknown>(
          connection,
          `/projects/${project.id}/campaigns`,
          { signal: controller.signal },
        ),
        project.id,
      );
      if (current(controller, version)) setItems(items);
    } catch (cause) {
      if (current(controller, version)) failure(cause);
    } finally {
      if (current(controller, version)) setBusy(false);
    }
  }
  async function read() {
    const id = selection.current;
    if (!allowed || !id) return;
    const { controller, version } = begin();
    try {
      const data = readCampaignReport(
        await request<unknown>(
          connection,
          `/projects/${project.id}/campaigns/${id}/report`,
          { signal: controller.signal },
        ),
        { project: project.id, campaign: id },
      );
      if (current(controller, version) && selection.current === id)
        setReport(data);
    } catch (cause) {
      if (current(controller, version)) failure(cause);
    } finally {
      if (current(controller, version)) setBusy(false);
    }
  }
  if (!allowed) return null;
  return (
    <section className="campaign-reports" aria-label="Campaign reports">
      <div className="section-heading">
        <div>
          <h2>Campaign reports</h2>
          <p className="subtle">
            View component states and saved evidence for an existing campaign.
            Loading a report does not start or resume work.
          </p>
        </div>
        <button
          className="secondary"
          disabled={busy}
          onClick={() => void list()}
        >
          <RefreshCw size={16} />
          {busy ? "Loading campaign evidence…" : "Load campaigns"}
        </button>
      </div>
      {error && (
        <p className="alert" role="alert">
          {error}
        </p>
      )}
      {items === null && !error && (
        <p className="subtle">
          Choose Load campaigns to inspect existing campaign records.
        </p>
      )}
      {items?.length === 0 && (
        <p role="status">No campaigns are recorded for this product.</p>
      )}
      {items && items.length > 0 && (
        <div className="campaign-report-toolbar">
          <div className="field">
            <label htmlFor="campaign-report-selection">Campaign</label>
            <select
              id="campaign-report-selection"
              value={selected}
              onChange={(event) => {
                active.current?.abort();
                operation.current++;
                selection.current = event.target.value;
                setSelected(event.target.value);
                clear();
                setError("");
                setBusy(false);
              }}
            >
              <option value="">Choose an existing campaign</option>
              {items.map((item) => (
                <option key={item.id} value={item.id}>
                  {item.created_at
                    ? new Date(item.created_at).toLocaleString()
                    : "Date not recorded"}{" "}
                  · {label(item.status)} · ref {item.id}
                </option>
              ))}
            </select>
          </div>
          <button
            className="secondary"
            disabled={busy || !selected}
            onClick={() => void read()}
          >
            <FileSearch size={16} />
            View report
          </button>
        </div>
      )}
      {selected && (
        <details className="campaign-report-references">
          <summary>Selected campaign reference</summary>
          <code>{selected}</code>
        </details>
      )}
      {selected && (
        <CampaignDeliveryPlans
          key={"delivery:" + selected}
          connection={connection}
          session={session}
          project={project.id}
          campaign={selected}
          report={report}
          archive={archive}
          onDenied={() =>
            failure(new ApiError("Project evidence access denied", 403))
          }
        />
      )}
      {selected && (
        <CampaignReportHistory
          key={selected + (report?.digest ?? "")}
          connection={connection}
          session={session}
          project={project.id}
          campaign={selected}
          report={report}
          onArchiveSelected={setArchive}
          onDenied={() =>
            failure(new ApiError("Project evidence access denied", 403))
          }
          renderReport={(data) => (
            <ReportSnapshot
              report={data}
              heading={historicalHeading}
              title="Historical report evidence"
            />
          )}
        />
      )}
      {report && (
        <ReportSnapshot
          key={report.digest + selected}
          report={report}
          heading={result}
        />
      )}
    </section>
  );
}
function ReportSnapshot({
  report,
  heading,
  title = "Campaign report snapshot",
}: {
  title?: string;
  report: CampaignReport;
  heading: React.RefObject<HTMLHeadingElement | null>;
}) {
  const [page, setPage] = useState(0),
    [selected, setSelected] = useState("");
  const detail = report.components.find((c) => c.component_id === selected);
  return (
    <section className="campaign-report-snapshot" aria-label={title}>
      <h3 ref={heading} tabIndex={-1}>
        {title}
      </h3>
      <dl className="campaign-report-summary">
        <div>
          <dt>Server evidence state</dt>
          <dd>{label(report.state)}</dd>
        </div>
        <div>
          <dt>Recorded campaign status</dt>
          <dd>
            {report.campaign.recorded_status
              ? label(report.campaign.recorded_status)
              : "Not recorded"}
          </dd>
        </div>
        {report.counts !== null &&
          campaignStates.map((state) => (
            <div key={state}>
              <dt>{label(state)} components</dt>
              <dd>{report.counts![state] ?? 0}</dd>
            </div>
          ))}
      </dl>
      <p className="subtle">
        Based on saved checks and execution records. Evidence state and recorded
        workflow status can differ.
      </p>
      {report.reason && (
        <p>
          {report.reason === "evidence_integrity"
            ? "Retained evidence integrity could not be established for this snapshot."
            : report.reason === "evidence_limit"
              ? "The bounded evidence read limit prevented this snapshot."
              : report.reason === "snapshot_timeout"
                ? "The bounded evidence snapshot could not be read in time."
                : report.reason}
        </p>
      )}
      {report.counts === null ? (
        <p role="status">
          Component counts are unavailable; this snapshot does not establish an
          empty or verified campaign.
        </p>
      ) : (
        <>
          <h4>Components</h4>
          <ul
            className="campaign-report-components"
            aria-label="Campaign components"
          >
            {report.components
              .slice(page * PAGE, (page + 1) * PAGE)
              .map((component) => (
                <li key={component.component_id}>
                  <button
                    className="secondary"
                    aria-label={`Inspect component ${component.component_id}`}
                    aria-pressed={selected === component.component_id}
                    onClick={() => setSelected(component.component_id)}
                  >
                    <strong>{component.component_id}</strong>
                    <span>{label(component.state)}</span>
                    <span>
                      Attempt {component.attempt} · recorded{" "}
                      {label(component.recorded_status)}
                    </span>
                  </button>
                </li>
              ))}
          </ul>
          <Pagination
            page={page}
            total={report.components.length}
            name="components"
            onPage={(value) => {
              setPage(value);
              setSelected("");
            }}
          />
          {detail && (
            <ComponentEvidence key={detail.component_id} component={detail} />
          )}
        </>
      )}
      <details className="campaign-report-references">
        <summary>About this report</summary>
        <p className="subtle">
          SCM freshness: not observed. This report format does not include the
          separate reporting-plan criteria.
        </p>
        <p className="subtle">
          This snapshot summarizes retained evidence. Server evidence state is
          distinct from recorded workflow status and does not establish current
          external SCM state.
        </p>

        <EvidenceView
          advanced={false}
          title="Retained campaign references"
          value={{
            campaign_id: report.campaign.id,
            project_id: report.project_id,
            project_digest: report.campaign.project_digest ?? "Not recorded",
            request_digest: report.campaign.request_digest ?? "Not recorded",
            campaign_journal_head:
              report.campaign.journal_head ?? "Not recorded",
            event_count: report.campaign.event_count ?? "Not recorded",
          }}
        />
        <EvidenceView
          advanced={false}
          title="Report read limits"
          value={report.limits}
        />
        <div className="campaign-report-digest">
          <span>Server report digest (reference)</span>
          <code>{report.digest}</code>
          <small>
            This client displays the server reference; it does not independently
            verify the report digest.
          </small>
        </div>
      </details>
    </section>
  );
}
function Pagination({
  page,
  total,
  name,
  onPage,
}: {
  page: number;
  total: number;
  name: string;
  onPage: (value: number) => void;
}) {
  if (total <= PAGE) return null;
  return (
    <nav className="campaign-report-pagination" aria-label={`${name} pages`}>
      <button
        className="secondary"
        disabled={page === 0}
        onClick={() => onPage(page - 1)}
      >
        Previous {name}
      </button>
      <span>
        Showing {page * PAGE + 1}–{Math.min((page + 1) * PAGE, total)} of{" "}
        {total}
      </span>
      <button
        className="secondary"
        disabled={(page + 1) * PAGE >= total}
        onClick={() => onPage(page + 1)}
      >
        Next {name}
      </button>
    </nav>
  );
}
const tabs = ["Tasks", "Checks", "Source", "History", "References"] as const;
function ComponentEvidence({ component: c }: { component: CampaignComponent }) {
  const [tab, setTab] = useState<(typeof tabs)[number]>("Tasks"),
    [page, setPage] = useState(0);
  const heading = useRef<HTMLHeadingElement>(null);
  useEffect(() => {
    heading.current?.focus({ preventScroll: true });
    heading.current?.scrollIntoView({ block: "nearest" });
  }, []);
  return (
    <section
      className="campaign-report-detail"
      aria-label={`Component ${c.component_id} evidence`}
    >
      <h4 ref={heading} tabIndex={-1}>
        Component {c.component_id}
      </h4>
      <p>
        Server evidence state: {label(c.state)}. Recorded status:{" "}
        {label(c.recorded_status)}. Attempt {c.attempt}.
      </p>
      {c.reason && <p>{c.reason}</p>}
      <div
        role="tablist"
        className="campaign-report-tabs"
        aria-label={`Component ${c.component_id} evidence sections`}
      >
        {tabs.map((name, index) => (
          <button
            key={name}
            type="button"
            className="secondary"
            role="tab"
            aria-selected={tab === name}
            tabIndex={tab === name ? 0 : -1}
            id={`campaign-${c.component_id}-tab-${name}`}
            aria-controls={`campaign-${c.component_id}-evidence`}
            onClick={() => {
              setTab(name);
              setPage(0);
            }}
            onKeyDown={(event) => {
              if (
                ["ArrowRight", "ArrowLeft", "Home", "End"].includes(event.key)
              ) {
                event.preventDefault();
                const next =
                  event.key === "Home"
                    ? 0
                    : event.key === "End"
                      ? tabs.length - 1
                      : (index +
                          (event.key === "ArrowRight" ? 1 : -1) +
                          tabs.length) %
                        tabs.length;
                setTab(tabs[next]);
                setPage(0);
                const node =
                  event.currentTarget.parentElement?.querySelectorAll<HTMLButtonElement>(
                    '[role="tab"]',
                  )[next];
                node?.focus();
                node?.scrollIntoView({ block: "nearest", inline: "nearest" });
              }
            }}
          >
            {name}
          </button>
        ))}
      </div>
      <div
        role="tabpanel"
        id={`campaign-${c.component_id}-evidence`}
        aria-labelledby={`campaign-${c.component_id}-tab-${tab}`}
        tabIndex={0}
      >
        {tab === "Tasks" && (
          <>
            {c.tasks.length ? (
              c.tasks.slice(page * PAGE, (page + 1) * PAGE).map((task) => (
                <EvidenceView
                  key={task.id}
                  advanced={false}
                  title={`Task ${task.id}`}
                  value={{
                    attempt: task.attempt,
                    recorded_state: label(task.state),
                    evidence_digest: task.evidence_digest ?? "Not recorded",
                    receipt_sequence: task.receipt?.sequence ?? "Not recorded",
                    receipt_digest: task.receipt?.digest ?? "Not recorded",
                  }}
                />
              ))
            ) : (
              <p>No task evidence was recorded in this snapshot.</p>
            )}
            <Pagination
              page={page}
              total={c.tasks.length}
              name="tasks"
              onPage={setPage}
            />
          </>
        )}
        {tab === "Checks" && (
          <>
            {c.checks.length ? (
              c.checks
                .slice(page * PAGE, (page + 1) * PAGE)
                .map((check, index) => (
                  <EvidenceView
                    key={index}
                    advanced={false}
                    title={`Recorded check ${page * PAGE + index + 1}`}
                    value={{
                      exit_code: check.exit_code,
                      command_digest: check.command_digest,
                      argument_count: check.argument_count,
                      receipt_sequence: check.receipt.sequence,
                      receipt_digest: check.receipt.digest,
                    }}
                  />
                ))
            ) : (
              <p>No executable check receipt was recorded in this snapshot.</p>
            )}
            <Pagination
              page={page}
              total={c.checks.length}
              name="checks"
              onPage={setPage}
            />
          </>
        )}
        {tab === "Source" && (
          <>
            <EvidenceView
              advanced={false}
              title="Recorded source"
              value={{
                branch: c.branch ?? "Not recorded",
                source_base: c.source_base,
                source_tree: c.source_tree ?? "Not recorded",
                checkpoint_revision: c.checkpoint_revision ?? "Not recorded",
              }}
            />
            {[
              ["Branch", c.branch_receipt],
              ["Source tree", c.source_tree_receipt],
              ["Checkpoint", c.checkpoint_receipt],
            ].map(([name, ref]) => (
              <EvidenceView
                key={String(name)}
                advanced={false}
                title={`${name} journal reference`}
                value={ref ?? { recorded: "No journal reference recorded" }}
              />
            ))}
          </>
        )}
        {tab === "History" && (
          <>
            {c.prior_attempts.length ? (
              c.prior_attempts
                .slice(page * PAGE, (page + 1) * PAGE)
                .map((prior) => (
                  <EvidenceView
                    key={prior.attempt}
                    advanced={false}
                    title={`Prior attempt ${prior.attempt}`}
                    value={prior}
                  />
                ))
            ) : (
              <p>No prior component attempt is referenced.</p>
            )}
            <Pagination
              page={page}
              total={c.prior_attempts.length}
              name="attempts"
              onPage={setPage}
            />
          </>
        )}
        {tab === "References" && (
          <EvidenceView
            advanced={false}
            title="Component journal references"
            value={{
              run_id: c.run_id,
              repository_id: c.repository_id,
              admission_digest: c.admission_digest,
              run_digest: c.run_digest,
              plan_digest: c.plan_digest ?? "Not recorded",
              journal_sequence: c.journal.sequence,
              journal_digest: c.journal.digest,
              journal_head: c.journal.head,
              verification: c.verification,
              depends_on: c.depends_on.length
                ? c.depends_on.join(", ")
                : "No component dependencies recorded",
            }}
          />
        )}
      </div>
    </section>
  );
}
