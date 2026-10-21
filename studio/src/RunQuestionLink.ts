/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
export function linkedRun(query: string): { id: string; questions: boolean } | null {
  const parameters = new URLSearchParams(query), runs = parameters.getAll("run"), views = parameters.getAll("view");
  if (runs.length !== 1 || !/^[a-f0-9]{32}$/.test(runs[0]) || views.length > 1 || (views.length === 1 && views[0] !== "questions")) return null;
  return { id: runs[0], questions: views[0] === "questions" };
}
