/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
export type WorkerImage = {
  id: string;
  filename: string;
  mime_type: "image/png" | "image/jpeg" | "image/webp";
  bytes: number;
  sha256: string;
};
const invalid = () =>
  new Error("The retained image could not be verified. No preview was opened.");
export function workerEvidenceImages(value: unknown): WorkerImage[] {
  if (!Array.isArray(value) || value.length > 6) throw invalid();
  const images = value.map((item: unknown) => {
    if (!item || typeof item !== "object" || Array.isArray(item))
      throw invalid();
    const image = item as Record<string, unknown>;
    if (
      Object.keys(image).sort().join(",") !==
        "bytes,filename,id,mime_type,sha256" ||
      typeof image.id !== "string" ||
      !/^[a-f0-9]{64}$/.test(image.id) ||
      typeof image.filename !== "string" ||
      !image.filename ||
      Array.from(image.filename).length > 180 ||
      /[/\\]/.test(image.filename) ||
      Array.from(image.filename).some(
        (character) =>
          character.charCodeAt(0) < 32 ||
          character.charCodeAt(0) === 127 ||
          (character.length === 1 &&
            character.charCodeAt(0) >= 0xd800 &&
            character.charCodeAt(0) <= 0xdfff),
      ) ||
      [".", ".."].includes(image.filename) ||
      !["image/png", "image/jpeg", "image/webp"].includes(
        String(image.mime_type),
      ) ||
      !Number.isSafeInteger(image.bytes) ||
      Number(image.bytes) < 1 ||
      Number(image.bytes) > 1_000_000 ||
      typeof image.sha256 !== "string" ||
      !/^[a-f0-9]{64}$/.test(image.sha256)
    )
      throw invalid();
    return image as WorkerImage;
  });
  if (new Set(images.map((image) => image.id)).size !== images.length)
    throw invalid();
  return images;
}
export async function verifiedPreviewBlob(
  response: Response,
  image: WorkerImage,
): Promise<Blob> {
  workerEvidenceImages([image]);
  if (
    !response.ok ||
    response.headers.get("Content-Type")?.split(";")[0] !== image.mime_type ||
    response.headers.get("X-Content-SHA256") !== image.sha256 ||
    !response.body
  )
    throw invalid();
  const reader = response.body.getReader(),
    pieces: Uint8Array[] = [];
  let size = 0;
  try {
    while (true) {
      const next = await reader.read();
      if (next.done) break;
      size += next.value.byteLength;
      if (size > image.bytes || size > 1_000_000) throw invalid();
      pieces.push(next.value);
    }
    if (size !== image.bytes) throw invalid();
  } catch (cause) {
    await reader.cancel().catch(() => undefined);
    throw cause;
  } finally {
    reader.releaseLock();
  }
  const bytes = new Uint8Array(size);
  let offset = 0;
  for (const piece of pieces) {
    bytes.set(piece, offset);
    offset += piece.length;
  }
  const digest = Array.from(
    new Uint8Array(await crypto.subtle.digest("SHA-256", bytes)),
    (byte) => byte.toString(16).padStart(2, "0"),
  ).join("");
  const png = bytes.slice(0, 8).join(",") === "137,80,78,71,13,10,26,10",
    jpeg = bytes[0] === 255 && bytes[1] === 216 && bytes[2] === 255,
    webp =
      new TextDecoder().decode(bytes.slice(0, 4)) === "RIFF" &&
      new TextDecoder().decode(bytes.slice(8, 12)) === "WEBP";
  if (
    digest !== image.sha256 ||
    !(image.mime_type === "image/png"
      ? png
      : image.mime_type === "image/jpeg"
        ? jpeg
        : webp)
  )
    throw invalid();
  return new Blob([bytes], { type: image.mime_type });
}
