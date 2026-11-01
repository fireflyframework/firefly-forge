/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  Check,
  ChevronDown,
  ChevronLeft,
  ChevronRight,
  FileCode2,
  Files,
  Folder,
  GitBranch,
  RefreshCw,
} from "lucide-react";
import { request, type Connection } from "./api";
import { CodeSnippet } from "./CodeSnippet";
import { CopyButton } from "./ExecutionPresentation";
import {
  sourceTree,
  sourceBasename,
  sourceSize,
  sourceLanguage,
  type SourceTreeNode,
} from "./SourceTree";
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

type Props = {
  connection: Connection;
  runId: string;
  requestId?: string;
  onContentReady?: () => void;
};
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
function SourceSession({
  connection,
  runId,
  requestId,
  onContentReady,
}: Props) {
  const { base, token, authenticated } = connection;
  const scoped = useMemo(
    () => ({ base, token, authenticated }),
    [base, token, authenticated],
  );
  const [records, setRecords] = useState<SourceRevisions | null>(null),
    [problem, setProblem] = useState<Problem | null>(null);
  const [showControls, setShowControls] = useState(false);
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
        <span className="source-readonly">
          <Files size={17} aria-hidden="true" />
          {records
            ? `${number(selected?.reference.catalog.file_count ?? 0)} files`
            : "Code"}
          <span>Read only</span>
        </span>
        <button
          className="secondary source-selection-toggle"
          aria-label="Change branch or snapshot"
          aria-expanded={showControls}
          aria-controls="source-snapshot-controls"
          onClick={() => setShowControls((value) => !value)}
        >
          <GitBranch size={16} aria-hidden="true" />
          <span title={branch || "Snapshot"}>{branch || "Snapshot"}</span>
          {selected && (
            <small
              className="source-summary-commit"
              aria-label="Baseline commit"
            >
              {selected.reference.head.slice(0, 8)}
            </small>
          )}
          <ChevronDown size={15} aria-hidden="true" />
        </button>
        <button
          className="secondary source-refresh"
          aria-label="Refresh records"
          title="Refresh records"
          onClick={() => refresh()}
        >
          <RefreshCw size={16} aria-hidden="true" />
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
          <div
            className={`source-selectors ${showControls ? "expanded" : "collapsed"}`}
            id="source-snapshot-controls"
          >
            <label>
              <span>
                <GitBranch size={16} aria-hidden="true" />
                Branch
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
              <span>Snapshot</span>
              <select
                value={selectedId}
                onChange={(event) => setSelectedId(event.target.value)}
              >
                {options.map((item) => (
                  <option
                    value={item.reference.snapshot_id}
                    key={item.reference.snapshot_id}
                  >
                    Baseline {item.reference.head.slice(0, 12)} · Attempt{" "}
                    {item.reference.attempt}
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
              onContentReady={onContentReady}
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
function SnapshotProvenance({ reference }: { reference: SourceReference }) {
  return (
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
  );
}
function Snapshot({
  selected,
  connection,
  requestId,
  onProblem,
  onContentReady,
}: {
  selected: SourceRevision;
  connection: Connection;
  requestId?: string;
  onProblem: (error: unknown) => void;
  onContentReady?: () => void;
}) {
  const reference = selected.reference;
  return (
    <>
      {selected.status !== "published" ? (
        <>
          <Notice
            title={
              selected.status === "failed"
                ? "Publication needs review"
                : "Publication is incomplete"
            }
            message="No complete source publication is recorded for this snapshot. Its files stay unavailable until the retained publication is reviewed."
          />
          <details className="source-technical">
            <summary>Snapshot details</summary>
            <SnapshotProvenance reference={reference} />
          </details>
        </>
      ) : (
        <CatalogBrowser
          connection={connection}
          reference={reference}
          requestId={requestId}
          onProblem={onProblem}
          onContentReady={onContentReady}
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
  onContentReady,
}: {
  connection: Connection;
  reference: SourceReference;
  requestId?: string;
  onProblem: (error: unknown) => void;
  onContentReady?: () => void;
}) {
  const [page, setPage] = useState(0);
  const pageFocus = useRef(false);
  const takePageFocus = useCallback(() => {
    const requested = pageFocus.current;
    pageFocus.current = false;
    return requested;
  }, []);
  return (
    <CatalogPage
      key={page}
      connection={connection}
      reference={reference}
      requestId={requestId}
      page={page}
      setPage={(next) => {
        pageFocus.current = true;
        setPage(next);
      }}
      takePageFocus={takePageFocus}
      onProblem={onProblem}
      onContentReady={onContentReady}
    />
  );
}
function CatalogPage({
  connection,
  reference,
  requestId,
  page,
  setPage,
  takePageFocus,
  onProblem,
  onContentReady,
}: {
  connection: Connection;
  reference: SourceReference;
  requestId?: string;
  page: number;
  setPage: (page: number) => void;
  takePageFocus: () => boolean;
  onProblem: (error: unknown) => void;
  onContentReady?: () => void;
}) {
  const contentTarget = useRef<HTMLDivElement>(null);
  const moveFocus = useRef(false);
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
  const nodes = useMemo(
    () => sourceTree(catalog?.catalog.entries ?? []),
    [catalog],
  );
  useEffect(() => {
    if (moveFocus.current) {
      moveFocus.current = false;
      contentTarget.current?.focus();
    }
  }, [selected]);
  useEffect(() => {
    if (catalog && takePageFocus()) contentTarget.current?.focus();
  }, [catalog, takePageFocus]);
  function chooseFile(index: number) {
    setShowFiles(false);
    if (selected === index) contentTarget.current?.focus();
    else {
      moveFocus.current = true;
      setSelected(index);
    }
  }
  const listId = `source-files-${reference.snapshot_id}-${page}`;
  return (
    <div className="source-browser">
      <aside aria-label="Recorded file tree">
        <div className="source-index-heading">
          <h4>Files</h4>
          <button
            className="secondary source-picker-toggle"
            aria-expanded={showFiles}
            aria-controls={listId}
            onClick={() => setShowFiles((value) => !value)}
          >
            {showFiles ? "Hide files" : "Browse files"}
          </button>
        </div>
        <div
          className={`source-tree-list ${showFiles ? "expanded" : "collapsed"}`}
          id={listId}
        >
          {!catalog ? (
            <p role="status">Loading source catalog…</p>
          ) : !catalog.catalog.entries.length ? (
            <p>No public files in this snapshot.</p>
          ) : (
            <SourceTree
              nodes={nodes}
              selected={selected}
              onSelect={chooseFile}
            />
          )}
          {reference.catalog.page_count > 1 && (
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
          )}
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
            onContentReady={onContentReady}
          />
        )}
      </div>
    </div>
  );
}
function SourceTree({
  nodes,
  selected,
  onSelect,
}: {
  nodes: SourceTreeNode[];
  selected: number | null;
  onSelect: (index: number) => void;
}) {
  const [collapsed, setCollapsed] = useState<Set<string>>(() => new Set());
  function render(nodes: SourceTreeNode[]) {
    return (
      <ul className="source-tree">
        {nodes.map((node) => (
          <li key={node.path}>
            {node.entryIndex === null ? (
              <>
                <button
                  aria-label={node.path}
                  title={node.path}
                  aria-expanded={!collapsed.has(node.path)}
                  onClick={() =>
                    setCollapsed((before) => {
                      const next = new Set(before);
                      if (next.has(node.path)) next.delete(node.path);
                      else next.add(node.path);
                      return next;
                    })
                  }
                >
                  <ChevronRight
                    className="source-folder-chevron"
                    size={14}
                    aria-hidden="true"
                  />
                  <Folder size={16} aria-hidden="true" />
                  <span>{node.name}</span>
                </button>
                {!collapsed.has(node.path) && render(node.children)}
              </>
            ) : (
              <button
                aria-label={node.path}
                title={node.path}
                aria-current={selected === node.entryIndex ? "true" : undefined}
                onClick={() => onSelect(node.entryIndex!)}
              >
                <FileCode2 size={16} aria-hidden="true" />
                <span>{node.name}</span>
              </button>
            )}
          </li>
        ))}
      </ul>
    );
  }
  return render(nodes);
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
  onContentReady?: () => void;
}) {
  const [offset, setOffset] = useState(0);
  return (
    <>
      <header className="source-file-heading">
        <div>
          <h3 title={props.entry.path}>
            <FileCode2 size={17} aria-hidden="true" />
            <span>{sourceBasename(props.entry.path)}</span>
          </h3>
        </div>
        <div className="source-file-tools">
          <span>{sourceSize(props.entry.byte_count)}</span>
          <CopyButton value={props.entry.path} label="Copy path" />
        </div>
        <p className="source-breadcrumb" title={props.entry.path}>
          {props.entry.path}
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
  onContentReady,
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
  onContentReady?: () => void;
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
  useEffect(() => {
    if (content) onContentReady?.();
  }, [content, onContentReady]);
  if (!content) return <p role="status">Verifying source window…</p>;
  const bytes = expanded ? content.bytes : content.bytes.subarray(0, 256);
  return (
    <>
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
        <>
          {offset > 0 && (
            <p className="source-line-scope">
              Preview lines · numbering restarts in this window
            </p>
          )}
          <CodeSnippet
            value={content.text}
            language={sourceLanguage(entry.path)}
            label="Recorded source code"
            lineNumbers
            copyLabel={
              offset === 0 && content.page.next_offset === null
                ? "Copy file"
                : "Copy code window"
            }
          />
        </>
      )}
      <details className="source-technical">
        <summary>Technical details</summary>
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
            Text preview aligned to UTF-8: bytes {number(content.textStart + 1)}
            –{number(content.textEnd)}. Adjacent windows can overlap at a
            character boundary.
          </p>
        )}
        <dl>
          <div>
            <dt>File size</dt>
            <dd>{number(entry.byte_count)} bytes</dd>
          </div>
          <div>
            <dt>File mode</dt>
            <dd>{(entry.mode & 0o777).toString(8).padStart(4, "0")}</dd>
          </div>
          <div>
            <dt>File SHA-256</dt>
            <dd>{entry.sha256}</dd>
          </div>
          <div>
            <dt>Window SHA-256</dt>
            <dd>{content.page.sha256}</dd>
          </div>
        </dl>
        <SnapshotProvenance reference={reference} />
      </details>
      {(offset > 0 || content.page.next_offset !== null) && (
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
      )}
    </>
  );
}
