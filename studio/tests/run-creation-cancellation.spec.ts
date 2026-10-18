/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { expect, test } from "@playwright/test";
import {
  fixture,
  open,
  submit,
  markers,
} from "./run-creation-cancellation-fixture";

test("a missing decision holds the original until explicit verified cancellation allows new input", async ({
  page,
  context,
}) => {
  const f = await fixture(context);
  await open(page, true);
  expect(await page.evaluate(() => typeof navigator.locks.request)).toBe(
    "function",
  );
  await submit(page, "Original fictional work");
  const [original] = await markers(page);
  await page
    .getByRole("button", { name: "Check original result", exact: true })
    .click();
  await expect(
    page.getByRole("button", {
      name: "Send original request again",
      exact: true,
    }),
  ).toBeVisible();
  expect((await markers(page))[0].requestId).toBe(original.requestId);
  expect(f.calls.filter((call) => call.action === "original")).toHaveLength(1);
  await page
    .getByRole("button", { name: "Cancel this request", exact: true })
    .click();
  await expect(
    page.getByText("Request cancelled. You can edit and start again.", {
      exact: true,
    }),
  ).toBeVisible();
  expect(await markers(page)).toEqual([]);
  await page.getByRole("button", { name: "Back", exact: true }).click();
  await page.getByRole("button", { name: "Back", exact: true }).click();
  await page.getByRole("button", { name: "Back", exact: true }).click();
  await submit(page, "New fictional work after cancellation");
  const [next] = await markers(page);
  expect(next.requestId).not.toBe(original.requestId);
  expect(f.calls.map((call) => call.action)).toEqual([
    "original",
    "decision",
    "cancel",
    "original",
  ]);
});

test("a delayed old cancellation cannot erase a new request retained in another tab", async ({
  page,
  context,
}) => {
  const f = await fixture(context);
  await open(page, true);
  await submit(page, "First tab original");
  const [original] = await markers(page);
  f.delay();
  await page
    .getByRole("button", { name: "Cancel this request", exact: true })
    .click();
  await expect
    .poll(() => f.calls.filter((call) => call.action === "cancel").length)
    .toBe(1);
  const other = await context.newPage();
  await open(other);
  await expect(
    other.getByRole("button", { name: "Check original result", exact: true }),
  ).toBeVisible();
  await other
    .getByRole("button", { name: "Check original result", exact: true })
    .click();
  await expect(
    other.getByText("Request cancelled. You can edit and start again.", {
      exact: true,
    }),
  ).toBeVisible();
  await submit(other, "Second tab new request");
  const [next] = await markers(other);
  expect(next.requestId).not.toBe(original.requestId);
  f.release();
  await expect(
    page.getByText(
      "This request is resolved. Another request is waiting; check its result before starting again.",
      { exact: true },
    ),
  ).toBeVisible();
  expect((await markers(page))[0].requestId).toBe(next.requestId);
  await other.reload();
  await open(other);
  await other
    .getByRole("button", { name: "Check original result", exact: true })
    .click();
  await expect(
    other.getByRole("button", {
      name: "Send original request again",
      exact: true,
    }),
  ).toBeVisible();
  expect((await markers(other))[0].requestId).toBe(next.requestId);
  expect(f.calls.filter((call) => call.action === "original")).toHaveLength(2);
  await other.close();
});
