/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { useEffect, useState } from "react";
import {
  ChevronLeft,
  ChevronRight,
  FileCode2,
  Files,
  RefreshCw,
} from "lucide-react";
import { request, type Connection } from "./api";
import {
  diffReference,
  diffIndexPage,
  diffFilePage,
  verifyDiffContent,
  type DiffFile,
  type DiffIndexPage,
  type DiffFilePage,
} from "./GitDiffData";
import { UnifiedDiff } from "./ExecutionPresentation";
import "./GitDiffBrowser.css";
const number = (value: number) => value.toLocaleString("en-US");
export function diffBytes(value: number): string {
  return value >= 1048576
    ? `${(value / 1048576).toLocaleString("en-US", { maximumFractionDigits: 1 })} MiB`
    : `${number(value)} bytes`;
}
export function GitDiffBrowser({
  connection,
  runId,
  artifact,
}: {
  connection: Connection;
  runId: string;
  artifact: unknown;
}) {
  let ref;
  try {
    ref = diffReference(artifact);
  } catch {
    return (
      <p role="alert">
        The saved diff reference could not be verified. Refresh this run before
        viewing its changes.
      </p>
    );
  }
  return (
    <DiffPages
      key={JSON.stringify([
        runId,
        connection.base,
        connection.token,
        ref.artifact_id,
        ref.manifest_sha256,
      ])}
      connection={connection}
      runId={runId}
      reference={ref}
    />
  );
}
function DiffPages({
  connection,
  runId,
  reference,
}: {
  connection: Connection;
  runId: string;
  reference: ReturnType<typeof diffReference>;
}) {
  const [showFiles, setShowFiles] = useState(false);
  const [page, setPage] = useState(0),
    [index, setIndex] = useState<DiffIndexPage | null>(null),
    [selected, setSelected] = useState<DiffFile | null>(null),
    [indexBack, setIndexBack] = useState<number[]>([]),
    [reload, setReload] = useState(0);
  const [offset, setOffset] = useState(0),
    [back, setBack] = useState<number[]>([]),
    [content, setContent] = useState<{
      page: DiffFilePage;
      text: string | null;
      hex: string;
    } | null>(null);
  const [indexError, setIndexError] = useState(""),
    [fileError, setFileError] = useState("");
  const path = `/runs/${encodeURIComponent(runId)}/diff`,
    manifest = `expected_manifest=${reference.manifest_sha256}`;
  useEffect(() => {
    const controller = new AbortController();
    setIndex(null);
    setSelected(null);
    setIndexError("");
    setOffset(0);
    setBack([]);
    if (!reference.file_count) return () => controller.abort();
    void request<unknown>(
      connection,
      `${path}/index?${manifest}&page=${page}`,
      { signal: controller.signal },
    )
      .then((raw) => {
        const value = diffIndexPage(raw, reference, page);
        if (controller.signal.aborted) return;
        setIndex(value);
        setSelected(value.files[0] ?? null);
      })
      .catch(() => {
        if (!controller.signal.aborted)
          setIndexError(
            "The file index could not be read and verified. Refresh it to check current access and the saved diff.",
          );
      });
    return () => controller.abort();
  }, [connection, path, manifest, page, reload, reference]);
  useEffect(() => {
    const controller = new AbortController();
    setContent(null);
    setFileError("");
    if (!selected) return () => controller.abort();
    void (async () => {
      const raw = await request<unknown>(
        connection,
        `${path}/files/${selected.file_index}?${manifest}&offset=${offset}&limit=32768`,
        { signal: controller.signal },
      );
      const value = diffFilePage(raw, reference, selected.file_index, offset);
      if (value.total_bytes !== selected.byte_count || value.byte_count > 32768)
        throw Error("File extent changed");
      const bytes = await verifyDiffContent(value);
      let text: string | null = null;
      if (value.utf8_complete) {
        try {
          text = new TextDecoder("utf-8", { fatal: true }).decode(bytes);
        } catch {
          throw Error("Invalid text encoding");
        }
      }
      if (controller.signal.aborted) return;
      setContent({
        page: value,
        text,
        hex:
          text === null
            ? Array.from(bytes, (byte) =>
                byte.toString(16).padStart(2, "0"),
              ).join(" ")
            : "",
      });
    })().catch(() => {
      if (!controller.signal.aborted)
        setFileError(
          "This content window could not be verified. No code from this response is displayed. Refresh the file index before continuing.",
        );
    });
    return () => controller.abort();
  }, [connection, path, manifest, reference, selected, offset]);
  function choose(file: DiffFile) {
    setShowFiles(false);
    setSelected(file);
    setOffset(0);
    setBack([]);
  }
  return (
    <section className="git-diff-browser" aria-label="Saved source changes">
      <header className="git-diff-summary">
        <div>
          <h3>
            <Files size={20} aria-hidden="true" />
            {number(reference.file_count)} changed files
          </h3>
          <p>
            {diffBytes(reference.byte_count)} retained. Browse one file and one
            content window at a time.
          </p>
        </div>
        <button
          className="secondary"
          onClick={() => setReload((value) => value + 1)}
        >
          <RefreshCw size={16} aria-hidden="true" />
          Refresh index
        </button>
      </header>
      {!reference.file_count ? (
        <p>No file changes in this saved diff.</p>
      ) : (
        <div className="git-diff-layout">
          <aside aria-label="Changed file index">
            <div className="git-diff-index-heading">
              <h4>Changed files</h4>
              <button
                className="secondary git-diff-picker-toggle"
                aria-expanded={showFiles}
                aria-controls={`diff-files-${reference.artifact_id}`}
                onClick={() => setShowFiles((value) => !value)}
              >
                {showFiles ? "Hide files" : "Browse files"}
              </button>
            </div>
            {indexError ? (
              <p className="alert" role="alert">
                {indexError}
              </p>
            ) : !index || index.page !== page ? (
              <p role="status">Loading file index…</p>
            ) : (
              <div
                id={`diff-files-${reference.artifact_id}`}
                className={`git-diff-file-list ${showFiles ? "expanded" : "collapsed"}`}
              >
                <ul className="git-diff-files">
                  {index.files.map((file) => (
                    <li key={file.file_index}>
                      <button
                        className={
                          selected?.file_index === file.file_index
                            ? "selected"
                            : ""
                        }
                        aria-current={
                          selected?.file_index === file.file_index
                            ? "true"
                            : undefined
                        }
                        onClick={() => choose(file)}
                      >
                        <FileCode2 size={17} aria-hidden="true" />
                        <span>
                          {file.path}
                          <small>{diffBytes(file.byte_count)}</small>
                        </span>
                      </button>
                    </li>
                  ))}
                </ul>
                <nav
                  className="git-diff-pagination"
                  aria-label="File index pages"
                >
                  <button
                    className="secondary"
                    disabled={!indexBack.length}
                    onClick={() => {
                      setPage(indexBack.at(-1)!);
                      setIndexBack((values) => values.slice(0, -1));
                    }}
                  >
                    <ChevronLeft size={16} />
                    Previous files
                  </button>
                  <span>
                    Page {number(page + 1)} of{" "}
                    {number(reference.index_page_count)}
                  </span>
                  <button
                    className="secondary"
                    disabled={index.next_page === null}
                    onClick={() => {
                      setIndexBack((values) => [...values, page]);
                      setPage(index.next_page!);
                    }}
                  >
                    Next files
                    <ChevronRight size={16} />
                  </button>
                </nav>
              </div>
            )}
          </aside>
          <div className="git-diff-content" aria-label="Selected file changes">
            {selected ? (
              <>
                <h4 className="git-diff-path">
                  <FileCode2 size={18} />
                  {selected.path}
                </h4>
                {fileError ? (
                  <p className="alert" role="alert">
                    {fileError}
                  </p>
                ) : !content ||
                  content.page.file_index !== selected.file_index ||
                  content.page.offset !== offset ? (
                  <p role="status">Loading and verifying content…</p>
                ) : (
                  <>
                    <div className="git-diff-window">
                      <p>
                        Bytes {number(offset + 1)}–
                        {number(offset + content.page.byte_count)} of{" "}
                        {number(content.page.total_bytes)}.{" "}
                        {offset > 0 || content.page.next_offset !== null
                          ? "Partial file window."
                          : "Complete file diff."}
                        {content.page.starts_mid_line ||
                        content.page.ends_mid_line
                          ? " A line continues outside this window."
                          : ""}
                      </p>
                      <span className="git-diff-verified">
                        Page checksum verified
                      </span>
                    </div>
                    {content.text === null ? (
                      <>
                        <p className="alert">
                          This window is not complete UTF-8 text. Exact bytes
                          are shown as hexadecimal; no characters were replaced.
                        </p>
                        <pre
                          className="git-diff-hex"
                          tabIndex={0}
                          aria-label="Exact diff bytes"
                        >
                          {content.hex}
                        </pre>
                      </>
                    ) : (
                      <UnifiedDiff value={content.text} />
                    )}
                    <nav
                      className="git-diff-pagination"
                      aria-label="File content windows"
                    >
                      <button
                        className="secondary"
                        disabled={!back.length}
                        onClick={() => {
                          setOffset(back.at(-1)!);
                          setBack((values) => values.slice(0, -1));
                        }}
                      >
                        <ChevronLeft size={16} />
                        Previous window
                      </button>
                      <button
                        className="secondary"
                        disabled={content.page.next_offset === null}
                        onClick={() => {
                          setBack((values) => [...values, offset]);
                          setOffset(content.page.next_offset!);
                        }}
                      >
                        Next window
                        <ChevronRight size={16} />
                      </button>
                    </nav>
                  </>
                )}
              </>
            ) : (
              <p>Select a file to inspect its saved changes.</p>
            )}
          </div>
        </div>
      )}
    </section>
  );
}
