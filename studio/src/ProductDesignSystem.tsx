/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { Grid3X3, Palette, Type } from "lucide-react";
import type { ProductBrand, ProductVisualDesign } from "./api";
import { TextFileInput } from "./TextFileInput";
export const defaultProductDesign: ProductVisualDesign = {
  renderer: "product-reference-template-v2",
  geometry_configurable: true,
  baseline: 4,
  desktop: {
    viewport_width: 1440,
    viewport_height: 900,
    columns: 12,
    gutter: 24,
    margin: 32,
  },
  mobile: {
    viewport_width: 390,
    viewport_height: 844,
    columns: 4,
    gutter: 16,
    margin: 20,
  },
  shell: {
    header_height: 76,
    sidebar_width: 248,
    footer_height: 64,
    max_content_width: 1440,
  },
  atomic_hierarchy: ["atoms", "molecules", "organisms", "templates", "pages"],
  states: ["default", "loading", "empty", "error", "focus"],
  accessibility: [
    "semantic-controls",
    "keyboard",
    "visible-focus",
    "contrast",
    "status-announcements",
  ],
  guidance: [],
};
export function normalizeProductDesign(
  value?: ProductVisualDesign,
): ProductVisualDesign {
  return {
    ...defaultProductDesign,
    ...value,
    renderer: "product-reference-template-v2",
    geometry_configurable: true,
    desktop: { ...defaultProductDesign.desktop, ...value?.desktop },
    mobile: { ...defaultProductDesign.mobile, ...value?.mobile },
    shell: { ...defaultProductDesign.shell, ...value?.shell },
    atomic_hierarchy: defaultProductDesign.atomic_hierarchy,
    states: defaultProductDesign.states,
    accessibility: defaultProductDesign.accessibility,
  };
}
export function validateProductDesign(value: ProductVisualDesign) {
  const bounds = {
    desktop: {
      viewport_width: [1024, 1920],
      viewport_height: [900, 1600],
      columns: [6, 16],
      gutter: [0, 48],
      margin: [12, 64],
    },
    mobile: {
      viewport_width: [320, 600],
      viewport_height: [844, 1400],
      columns: [2, 6],
      gutter: [0, 32],
      margin: [12, 40],
    },
    shell: {
      header_height: [0, 128],
      sidebar_width: [0, 360],
      footer_height: [0, 100],
      max_content_width: [640, 1920],
    },
  };
  for (const scope of ["desktop", "mobile", "shell"] as const)
    for (const [field, [minimum, maximum]] of Object.entries(bounds[scope])) {
      const number = (value[scope] as Record<string, number>)[field];
      if (!Number.isInteger(number) || number < minimum || number > maximum)
        throw new Error(
          `${scope}.${field}: choose an integer from ${minimum} to ${maximum}.`,
        );
    }
  if (
    ![2, 4, 8].includes(value.baseline) ||
    value.guidance.length > 30 ||
    value.guidance.some(
      (line) => typeof line !== "string" || !line.trim() || line.length > 2000,
    )
  )
    throw new Error(
      "Use a supported baseline and at most 30 bounded guidance entries.",
    );

  for (const [kind, minimumContent, minimumColumn, minimumHeight] of [
    ["desktop", 600, 36, 740],
    ["mobile", 240, 24, 680],
  ] as const) {
    const grid = value[kind],
      rail = kind === "desktop" ? value.shell.sidebar_width : 0;
    const available = Math.min(
      grid.viewport_width - rail - grid.margin * 2,
      value.shell.max_content_width,
    );
    if (
      available < minimumContent ||
      (available - (grid.columns - 1) * grid.gutter) / grid.columns <
        minimumColumn
    )
      throw new Error(
        `${kind}: reduce columns, gutters, margins or sidebar width to leave readable content.`,
      );
    if (
      (grid.viewport_height ?? (kind === "desktop" ? 900 : 844)) -
        value.shell.header_height -
        value.shell.footer_height <
      minimumHeight
    )
      throw new Error(
        `${kind}: increase canvas height or reduce header/footer height to leave ${minimumHeight}px of content.`,
      );
  }
  for (const [name, minimum] of [
    ["header_height", 48],
    ["sidebar_width", 160],
    ["footer_height", 40],
  ] as const)
    if (value.shell[name] && value.shell[name] < minimum)
      throw new Error(
        `${name.replaceAll("_", " ")} must be 0 (disabled) or at least ${minimum}px.`,
      );
}
export function ProductDesignSystem({
  value,
  brand,
  onChange,
}: {
  value: ProductVisualDesign;
  brand: ProductBrand | null;
  onChange: (value: ProductVisualDesign) => void;
}) {
  const titles: Record<string, string> = {
    viewport_width: "Canvas width",
    viewport_height: "Canvas height",
    columns: "Columns",
    gutter: "Column gutter",
    margin: "Outer margin",
    header_height: "Header height",
    sidebar_width: "Desktop sidebar width",
    footer_height: "Footer height",
    max_content_width: "Maximum content width",
  };
  const range = {
    desktop: {
      viewport_width: [1024, 1920],
      viewport_height: [900, 1600],
      columns: [6, 16],
      gutter: [0, 48],
      margin: [12, 64],
    },
    mobile: {
      viewport_width: [320, 600],
      viewport_height: [844, 1400],
      columns: [2, 6],
      gutter: [0, 32],
      margin: [12, 40],
    },
    shell: {
      header_height: [0, 128],
      sidebar_width: [0, 360],
      footer_height: [0, 100],
      max_content_width: [640, 1920],
    },
  };
  return (
    <section className="product-design-system">
      <div className="feature-toolbar">
        <div>
          <h3>
            <Grid3X3 size={24} />
            Product grid and layout
          </h3>
          <p>
            These settings belong to this product and drive its actual reference
            geometry. They do not change the factory's interface.
          </p>
        </div>
        <button
          type="button"
          className="secondary"
          onClick={() => onChange(structuredClone(defaultProductDesign))}
        >
          Use Firefly defaults
        </button>
      </div>
      <div className="design-system-authority">
        <Type size={20} />
        <span>
          Typography: {brand?.font_family || "Manrope reference default"}
        </span>
        <Palette size={20} />
        <span>
          {brand
            ? `${brand.name} · brand revision ${brand.revision}`
            : "No saved product brand"}
        </span>
      </div>
      <p className="feature-meta">
        The saved product brand supplies font and palette. Import tokens or
        artwork in the Brand step; the grid is saved in the product definition.
      </p>
      <div className="field">
        <label htmlFor="product-baseline">Spacing baseline</label>
        <select
          id="product-baseline"
          value={value.baseline}
          onChange={(event) =>
            onChange({
              ...value,
              baseline: Number(event.target.value) as 2 | 4 | 8,
            })
          }
        >
          {[2, 4, 8].map((number) => (
            <option key={number} value={number}>
              {number}px
            </option>
          ))}
        </select>
      </div>
      <div className="design-system-grids">
        {(["desktop", "mobile"] as const).map((kind) => (
          <fieldset key={kind}>
            <legend>
              {kind === "desktop"
                ? "Desktop product canvas"
                : "Mobile product canvas"}
            </legend>
            <div
              className="design-grid-map"
              aria-label={`${kind} draft grid`}
              style={{
                gridTemplateColumns: `repeat(${value[kind].columns},minmax(0,1fr))`,
                gap: `${Math.min(value[kind].gutter, 24)}px`,
              }}
            >
              {Array.from({ length: value[kind].columns }, (_, index) => (
                <span key={index} />
              ))}
            </div>
            <div className="two-columns">
              {Object.entries(range[kind]).map(([key, [min, max]]) => (
                <div className="field" key={key}>
                  <label htmlFor={`product-grid-${kind}-${key}`}>
                    {titles[key]}
                  </label>
                  <input
                    id={`product-grid-${kind}-${key}`}
                    type="number"
                    required
                    min={min}
                    max={max}
                    step={1}
                    value={
                      value[kind][key as keyof typeof value.desktop] ??
                      (kind === "desktop" ? 900 : 844)
                    }
                    onChange={(event) =>
                      onChange({
                        ...value,
                        [kind]: {
                          ...value[kind],
                          [key]: Number(event.target.value),
                        },
                      })
                    }
                  />
                </div>
              ))}
            </div>
          </fieldset>
        ))}
      </div>
      <fieldset>
        <legend>Product shell regions</legend>
        <div className="two-columns">
          {Object.entries(range.shell).map(([key, [min, max]]) => (
            <div className="field" key={key}>
              <label htmlFor={`product-shell-${key}`}>{titles[key]}</label>
              <input
                id={`product-shell-${key}`}
                type="number"
                required
                min={min}
                max={max}
                step={1}
                value={value.shell[key as keyof typeof value.shell]}
                onChange={(event) =>
                  onChange({
                    ...value,
                    shell: {
                      ...value.shell,
                      [key]: Number(event.target.value),
                    },
                  })
                }
              />
              {key !== "max_content_width" && (
                <small>
                  0 disables the region. Minimum visible size:
                  {key === "header_height"
                    ? 48
                    : key === "footer_height"
                      ? 40
                      : 160}
                  px.
                </small>
              )}
            </div>
          ))}
        </div>
      </fieldset>
      <details>
        <summary>Atomic structure, states and accessibility</summary>
        <p>Hierarchy:{value.atomic_hierarchy.join(" → ")}</p>
        <p>States:{value.states.join(", ")}</p>
        <p>
          Accessibility:{value.accessibility.join(", ").replaceAll("-", " ")}
        </p>
      </details>
      <div className="field">
        <label htmlFor="product-design-guidance">Product design guidance</label>
        <textarea
          id="product-design-guidance"
          rows={3}
          value={value.guidance.join("\n")}
          onChange={(event) =>
            onChange({
              ...value,
              guidance: event.target.value
                .split("\n")
                .map((line) => line.trim())
                .filter(Boolean),
            })
          }
        />
        <small>One product-specific instruction per line.</small>
      </div>
      <details>
        <summary>Import an existing grid contract</summary>
        <TextFileInput
          label="Upload product grid JSON"
          extensions={[".json"]}
          maxBytes={65536}
          onRead={(text) => {
            const input = JSON.parse(text) as ProductVisualDesign;
            if (!input.desktop || !input.mobile || !input.shell)
              throw new Error(
                "The JSON needs desktop, mobile and shell fields.",
              );
            const normalized = normalizeProductDesign(input);
            validateProductDesign(normalized);
            onChange(normalized);
          }}
        />
      </details>
    </section>
  );
}
