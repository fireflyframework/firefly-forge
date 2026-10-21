/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { useEffect, useRef, useState } from "react";
import { Image as ImageIcon, Maximize2, X } from "lucide-react";
import { ApiError, normalizeBase, request, type Connection } from "./api";
import {
  verifiedPreviewBlob,
  workerEvidenceImages,
  type WorkerImage,
} from "./WorkerEvidenceData";
import "./WorkerEvidencePreview.css";
type Preview = { image: WorkerImage; url: string };
export function WorkerEvidencePreview({
  connection,
  runId,
  requestId,
}: {
  connection: Connection;
  runId: string;
  requestId: string;
}) {
  const [opened, setOpened] = useState(false),
    [images, setImages] = useState<WorkerImage[] | null>(null),
    [busy, setBusy] = useState(false),
    [error, setError] = useState(""),
    [full, setFull] = useState<Preview | null>(null),
    [originalSize, setOriginalSize] = useState(false);
  const generation = useRef(0),
    abort = useRef<AbortController | null>(null),
    urls = useRef<string[]>([]),
    dialog = useRef<HTMLDialogElement>(null),
    returnFocus = useRef<HTMLButtonElement | null>(null);
  const path = `/runs/${encodeURIComponent(runId)}/worker-evidence`;
  useEffect(() => {
    const cycle = ++generation.current;
    return () => {
      if (cycle === generation.current) generation.current += 1;
      abort.current?.abort();
      for (const url of urls.current) URL.revokeObjectURL(url);
      urls.current = [];
    };
  }, [connection, runId, requestId]);
  useEffect(() => {
    const node = dialog.current;
    if (!full || !node) return;
    const measure = () => {
      const value = Number.parseFloat(
        getComputedStyle(document.documentElement).zoom,
      );
      node.style.setProperty(
        "--evidence-zoom",
        String(Number.isFinite(value) && value > 0 ? value : 1),
      );
    };
    measure();
    node.showModal();
    window.addEventListener("resize", measure);
    return () => {
      window.removeEventListener("resize", measure);
      node.close();
    };
  }, [full]);
  function clear() {
    generation.current += 1;
    abort.current?.abort();
    for (const url of urls.current) URL.revokeObjectURL(url);
    urls.current = [];
    setImages(null);
    setFull(null);
    setOriginalSize(false);
    setBusy(false);
  }
  function closePreview() {
    dialog.current?.close();
    setFull(null);
    setOriginalSize(false);
    for (const url of urls.current) URL.revokeObjectURL(url);
    urls.current = [];
    returnFocus.current?.focus();
  }
  async function show() {
    if (busy || images) return;
    const cycle = generation.current,
      controller = new AbortController();
    abort.current = controller;
    setBusy(true);
    setError("");
    try {
      const value = await request<unknown>(
        connection,
        `${path}?request_id=${encodeURIComponent(requestId)}`,
        { signal: controller.signal },
      );
      if (cycle === generation.current && !controller.signal.aborted)
        setImages(workerEvidenceImages(value));
    } catch (cause) {
      if (cycle === generation.current && !controller.signal.aborted)
        setError(
          cause instanceof Error
            ? cause.message
            : "The retained images could not be read.",
        );
    } finally {
      if (cycle === generation.current && !controller.signal.aborted)
        setBusy(false);
    }
  }
  async function preview(image: WorkerImage, button: HTMLButtonElement) {
    returnFocus.current = button;
    if (busy) return;
    const cycle = generation.current,
      controller = new AbortController();
    abort.current = controller;
    setBusy(true);
    setError("");
    try {
      const response = await fetch(
        normalizeBase(connection.base) + `${path}/${image.id}`,
        {
          credentials: "include",
          signal: controller.signal,
          headers: connection.token
            ? { Authorization: `Bearer ${connection.token}` }
            : {},
        },
      );
      if ([401, 403].includes(response.status))
        throw new ApiError(
          "Your current project access no longer permits this preview.",
          response.status,
        );
      const blob = await verifiedPreviewBlob(response, image);
      if (cycle !== generation.current || controller.signal.aborted) return;
      const item = { image, url: URL.createObjectURL(blob) };
      urls.current.push(item.url);
      setFull(item);
    } catch (cause) {
      if (cycle === generation.current && !controller.signal.aborted) {
        if (cause instanceof ApiError && [401, 403].includes(cause.status))
          clear();
        setError(
          cause instanceof Error
            ? cause.message
            : "The image could not be verified.",
        );
      }
    } finally {
      if (cycle === generation.current && !controller.signal.aborted)
        setBusy(false);
    }
  }
  return (
    <details
      className="worker-evidence"
      aria-label="Retained question screenshots"
      open={opened}
      onToggle={(event) => {
        const open = event.currentTarget.open;
        setOpened(open);
        if (open) void show();
        else clear();
      }}
    >
      <summary>
        <ImageIcon size={17} aria-hidden="true" /> Retained screenshots
      </summary>
      <div className="worker-evidence-content">
        <p>
          Images saved with this question. They show the recorded workspace, not
          a live view or proof that the product passed its checks.
        </p>
        {busy && <p role="status">Verifying retained evidence…</p>}
        {error && (
          <p role="alert" className="alert">
            {error}
          </p>
        )}
        {images?.length === 0 && (
          <p>No screenshots were retained for this question.</p>
        )}
        {!!images?.length && (
          <ul className="worker-evidence-list">
            {images.map((image, index) => (
              <li key={image.id}>
                <span>
                  <strong>Screenshot {index + 1}</strong>
                  <small>
                    {image.filename} · {Math.ceil(image.bytes / 1024)} KB
                  </small>
                </span>
                <button
                  className="secondary"
                  disabled={busy}
                  onClick={(event) => void preview(image, event.currentTarget)}
                >
                  <Maximize2 size={16} /> View image
                </button>
              </li>
            ))}
          </ul>
        )}
        <small>
          Access is checked again when an image opens. Review images before
          sharing: byte and metadata checks do not remove secrets visible in
          pixels.
        </small>
      </div>
      {full && (
        <dialog
          ref={dialog}
          className="worker-image-dialog"
          aria-labelledby="worker-image-heading"
          onCancel={(event) => {
            event.preventDefault();
            closePreview();
          }}
        >
          <header>
            <div>
              <h3 id="worker-image-heading">Retained screenshot</h3>
              <p>{full.image.filename}</p>
            </div>
            <button
              className="secondary"
              autoFocus
              onClick={closePreview}
              aria-label="Close screenshot"
            >
              <X size={18} />
            </button>
          </header>
          <div>
            <button
              className="secondary"
              onClick={() => setOriginalSize((value) => !value)}
            >
              <Maximize2 size={16} />
              {originalSize ? "Fit to screen" : "View original size"}
            </button>
          </div>
          <div
            className="worker-image-canvas"
            role="region"
            aria-label="Screenshot image"
            tabIndex={0}
            data-original={originalSize}
          >
            <img
              src={full.url}
              alt={`Recorded evidence: ${full.image.filename}`}
            />
          </div>
          <p className="subtle">
            {originalSize
              ? "Use the arrow keys or scroll to explore the original image. "
              : ""}
            Verified against the retained image hash. This preview does not
            approve the product or answer the question.
          </p>
        </dialog>
      )}
    </details>
  );
}
