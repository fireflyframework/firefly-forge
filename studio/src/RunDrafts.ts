/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import type { ProductDraftBook } from "./ProductDrafts";

export function selectStandaloneRunSource(
  drafts: ProductDraftBook,
  repository: string,
) {
  const session = drafts.repository(`run:standalone:${repository}`);
  if (session.read("repositoryId", repository) !== repository)
    session.write("repositoryId", repository, true);
  return session;
}
