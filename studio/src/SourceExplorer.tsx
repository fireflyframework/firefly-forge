/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import {
  Fragment,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type CSSProperties,
} from "react";
import {
  Check,
  ChevronLeft,
  ChevronRight,
  FileCode2,
  Files,
  Folder,
  GitBranch,
  RefreshCw,
} from "lucide-react";
import { request, type Connection } from "./api";
import { diffBytes } from "./GitDiffBrowser";
import { tokenizeCode } from "./ExecutionFormat";
import { RichMarkdown } from "./RichMarkdown";
import {
  sourceRevisions,
  sourceCatalog,
  sourceFile,
  verifySourceContent,
  alignedSourceText,
  sourceProblem,
  type SourceRevisions,
  type SourceRevision,
  type SourceReference,
  type SourceCatalog,
  type SourceEntry,
  type SourceFile,
} from "./SourceExplorerData";
import "./SourceExplorer.css";

type Props = { connection: Connection; runId: string; requestId?: string };
type Problem = ReturnType<typeof sourceProblem>;
const number = (value: number) => value.toLocaleString("en-US");
function Notice({
  title,
  message,
  alert = false,
}: {
  title: string;
  message: string;
  alert?: boolean;
}) {
  return (
    <div className="source-notice" role={alert ? "alert" : "status"}>
      <h4>{title}</h4>
      <RichMarkdown value={message} />
    </div>
  );
}
export function SourceExplorer(props: Props) {
  if (
    !/^[a-f0-9]{32}$/.test(props.runId) ||
    (props.requestId !== undefined && !/^[a-f0-9]{32}$/.test(props.requestId))
  )
    return (
      <Notice
        title="Select a recorded run"
        message="A valid run reference is required to read archived source."
        alert
      />
    );
  return (
    <SourceSession
      key={JSON.stringify([
        props.runId,
        props.requestId,
        props.connection.base,
        props.connection.token,
        props.connection.authenticated,
      ])}
      {...props}
    />
  );
}
function SourceSession({ connection, runId, requestId }: Props) {
  const { base, token, authenticated } = connection;
  const scoped = useMemo(
    () => ({ base, token, authenticated }),
    [base, token, authenticated],
  );
  const [records, setRecords] = useState<SourceRevisions | null>(null),
    [problem, setProblem] = useState<Problem | null>(null);
  const [after, setAfter] = useState<string | null>(null),
    [reload, setReload] = useState(0);
  const [branch, setBranch] = useState<string | null>(null),
    [selectedId, setSelectedId] = useState("");
  const path = `/runs/${encodeURIComponent(runId)}/source/revisions`;
  const hold = useCallback((error: unknown) => {
    setRecords(null);
    setProblem(sourceProblem(error));
  }, []);
  useEffect(() => {
    const controller = new AbortController();
    void request<unknown>(scoped, path + (after ? `?after=${after}` : ""), {
      signal: controller.signal,
    })
      .then((raw) => {
        const value = sourceRevisions(raw, runId, after);
        if (controller.signal.aborted) return;
        const first =
          value.items.find((item) => item.status === "published") ??
          value.items[0];
        setRecords(value);
        setProblem(null);
        setBranch(first?.reference.branch ?? null);
        setSelectedId(first?.reference.snapshot_id ?? "");
      })
      .catch((error: unknown) => {
        if (!controller.signal.aborted) hold(error);
      });
    return () => controller.abort();
  }, [scoped, path, after, reload, runId, hold]);
  function refresh(cursor = after) {
    setRecords(null);
    setProblem(null);
    setSelectedId("");
    setAfter(cursor);
    setReload((value) => value + 1);
  }
  const branches = records
    ? [...new Set(records.items.map((item) => item.reference.branch))]
    : [];
  const options =
    records?.items.filter((item) => item.reference.branch === branch) ?? [];
  const selected = records?.items.find(
    (item) => item.reference.snapshot_id === selectedId,
  );
  function chooseBranch(value: string) {
    const next =
      branches.find((candidate) => (candidate ?? "") === value) ?? null;
    setBranch(next);
    const items =
      records?.items.filter((item) => item.reference.branch === next) ?? [];
    setSelectedId(
      (items.find((item) => item.status === "published") ?? items[0])?.reference
        .snapshot_id ?? "",
    );
  }
  return (
    <section className="source-explorer" aria-label="Recorded source snapshot">
      <header className="source-heading">
        <div>
          <h3>
            <Files size={20} aria-hidden="true" />
            Recorded source snapshot
          </h3>
          <p>
            Read archived files and their provenance. This view is read only.
          </p>
        </div>
        <button className="secondary" onClick={() => refresh()}>
          <RefreshCw size={16} aria-hidden="true" />
          Refresh records
        </button>
      </header>
      {problem ? (
        <Notice title={problem.title} message={problem.message} alert />
      ) : !records ? (
        <p role="status">Loading recorded snapshots…</p>
      ) : !records.items.length ? (
        <Notice
          title="No recorded source"
          message={
            records.recording_enabled
              ? "No source publication is recorded for this run. Older snapshots remain unavailable until their bytes have been archived."
              : "No source publication is recorded for this run. Recording is not enabled for this project and storage configuration."
          }
        />
      ) : (
        <>
          <div className="source-selectors">
            <label>
              <span>
                <GitBranch size={16} aria-hidden="true" />
                Recorded branch
              </span>
              <select
                value={branch ?? ""}
                onChange={(event) => chooseBranch(event.target.value)}
              >
                {branches.map((value) => (
                  <option key={value ?? "unrecorded"} value={value ?? ""}>
                    {value || "Branch not recorded"}
                  </option>
                ))}
              </select>
            </label>
            <label>
              <span>Snapshot record</span>
              <select
                value={selectedId}
                onChange={(event) => setSelectedId(event.target.value)}
              >
                {options.map((item) => (
                  <option
                    value={item.reference.snapshot_id}
                    key={item.reference.snapshot_id}
                  >
                    {item.reference.head.slice(0, 12)} · {item.status} ·{" "}
                    {item.reference.snapshot_id.slice(0, 6)}
                  </option>
                ))}
              </select>
            </label>
          </div>
          {selected && (
            <Snapshot
              key={
                selected.reference.snapshot_id +
                selected.reference.catalog.manifest_sha256
              }
              selected={selected}
              connection={scoped}
              requestId={requestId}
              onProblem={hold}
            />
          )}
        </>
      )}
      {records && (after !== null || records.next_after !== null) && (
        <nav className="source-pagination" aria-label="Recorded snapshot pages">
          <button
            className="secondary"
            disabled={after === null}
            onClick={() => refresh(null)}
          >
            First records
          </button>
          <span>{number(records.items.length)} records on this page</span>
          <button
            className="secondary"
            disabled={records.next_after === null}
            onClick={() => refresh(records.next_after)}
          >
            Next records
            <ChevronRight size={16} aria-hidden="true" />
          </button>
        </nav>
      )}
    </section>
  );
}
function Snapshot({
  selected,
  connection,
  requestId,
  onProblem,
}: {
  selected: SourceRevision;
  connection: Connection;
  requestId?: string;
  onProblem: (error: unknown) => void;
}) {
  const reference = selected.reference;
  return (
    <>
      <details className="source-provenance">
        <summary>View snapshot provenance</summary>
        <RichMarkdown value="Branch and commit metadata describe the recorded baseline. Snapshot and catalog digests identify the archived files displayed here." />
        <dl>
          {[
            ["Recorded branch", reference.branch ?? "Not recorded"],
            ["Baseline commit (context)", reference.head],
            ["Recorded tree (context)", reference.tree],
            ["Source catalog digest", reference.catalog.source_digest],
            ["Catalog manifest", reference.catalog.manifest_sha256],
            ["Snapshot ID", reference.snapshot_id],
          ].map(([label, value]) => (
            <div key={label}>
              <dt>{label}</dt>
              <dd>{value}</dd>
            </div>
          ))}
        </dl>
      </details>
      {selected.status !== "published" ? (
        <Notice
          title={
            selected.status === "failed"
              ? "Publication needs review"
              : "Publication is incomplete"
          }
          message="No complete source publication is recorded for this snapshot. Its files stay unavailable until the retained publication is reviewed."
        />
      ) : (
        <CatalogBrowser
          connection={connection}
          reference={reference}
          requestId={requestId}
          onProblem={onProblem}
        />
      )}
    </>
  );
}
function CatalogBrowser({
  connection,
  reference,
  requestId,
  onProblem,
}: {
  connection: Connection;
  reference: SourceReference;
  requestId?: string;
  onProblem: (error: unknown) => void;
}) {
  const [page, setPage] = useState(0);
  return (
    <CatalogPage
      key={page}
      connection={connection}
      reference={reference}
      requestId={requestId}
      page={page}
      setPage={setPage}
      onProblem={onProblem}
    />
  );
}
function CatalogPage({
  connection,
  reference,
  requestId,
  page,
  setPage,
  onProblem,
}: {
  connection: Connection;
  reference: SourceReference;
  requestId?: string;
  page: number;
  setPage: (page: number) => void;
  onProblem: (error: unknown) => void;
}) {
  const contentTarget = useRef<HTMLDivElement>(null);
  const [catalog, setCatalog] = useState<SourceCatalog | null>(null),
    [selected, setSelected] = useState<number | null>(null),
    [showFiles, setShowFiles] = useState(false);
  const path = `/runs/${reference.run_id}/source/revisions/${reference.snapshot_id}`;
  const manifest = `expected_manifest=${reference.catalog.manifest_sha256}${requestId ? `&request_id=${requestId}` : ""}`;
  useEffect(() => {
    const controller = new AbortController();
    void request<unknown>(
      connection,
      `${path}/catalog?${manifest}&page=${page}`,
      { signal: controller.signal },
    )
      .then((raw) => {
        const value = sourceCatalog(raw, reference, page);
        if (controller.signal.aborted) return;
        setCatalog(value);
        const file = value.catalog.entries.findIndex(
          (item) => item.kind === "file",
        );
        setSelected(file >= 0 ? file : value.catalog.entries.length ? 0 : null);
      })
      .catch((error: unknown) => {
        if (!controller.signal.aborted) onProblem(error);
      });
    return () => controller.abort();
  }, [connection, path, manifest, page, reference, onProblem]);
  const entry = selected === null ? null : catalog?.catalog.entries[selected];
  const listId = `source-files-${reference.snapshot_id}-${page}`;
  return (
    <div className="source-browser">
      <aside aria-label="Recorded file tree">
        <div className="source-index-heading">
          <h4>Files and folders</h4>
          <button
            className="secondary source-picker-toggle"
            aria-expanded={showFiles}
            aria-controls={listId}
            onClick={() => setShowFiles((value) => !value)}
          >
            {showFiles ? "Hide files" : "Browse files"}
          </button>
        </div>
        <p className="source-file-count">
          {number(reference.catalog.file_count)} recorded files
        </p>
        <div
          className={`source-tree-list ${showFiles ? "expanded" : "collapsed"}`}
          id={listId}
        >
          {!catalog ? (
            <p role="status">Loading source catalog…</p>
          ) : !catalog.catalog.entries.length ? (
            <p>No public files in this snapshot.</p>
          ) : (
            <ul className="source-tree">
              {catalog.catalog.entries.map((item, index) => (
                <li
                  key={item.path}
                  style={
                    {
                      "--source-depth": Math.min(
                        4,
                        item.path.replace(/\/$/, "").split("/").length - 1,
                      ),
                    } as CSSProperties
                  }
                >
                  <button
                    aria-current={selected === index ? "true" : undefined}
                    onClick={() => {
                      setSelected(index);
                      setShowFiles(false);
                      contentTarget.current?.focus();
                    }}
                  >
                    {item.kind === "directory" ? (
                      <Folder size={17} aria-hidden="true" />
                    ) : (
                      <FileCode2 size={17} aria-hidden="true" />
                    )}
                    <span>
                      {item.path}
                      <small>
                        {item.kind === "directory"
                          ? "Directory"
                          : diffBytes(item.byte_count)}
                      </small>
                    </span>
                  </button>
                </li>
              ))}
            </ul>
          )}
          <nav
            className="source-pagination source-file-pages"
            aria-label="Source catalog pages"
          >
            <button
              className="secondary"
              disabled={page === 0 || !catalog}
              onClick={() => setPage(page - 1)}
            >
              <ChevronLeft size={16} aria-hidden="true" />
              Previous files
            </button>
            <span>
              Page {number(page + 1)} of{" "}
              {number(Math.max(1, reference.catalog.page_count))}
            </span>
            <button
              className="secondary"
              disabled={!catalog || catalog.catalog.next_page === null}
              onClick={() => {
                if (catalog?.catalog.next_page !== null && catalog)
                  setPage(catalog.catalog.next_page);
              }}
            >
              Next files
              <ChevronRight size={16} aria-hidden="true" />
            </button>
          </nav>
        </div>
      </aside>
      <div
        className="source-file-content"
        ref={contentTarget}
        tabIndex={-1}
        role="region"
        aria-label="Recorded file content"
      >
        {!catalog ? (
          <p role="status">Loading source catalog…</p>
        ) : !entry ? (
          <Notice
            title="No public source files"
            message="This snapshot contains no file content to display."
          />
        ) : entry.kind === "directory" ? (
          <Notice
            title={entry.path}
            message="Choose a file from the recorded catalog to read its contents."
          />
        ) : (
          <SourceFileBrowser
            key={`${page}:${selected}`}
            connection={connection}
            reference={reference}
            entry={entry}
            page={page}
            entryIndex={selected!}
            path={path}
            manifest={manifest}
            onProblem={onProblem}
          />
        )}
      </div>
    </div>
  );
}
function SourceFileBrowser(props: {
  connection: Connection;
  reference: SourceReference;
  entry: SourceEntry;
  page: number;
  entryIndex: number;
  path: string;
  manifest: string;
  onProblem: (error: unknown) => void;
}) {
  const [offset, setOffset] = useState(0);
  return (
    <>
      <header className="source-file-heading">
        <h4>
          <FileCode2 size={18} aria-hidden="true" />
          {props.entry.path}
        </h4>
        <p>
          {diffBytes(props.entry.byte_count)} · Mode{" "}
          {(props.entry.mode & 0o777).toString(8).padStart(4, "0")}
        </p>
      </header>
      <FileWindow
        key={offset}
        {...props}
        offset={offset}
        setOffset={setOffset}
      />
    </>
  );
}
function FileWindow({
  connection,
  reference,
  entry,
  page,
  entryIndex,
  path,
  manifest,
  onProblem,
  offset,
  setOffset,
}: {
  connection: Connection;
  reference: SourceReference;
  entry: SourceEntry;
  page: number;
  entryIndex: number;
  path: string;
  manifest: string;
  onProblem: (error: unknown) => void;
  offset: number;
  setOffset: (offset: number) => void;
}) {
  const [content, setContent] = useState<{
      page: SourceFile;
      bytes: Uint8Array;
      text: string | null;
      textStart: number;
      textEnd: number;
    } | null>(null),
    [expanded, setExpanded] = useState(false);
  useEffect(() => {
    const controller = new AbortController();
    void (async () => {
      async function read(start: number, limit: number) {
        if (controller.signal.aborted)
          throw new DOMException("Read cancelled", "AbortError");
        const raw = await request<unknown>(
          connection,
          `${path}/files/${entryIndex}?${manifest}&page=${page}&offset=${start}&limit=${limit}`,
          { signal: controller.signal },
        );
        const value = sourceFile(
          raw,
          reference,
          page,
          entryIndex,
          entry,
          start,
          limit,
        );
        return { page: value, ...(await verifySourceContent(value)) };
      }
      const window = await read(offset, 32768);
      if (controller.signal.aborted) return;
      const preview = await alignedSourceText(window, read);
      if (!controller.signal.aborted)
        setContent({
          ...window,
          text: preview.text,
          textStart: preview.start,
          textEnd: preview.end,
        });
    })().catch((error: unknown) => {
      if (!controller.signal.aborted) onProblem(error);
    });
    return () => controller.abort();
  }, [
    connection,
    path,
    manifest,
    page,
    entryIndex,
    entry,
    reference,
    offset,
    onProblem,
  ]);
  if (!content) return <p role="status">Verifying source window…</p>;
  const bytes = expanded ? content.bytes : content.bytes.subarray(0, 256);
  return (
    <>
      <div className="source-window-meta">
        <p>
          {entry.byte_count
            ? `Bytes ${number(offset + 1)}–${number(offset + content.page.byte_count)} of ${number(entry.byte_count)}`
            : "Empty file"}
        </p>
        <span>
          <Check size={15} aria-hidden="true" />
          Window checksum verified
        </span>
      </div>
      {(content.textStart !== offset ||
        content.textEnd !== offset + content.page.byte_count) && (
        <p className="source-byte-note">
          Text preview aligned to UTF-8: bytes {number(content.textStart + 1)}–
          {number(content.textEnd)}. Adjacent windows can overlap at a character
          boundary.
        </p>
      )}
      {content.text === null ? (
        <>
          <p className="source-byte-note">
            Raw byte window. It may contain binary data or cut a UTF-8
            character; hexadecimal preserves the exact bytes.
          </p>
          <pre
            className="source-code source-hex"
            tabIndex={0}
            aria-label="Exact source bytes"
          >
            {Array.from(bytes, (byte) =>
              byte.toString(16).padStart(2, "0"),
            ).join(" ")}
          </pre>
          {content.bytes.length > 256 && (
            <button
              className="secondary"
              onClick={() => setExpanded((value) => !value)}
            >
              {expanded ? "Show byte preview" : "Show full byte window"}
            </button>
          )}
        </>
      ) : (
        <pre
          className="source-code"
          tabIndex={0}
          aria-label="Recorded source code"
        >
          <code>
            {content.text.split("\n").map((line, index, lines) => (
              <Fragment key={index}>
                {tokenizeCode(line, entry.path).map((token, i) => (
                  <span key={i} className={`code-token-${token.kind}`}>
                    {token.text}
                  </span>
                ))}
                {index < lines.length - 1 ? "\n" : ""}
              </Fragment>
            ))}
          </code>
        </pre>
      )}
      <nav className="source-pagination" aria-label="Source byte windows">
        <button
          className="secondary"
          disabled={offset === 0}
          onClick={() => setOffset(Math.max(0, offset - 32768))}
        >
          <ChevronLeft size={16} aria-hidden="true" />
          Previous window
        </button>
        <button
          className="secondary"
          disabled={content.page.next_offset === null}
          onClick={() => {
            if (content.page.next_offset !== null)
              setOffset(content.page.next_offset);
          }}
        >
          Next window
          <ChevronRight size={16} aria-hidden="true" />
        </button>
      </nav>
    </>
  );
}
