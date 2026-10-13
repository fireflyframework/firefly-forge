/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
 * https://www.apache.org/licenses/LICENSE-2.0 */
import { createHash } from "node:crypto";
export function modelFileName(bytes) {
  return `forge-operational-model.${createHash("sha256").update(bytes).digest("hex").slice(0, 16)}.json`;
}
