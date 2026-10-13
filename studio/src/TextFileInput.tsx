/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { useId, useState } from "react";
import { Upload, X } from "lucide-react";
const fileLimit = (bytes: number) =>
  bytes === 1000000
    ? "1 MB (1,000,000 bytes)"
    : `${Math.round(bytes / 1024)} KB`;
export async function readTextFile(
  file: File,
  extensions: string[],
  maxBytes: number,
): Promise<string> {
  if (
    !extensions.some((extension) => file.name.toLowerCase().endsWith(extension))
  )
    throw new Error(`Choose a ${extensions.join(", ")} file.`);
  if (!file.size || file.size > maxBytes)
    throw new Error(
      `Choose a nonempty file of at most ${fileLimit(maxBytes)}.`,
    );
  try {
    return new TextDecoder("utf-8", { fatal: true }).decode(
      await file.arrayBuffer(),
    );
  } catch {
    throw new Error("The file must contain valid UTF-8 text.");
  }
}
export function TextFileInput({
  label,
  extensions,
  maxBytes,
  onRead,
  onClear,
}: {
  label: string;
  extensions: string[];
  maxBytes: number;
  onRead: (value: string, name: string) => void | Promise<void>;
  onClear?: () => void;
}) {
  const id = useId();
  const [name, setName] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  return (
    <div className="field text-file-input">
      <label htmlFor={id}>
        <Upload size={18} />
        {label}
      </label>
      <input
        id={id}
        type="file"
        accept={extensions.join(",")}
        disabled={busy}
        onChange={(event) => {
          const file = event.target.files?.[0];
          event.target.value = "";
          if (!file) return;
          setBusy(true);
          setError("");
          void readTextFile(file, extensions, maxBytes)
            .then((value) => onRead(value, file.name))
            .then(() => setName(file.name))
            .catch((cause) =>
              setError(
                cause instanceof Error
                  ? cause.message
                  : "Could not read this file.",
              ),
            )
            .finally(() => setBusy(false));
        }}
      />
      <small>
        {busy
          ? "Reading file…"
          : name
            ? `Loaded ${name}`
            : `UTF-8 text, up to ${fileLimit(maxBytes)}. The server validates imported content.`}
      </small>
      {error && (
        <p className="file-error" role="alert">
          {error}
        </p>
      )}
      {onClear && (
        <button
          className="quiet"
          type="button"
          onClick={() => {
            onClear();
            setName("");
            setError("");
          }}
        >
          <X size={15} />
          Reset import
        </button>
      )}
    </div>
  );
}
export function validateSvgText(value: string) {
  if (/<!DOCTYPE|<!ENTITY/i.test(value))
    throw new Error("SVG declarations and entities are not supported.");
  const xml = new DOMParser().parseFromString(value, "image/svg+xml");
  if (
    xml.querySelector("parsererror") ||
    xml.documentElement.localName !== "svg"
  )
    throw new Error("Choose a valid SVG document.");
  if (xml.querySelector("script,foreignObject"))
    throw new Error("SVG scripts and embedded HTML are not supported.");
}
