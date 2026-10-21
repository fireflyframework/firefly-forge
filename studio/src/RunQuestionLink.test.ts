/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { expect, it } from "vitest";
import { linkedRun } from "./RunQuestionLink";
it("accepts only a single native run identity and the questions destination", () => {
  expect(linkedRun(`?run=${"a".repeat(32)}&view=questions`)).toEqual({
    id: "a".repeat(32),
    questions: true,
  });
  for (const query of [
    "?run=../private",
    `?run=${"a".repeat(32)}&run=${"b".repeat(32)}`,
    "?run=https://private",
  ])
    expect(linkedRun(query)).toBeNull();
});
