/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { useEffect, useRef, useState } from "react";
import {
  CheckCircle2,
  Download,
  ExternalLink,
  Grid3X3,
  Monitor,
  Palette,
  Smartphone,
  Workflow,
} from "lucide-react";
import {
  request,
  type Connection,
  type CustomerProductDefinition,
  type ProductDesignReview,
} from "./api";
type Frame = {
  id: string;
  title: string;
  desktop_svg: string;
  mobile_svg: string;
  desktop_dark_svg: string;
  mobile_dark_svg: string;
  interaction_contract: {
    journey: string;
    acceptance_ids: string[];
    primary_action: string;
    states: string[];
    feature_ids?: string[];
    expected_behaviors?: {
      feature_id: string;
      id: string;
      given: string;
      when: string;
      then: string;
      failure_paths: string[];
    }[];
  };
};
type Grid = {
  renderer: string;
  geometry_configurable: boolean;
  baseline: number;
  desktop: {
    viewport_width: number;
    columns: number;
    gutter: number;
    margin: number;
  };
  mobile: {
    viewport_width: number;
    columns: number;
    gutter: number;
    margin: number;
  };
  shell: {
    header_height: number;
    sidebar_width: number;
    footer_height: number;
    max_content_width: number;
  };
  scope: "product_ui" | "diagram_document";
};
type Bundle = {
  digest: string;
  definition_digest: string;
  brand_digest: string | null;
  method: "wireframe" | "process_map";
  font_family: string;
  font_css?: string;
  screens: Frame[];
  coverage: { journeys: number; screen_count: number };
  functional_definition_status?: "ready" | "pending";
  functional_contract?: {
    pending_fields: {
      journey?: string;
      process_id?: string;
      step_id?: string;
      field: string;
    }[];
  };
  requirements?: { grid?: Grid };
};
type Viewport = "desktop" | "mobile";
type Tone = "light" | "dark";
export function DesignReview({
  connection,
  repository,
  definition,
  value,
  pending = false,
  onChange,
  onPending,
}: {
  connection: Connection;
  repository: string;
  definition: CustomerProductDefinition;
  value: ProductDesignReview | null;
  pending?: boolean;
  onChange: (review: ProductDesignReview | null) => void;
  onPending: (pending: boolean) => void;
}) {
  const previewRequest = useRef<AbortController | null>(null);
  useEffect(() => () => previewRequest.current?.abort(), []);
  const dialog = useRef<HTMLDialogElement>(null);
  const [viewerOpen, setViewerOpen] = useState(false);
  useEffect(() => {
    if (viewerOpen) dialog.current?.showModal();
    else dialog.current?.close();
  }, [viewerOpen]);
  const [bundle, setBundle] = useState<Bundle | null>(null);
  const [reviewed, setReviewed] = useState<string[]>([]);
  const [viewed, setViewed] = useState<Record<string, string[]>>({});
  const [selected, setSelected] = useState("");
  const [viewport, setViewport] = useState<Viewport>("desktop");
  const [tone, setTone] = useState<Tone>("light");
  const [compare, setCompare] = useState(false);
  const [zoom, setZoom] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const screen =
    bundle?.screens.find((item) => item.id === selected) ?? bundle?.screens[0];
  const process = bundle?.method === "process_map";
  const incomplete =
    process && bundle?.functional_definition_status !== "ready";
  const label = process ? "Process reference" : "Wireframe reference";
  function renderedFrame(svg: string) {
    if (!bundle?.font_css) return svg;
    const end = svg.indexOf(">") + 1;
    return `${svg.slice(0, end)}<style>${bundle.font_css}</style>${svg.slice(end)}`;
  }
  function svgFor(item: Frame, view: Viewport, theme: Tone) {
    return renderedFrame(
      view === "desktop"
        ? theme === "light"
          ? item.desktop_svg
          : item.desktop_dark_svg
        : theme === "light"
          ? item.mobile_svg
          : item.mobile_dark_svg,
    );
  }
  async function generate() {
    setBusy(true);
    setError("");
    const controller = new AbortController();
    previewRequest.current?.abort();
    previewRequest.current = controller;
    try {
      const next = await request<Bundle>(
        connection,
        `/repositories/${repository}/design/preview`,
        {
          method: "POST",
          body: JSON.stringify({
            product_definition: { ...definition, design_review: null },
          }),
          signal: controller.signal,
        },
      );
      if (controller.signal.aborted) return;
      setBundle(next);
      setViewerOpen(true);
      setSelected(next.screens[0]?.id ?? "");
      setReviewed([]);
      setViewed({});
      onPending(true);
      onChange(null);
    } catch (cause) {
      if (controller.signal.aborted) return;
      setError(
        cause instanceof Error
          ? cause.message
          : "The design reference could not be generated.",
      );
    } finally {
      if (!controller.signal.aborted) setBusy(false);
    }
  }
  function check(id: string, checked: boolean) {
    if (!bundle || incomplete || ((viewed[id]?.length ?? 0) < 4 && checked))
      return;
    const ids = checked
      ? [...reviewed.filter((value) => value !== id), id]
      : reviewed.filter((value) => value !== id);
    setReviewed(ids);
    onPending(ids.length !== bundle.screens.length);
    onChange(
      ids.length === bundle.screens.length
        ? {
            digest: bundle.digest,
            reviewed: true,
            screen_ids: ids,
            viewports: ["desktop", "mobile"],
            themes: ["light", "dark"],
            method: bundle.method,
          }
        : null,
    );
  }
  function recordView(id: string, view: Viewport, theme: Tone) {
    const key = `${view}/${theme}`;
    setViewed((current) =>
      current[id]?.includes(key)
        ? current
        : { ...current, [id]: [...(current[id] ?? []), key] },
    );
  }
  function open(full: boolean) {
    if (!screen) return;
    const url = URL.createObjectURL(
      new Blob([svgFor(screen, viewport, tone)], { type: "image/svg+xml" }),
    );
    if (full) window.open(url, "_blank", "noopener,noreferrer");
    else {
      const link = document.createElement("a");
      link.href = url;
      link.download = `${screen.id}-${viewport}-${tone}.svg`;
      link.click();
    }
    window.setTimeout(() => URL.revokeObjectURL(url), 60000);
  }
  function removeReferenceReview() {
    setBundle(null);
    setReviewed([]);
    setViewed({});
    onChange(null);
    onPending(false);
    setViewerOpen(false);
  }
  const grid = bundle?.requirements?.grid;
  const layout = grid?.[viewport];
  return (
    <section className="design-review">
      <div className="feature-toolbar">
        <div>
          <h3>
            <Workflow size={24} />
            Journey and design review
          </h3>
          <p>
            Review the layout, then each journey's actual reference variants and
            interaction criteria. These are implementation references; final
            behavior and visual fidelity require separate tests.
          </p>
        </div>
        <button
          type="button"
          className="secondary"
          disabled={
            busy ||
            Boolean(
              definition.architecture && !definition.architecture.reviewed,
            )
          }
          onClick={() => void generate()}
        >
          {busy
            ? "Generating references…"
            : bundle
              ? "Regenerate references"
              : definition.architecture?.frontend === "none"
                ? "Generate process references"
                : "Generate wireframes"}
        </button>
      </div>
      {definition.architecture && !definition.architecture.reviewed && (
        <p>
          Review the architecture first so references match the definition you
          will save.
        </p>
      )}
      {pending && !bundle && (
        <div className="alert" role="status">
          <p>
            Reference review was interrupted. The unfinished views are no longer
            available in this editor. Generate them again, or remove the
            unfinished review to save a draft without it.
          </p>
          <button
            type="button"
            className="secondary"
            onClick={removeReferenceReview}
          >
            Remove reference review
          </button>
        </div>
      )}
      {value && !bundle && (
        <p>
          Saved design review <code>{value.digest}</code>. Regenerate after
          changing the definition or brand.
        </p>
      )}
      {error && (
        <div className="alert" role="alert">
          {error}
        </div>
      )}
      {bundle && (
        <button
          type="button"
          className="secondary"
          onClick={() => setViewerOpen(true)}
        >
          Review journey references · {bundle.screens.length - reviewed.length}{" "}
          pending
        </button>
      )}
      {bundle && (
        <dialog
          ref={dialog}
          className="design-viewer"
          aria-label="Journey reference viewer"
          onCancel={(event) => {
            event.preventDefault();
            setViewerOpen(false);
          }}
        >
          <header className="design-viewer-heading">
            <div>
              <h3>{process ? "Process references" : "Journey references"}</h3>
              <p>
                {bundle.screens.length - reviewed.length} journeys awaiting
                acknowledgement · {bundle.font_family} · {label}
              </p>
            </div>
            <button
              type="button"
              className="secondary"
              onClick={() => setViewerOpen(false)}
            >
              Done reviewing journeys
            </button>
          </header>
          {incomplete && (
            <div className="alert" role="alert">
              <strong>
                Complete the functional definition before acknowledging process
                references.
              </strong>
              <ul>
                {bundle.functional_contract?.pending_fields.map(
                  (item, index) => (
                    <li key={index}>
                      {[item.journey, item.process_id, item.step_id]
                        .filter(Boolean)
                        .join(" · ")}
                      : {item.field.replaceAll("_", " ")}
                    </li>
                  ),
                )}
              </ul>
            </div>
          )}
          <details className="design-layout">
            <summary>
              <Grid3X3 size={20} />
              Grid and layout
              {layout &&
                ` · ${layout.columns} columns · ${layout.viewport_width}px canvas`}
            </summary>
            {grid && layout ? (
              <>
                <div
                  className="design-grid-map"
                  style={{
                    gridTemplateColumns: `repeat(${layout.columns},minmax(0,1fr))`,
                    gap: `${Math.min(layout.gutter, 24)}px`,
                  }}
                  aria-label={`${viewport} reference grid with ${layout.columns} columns`}
                >
                  {Array.from({ length: layout.columns }, (_, index) => (
                    <span key={index} />
                  ))}
                </div>
                <p>
                  {layout.viewport_width}px canvas · {layout.columns} columns ·{" "}
                  {layout.gutter}px gutter · {layout.margin}px margins ·{" "}
                  {grid.baseline}px baseline
                </p>
                <p className="feature-meta">
                  {grid.scope === "diagram_document"
                    ? "Diagram document layout; no application frontend is selected."
                    : `${grid.shell.header_height}px header · ${grid.shell.sidebar_width}px sidebar · ${grid.shell.footer_height}px footer · ${grid.shell.max_content_width}px maximum content width.`}{" "}
                  {grid.geometry_configurable
                    ? "Configurable geometry"
                    : "Fixed reference renderer"}
                </p>
              </>
            ) : (
              <p>
                No typed grid contract was returned for this reference bundle.
              </p>
            )}
          </details>
          <div className="design-review-summary">
            <strong>
              {bundle.screens.length - reviewed.length} journeys awaiting
              acknowledgement
            </strong>
            <span>
              {bundle.font_family} · {label}
            </span>
            <details>
              <summary>Bound reference identities</summary>
              <p>
                Definition <code>{bundle.definition_digest}</code>
              </p>
              <p>
                Brand{" "}
                <code>{bundle.brand_digest || "No saved product brand"}</code>
              </p>
              <p>
                Reference <code>{bundle.digest}</code>
              </p>
              <p>
                Editing the definition or saved brand invalidates this review.
              </p>
            </details>
          </div>
          <div className="design-workbench">
            <nav className="design-journeys" aria-label="Journey references">
              <ol>
                {bundle.screens.map((item, index) => (
                  <li key={item.id}>
                    <button
                      type="button"
                      aria-pressed={screen?.id === item.id}
                      aria-label={`Journey ${index + 1}: ${item.title}`}
                      onClick={() => {
                        setSelected(item.id);
                        setZoom(false);
                      }}
                    >
                      <span className="journey-number">{index + 1}</span>
                      <span>
                        <strong>{item.title}</strong>
                        <small>
                          {reviewed.includes(item.id)
                            ? "Acknowledged"
                            : `${viewed[item.id]?.length ?? 0}/4 variants viewed`}
                        </small>
                      </span>
                      {reviewed.includes(item.id) && <CheckCircle2 size={18} />}
                    </button>
                  </li>
                ))}
              </ol>
            </nav>
            {screen && (
              <section
                className="design-selected"
                aria-label="Selected journey reference"
              >
                <header>
                  <div>
                    <h4>{screen.title}</h4>
                    <p>{screen.interaction_contract.journey}</p>
                  </div>
                </header>
                <div className="design-preview-controls">
                  <div
                    className="feature-tabs"
                    role="group"
                    aria-label="Preview viewport"
                  >
                    <button
                      type="button"
                      aria-pressed={viewport === "desktop"}
                      onClick={() => {
                        setViewport("desktop");
                        setZoom(false);
                      }}
                    >
                      <Monitor size={18} />
                      Desktop
                    </button>
                    <button
                      type="button"
                      aria-pressed={viewport === "mobile"}
                      onClick={() => {
                        setViewport("mobile");
                        setZoom(false);
                      }}
                    >
                      <Smartphone size={18} />
                      Mobile
                    </button>
                  </div>
                  <div
                    className="feature-tabs"
                    role="group"
                    aria-label="Preview theme"
                  >
                    <button
                      type="button"
                      aria-pressed={tone === "light"}
                      onClick={() => setTone("light")}
                    >
                      <Palette size={18} />
                      Light
                    </button>
                    <button
                      type="button"
                      aria-pressed={tone === "dark"}
                      onClick={() => setTone("dark")}
                    >
                      <Palette size={18} />
                      Dark
                    </button>
                  </div>
                  <label className="check-choice">
                    <input
                      type="checkbox"
                      checked={compare}
                      onChange={(event) => setCompare(event.target.checked)}
                    />
                    Compare themes
                  </label>
                </div>
                <div
                  className={`design-preview-body ${compare ? "comparison" : ""} ${zoom ? "native-size" : ""}`}
                >
                  {(compare
                    ? [tone, tone === "light" ? "dark" : "light"]
                    : [tone]
                  ).map((theme) => {
                    const svg = svgFor(screen, viewport, theme as Tone);
                    const xml = new DOMParser().parseFromString(
                      svg,
                      "image/svg+xml",
                    );
                    const width =
                      Number(xml.documentElement.getAttribute("width")) ||
                      undefined;
                    return (
                      <figure key={`${screen.id}/${viewport}/${theme}`}>
                        <figcaption>
                          {viewport} · {theme}
                        </figcaption>
                        <img
                          style={
                            zoom && width
                              ? { width: `${width}px`, maxWidth: "none" }
                              : undefined
                          }
                          src={`data:image/svg+xml;charset=utf-8,${encodeURIComponent(svg)}`}
                          alt={`${screen.title} ${viewport} ${theme} ${label.toLowerCase()}`}
                          onLoad={() =>
                            recordView(screen.id, viewport, theme as Tone)
                          }
                          onError={() =>
                            setError(
                              "This reference could not be rendered. Regenerate it before acknowledging the journey.",
                            )
                          }
                        />
                      </figure>
                    );
                  })}
                </div>
                <div className="design-export-actions">
                  <button
                    type="button"
                    className="quiet"
                    aria-pressed={zoom}
                    onClick={() => setZoom((value) => !value)}
                  >
                    {zoom ? "Fit width" : "100%"}
                  </button>
                  <button
                    type="button"
                    className="secondary"
                    aria-label="Open full size"
                    onClick={() => open(true)}
                  >
                    <ExternalLink size={16} />
                    Open SVG
                  </button>
                  <button
                    type="button"
                    className="secondary"
                    aria-label="Download SVG"
                    onClick={() => open(false)}
                  >
                    <Download size={16} />
                    Download
                  </button>
                </div>
                <div className="design-interaction">
                  <details className="design-criteria">
                    <summary>Interaction criteria</summary>
                    <p>
                      Primary action:{" "}
                      {screen.interaction_contract.primary_action}. States:{" "}
                      {screen.interaction_contract.states.join(", ")}.
                      Acceptance references:{" "}
                      {screen.interaction_contract.acceptance_ids.join(", ") ||
                        "None returned"}
                      .
                    </p>
                    {screen.interaction_contract.feature_ids?.length ? (
                      <p>
                        Mapped features:{" "}
                        {screen.interaction_contract.feature_ids.join(", ")}
                      </p>
                    ) : (
                      <p>Feature mapping is not complete for this reference.</p>
                    )}
                    {screen.interaction_contract.expected_behaviors?.map(
                      (behavior) => (
                        <article key={`${behavior.feature_id}/${behavior.id}`}>
                          <strong>
                            {behavior.feature_id} / {behavior.id}
                          </strong>
                          <p>Given {behavior.given}</p>
                          <p>When {behavior.when}</p>
                          <p>Then {behavior.then}</p>
                          <ul>
                            {behavior.failure_paths.map((path, index) => (
                              <li key={index}>{path}</li>
                            ))}
                          </ul>
                        </article>
                      ),
                    )}
                  </details>
                  <label className="check-choice">
                    <input
                      type="checkbox"
                      disabled={
                        incomplete || (viewed[screen.id]?.length ?? 0) < 4
                      }
                      checked={reviewed.includes(screen.id)}
                      onChange={(event) =>
                        check(screen.id, event.target.checked)
                      }
                    />
                    I reviewed this journey's desktop and mobile references in
                    light and dark themes.
                  </label>
                  <p className="feature-meta">
                    Viewed {viewed[screen.id]?.length ?? 0}/4 variants.
                    Acknowledgement is your explicit review, not an AI approval.
                  </p>
                </div>
              </section>
            )}
          </div>
          <button
            type="button"
            className="quiet"
            onClick={removeReferenceReview}
          >
            Remove reference review
          </button>
        </dialog>
      )}
    </section>
  );
}
