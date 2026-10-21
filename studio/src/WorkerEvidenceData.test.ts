/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { expect, it } from "vitest";
import {
  workerEvidenceImages,
  verifiedPreviewBlob,
} from "./WorkerEvidenceData";
const record = {
  id: "a".repeat(64),
  filename: "choice.png",
  mime_type: "image/png" as const,
  bytes: 8,
  sha256: "b".repeat(64),
};
it("accepts bounded opaque raster identities and refuses paths, SVG, bool sizes and duplicates", () => {
  expect(workerEvidenceImages([record])).toEqual([record]);
  for (const value of [
    [{ ...record, id: "../private" }],
    [{ ...record, filename: "../choice.png" }],
    [{ ...record, mime_type: "image/svg+xml" }],
    [{ ...record, bytes: true }],
    [record, record],
    Array(7).fill(record),
  ])
    expect(() => workerEvidenceImages(value)).toThrow();
});
it("rejects incorrect image bytes before creating any preview URL", async () => {
  await expect(
    verifiedPreviewBlob(
      new Response(new Uint8Array(8), {
        headers: {
          "Content-Type": "image/png",
          "X-Content-SHA256": record.sha256,
        },
      }),
      record,
    ),
  ).rejects.toThrow();
  await expect(
    verifiedPreviewBlob(
      new Response(new Uint8Array(1_000_001), {
        headers: {
          "Content-Type": "image/png",
          "X-Content-SHA256": record.sha256,
        },
      }),
      record,
    ),
  ).rejects.toThrow();
});
it("creates a raster blob only after the exact admitted bytes, MIME and hash match", async () => {
  const bytes = new Uint8Array([137, 80, 78, 71, 13, 10, 26, 10]);
  const sha256 = Array.from(
    new Uint8Array(await crypto.subtle.digest("SHA-256", bytes)),
    (byte) => byte.toString(16).padStart(2, "0"),
  ).join("");
  const image = { ...record, sha256 };
  const blob = await verifiedPreviewBlob(
    new Response(bytes, {
      headers: { "Content-Type": image.mime_type, "X-Content-SHA256": sha256 },
    }),
    image,
  );
  expect(blob.type).toBe("image/png");
  expect(blob.size).toBe(8);
  await expect(
    verifiedPreviewBlob(
      new Response(bytes, {
        headers: {
          "Content-Type": "image/svg+xml",
          "X-Content-SHA256": sha256,
        },
      }),
      image,
    ),
  ).rejects.toThrow();
});
