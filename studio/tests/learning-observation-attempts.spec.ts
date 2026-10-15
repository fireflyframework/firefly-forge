/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { expect, test } from "@playwright/test";
import { fixture, select, review } from "./observation-attempt-fixture";
test("a terminal refusal requires exact readback before explicit correction", async ({
  page,
}) => {
  const state = await fixture(page, { terminal: true });
  await select(page);
  await review(page);
  await page
    .getByRole("button", { name: "Verify source", exact: true })
    .click();
  await expect(
    page.getByText("Original request was not dispatched", { exact: true }),
  ).toBeVisible();
  expect(state.writes()).toBe(1);
  await page
    .getByRole("button", {
      name: "Review and start a new verification",
      exact: true,
    })
    .click();
  await expect(
    page.getByRole("button", { name: "Verify existing source", exact: true }),
  ).toBeVisible();
  expect(state.writes()).toBe(1);
});
test("terminal correction keeps exact history, restores focus, and uses a fresh request only after review", async ({
  page,
}) => {
  const state = await fixture(page, { terminal: true });
  await select(page);
  await review(page);
  await page
    .getByRole("button", { name: "Verify source", exact: true })
    .click();
  await expect(
    page.getByText("Original request was not dispatched", { exact: true }),
  ).toBeVisible();
  await page
    .getByRole("button", {
      name: "Review and start a new verification",
      exact: true,
    })
    .click();
  const opener = page.getByRole("button", {
    name: "Verify existing source",
    exact: true,
  });
  await expect(opener).toBeFocused();
  expect(state.attemptReads()).toBe(2);
  expect(state.writes()).toBe(1);
  const history = await page.evaluate(() =>
    Object.entries(sessionStorage).filter(([key]) => key.includes(":history:")),
  );
  expect(history).toHaveLength(1);
  expect(history[0][1]).not.toContain("owned-procedure-source");
  await review(page);
  await page
    .getByRole("button", { name: "Verify source", exact: true })
    .click();
  await expect.poll(() => state.writes()).toBe(2);
  expect((state.bodies()[0] as { attempt_id: string }).attempt_id).not.toBe(
    (state.bodies()[1] as { attempt_id: string }).attempt_id,
  );
});
for (const receipt of ["absent", "tampered", "actor", "source", "target"])
  test(`a ${receipt} attempt receipt cannot permit correction or another POST`, async ({
    page,
  }) => {
    const state = await fixture(page, { terminal: true, receipt });
    await select(page);
    await review(page);
    await page
      .getByRole("button", { name: "Verify source", exact: true })
      .click();
    await expect(page.getByRole("alert")).toBeVisible();
    await expect(
      page.getByRole("button", {
        name: "Review and start a new verification",
        exact: true,
      }),
    ).toHaveCount(0);
    expect(state.writes()).toBe(1);
  });
for (const attemptState of ["preparing", "dispatched", "unknown"] as const)
  test(`${attemptState} never unlocks correction after reload`, async ({
    page,
  }) => {
    const state = await fixture(page, { attemptState });
    await select(page);
    await review(page);
    await page
      .getByRole("button", { name: "Verify source", exact: true })
      .click();
    await expect(page.getByRole("alert")).toBeVisible();
    await select(page);
    await page
      .getByRole("button", { name: "Check original verification", exact: true })
      .click();
    await expect(page.getByRole("alert")).toBeVisible();
    expect(state.writes()).toBe(1);
    await expect(
      page.getByRole("button", {
        name: "Review and start a new verification",
        exact: true,
      }),
    ).toHaveCount(0);
  });
test("a terminal receipt does not survive current projection drift as correction authority", async ({
  page,
}) => {
  const state = await fixture(page, {
    terminal: true,
    changedProjection: true,
  });
  await select(page);
  await review(page);
  await page
    .getByRole("button", { name: "Verify source", exact: true })
    .click();
  await page
    .getByRole("button", {
      name: "Review and start a new verification",
      exact: true,
    })
    .click();
  await expect(page.getByRole("alert")).toBeVisible();
  expect(state.writes()).toBe(1);
  expect(
    await page.evaluate(
      () =>
        Object.keys(sessionStorage).filter((k) => k.includes(":history:"))
          .length,
    ),
  ).toBe(0);
});
test("correction rechecks current authority and retains identity on refusal", async ({
  page,
}) => {
  const state = await fixture(page, { terminal: true });
  await select(page);
  await review(page);
  await page
    .getByRole("button", { name: "Verify source", exact: true })
    .click();
  await expect(
    page.getByText("Original request was not dispatched", { exact: true }),
  ).toBeVisible();
  state.deny();
  await page
    .getByRole("button", {
      name: "Review and start a new verification",
      exact: true,
    })
    .click();
  await expect(
    page.getByText(/Private evidence has been cleared/),
  ).toBeVisible();
  expect(state.writes()).toBe(1);
  expect(
    await page.evaluate(
      () =>
        Object.keys(sessionStorage).filter((k) =>
          k.startsWith("forge.learning-observation.v2:"),
        ).length,
    ),
  ).toBe(1);
});
test("archival storage failure keeps the terminal identity and sends nothing", async ({
  page,
}) => {
  const state = await fixture(page, { terminal: true });
  await select(page);
  await review(page);
  await page
    .getByRole("button", { name: "Verify source", exact: true })
    .click();
  await expect(
    page.getByText("Original request was not dispatched", { exact: true }),
  ).toBeVisible();
  await page.evaluate(() => {
    const write = Storage.prototype.setItem;
    Storage.prototype.setItem = function (key, value) {
      if (key.includes(":history:")) throw Error("Unavailable");
      return write.call(this, key, value);
    };
  });
  await page
    .getByRole("button", {
      name: "Review and start a new verification",
      exact: true,
    })
    .click();
  await expect(page.getByRole("alert")).toBeVisible();
  expect(state.writes()).toBe(1);
  await expect(
    page.getByRole("button", { name: "Verify existing source", exact: true }),
  ).toHaveCount(0);
});
test("a changed terminal receipt is not accepted as the original immutable refusal", async ({
  page,
}) => {
  const state = await fixture(page, { terminal: true, changedReceipt: true });
  await select(page);
  await review(page);
  await page
    .getByRole("button", { name: "Verify source", exact: true })
    .click();
  await expect(
    page.getByText("Original request was not dispatched", { exact: true }),
  ).toBeVisible();
  await page
    .getByRole("button", {
      name: "Review and start a new verification",
      exact: true,
    })
    .click();
  await expect(page.getByRole("alert")).toBeVisible();
  expect(state.writes()).toBe(1);
  await expect(
    page.getByRole("button", { name: "Verify existing source", exact: true }),
  ).toHaveCount(0);
});
for (const receipt of ["event", "eventDigest"])
  test(`a verified attempt still requires its exact ${receipt} observation`, async ({
    page,
  }) => {
    const state = await fixture(page, { receipt });
    await select(page);
    await review(page);
    await page
      .getByRole("button", { name: "Verify source", exact: true })
      .click();
    await expect(page.getByRole("alert")).toBeVisible();
    expect(state.writes()).toBe(1);
    await expect(
      page.getByText("Original source verification confirmed", { exact: true }),
    ).toHaveCount(0);
  });
for (const receipt of [undefined, "legacy"])
  test(`a retained v1 marker uses only the legacy exact read (${receipt ?? "matching"})`, async ({
    page,
  }) => {
    const state = await fixture(page, { lost: true, receipt });
    await select(page);
    await review(page);
    await page
      .getByRole("button", { name: "Verify source", exact: true })
      .click();
    await expect(
      page.getByText("Original source verification unresolved", {
        exact: true,
      }),
    ).toBeVisible();
    const original = await page.evaluate(() => {
      const key = Object.keys(sessionStorage).find(
        (key) =>
          key.startsWith("forge.learning-observation.v2:") &&
          !key.includes(":history:"),
      )!;
      const marker = JSON.parse(sessionStorage.getItem(key)!);
      delete marker.inputDigest;
      delete marker.sourceDigest;
      const legacy = JSON.stringify({
        ...marker,
        version: 1,
        source: "owned-procedure-source",
      });
      sessionStorage.setItem(key.replace(".v2:", ".v1:"), legacy);
      sessionStorage.removeItem(key);
      return legacy;
    });
    await select(page);
    await page
      .getByRole("button", { name: "Check original verification", exact: true })
      .click();
    if (receipt) await expect(page.getByRole("alert")).toBeVisible();
    else
      await expect(
        page.getByText("Original source verification confirmed", {
          exact: true,
        }),
      ).toBeVisible();
    expect(state.writes()).toBe(1);
    expect(state.attemptReads()).toBe(0);
    expect(state.eventReads()).toBe(1);
    expect(
      await page.evaluate(() =>
        Object.values(sessionStorage).find((value) =>
          value.includes('"version":1'),
        ),
      ),
    ).toBe(original);
    await expect(
      page.getByRole("button", {
        name: "Review and start a new verification",
        exact: true,
      }),
    ).toHaveCount(0);
  });
for (const viewport of [
  { name: "desktop", width: 1440, height: 900 },
  { name: "phone", width: 390, height: 844 },
  { name: "landscape", width: 844, height: 390 },
])
  for (const theme of ["light", "dark"] as const)
    test(`terminal correction remains painted and keyboard reachable on ${viewport.name} ${theme}`, async ({
      page,
    }, info) => {
      await page.setViewportSize(viewport);
      await page.addInitScript(
        (theme) => localStorage.setItem("forge.appearance", theme),
        theme,
      );
      const state = await fixture(page, { terminal: true });
      await select(page);
      await review(page);
      await page
        .getByRole("button", { name: "Verify source", exact: true })
        .click();
      const correction = page.getByRole("button", {
        name: "Review and start a new verification",
        exact: true,
      });
      await correction.scrollIntoViewIfNeeded();
      const geometry = await correction.evaluate((button) => {
        const box = button.getBoundingClientRect();
        let top = 0,
          bottom = innerHeight,
          left = 0,
          right = innerWidth;
        for (let node = button.parentElement; node; node = node.parentElement) {
          const style = getComputedStyle(node),
            rect = node.getBoundingClientRect();
          if (/hidden|auto|scroll|clip/.test(style.overflowY)) {
            top = Math.max(top, rect.top + parseFloat(style.borderTopWidth));
            bottom = Math.min(
              bottom,
              rect.bottom - parseFloat(style.borderBottomWidth),
            );
          }
          if (/hidden|auto|scroll|clip/.test(style.overflowX)) {
            left = Math.max(
              left,
              rect.left + parseFloat(style.borderLeftWidth),
            );
            right = Math.min(
              right,
              rect.right - parseFloat(style.borderRightWidth),
            );
          }
        }
        return {
          top: box.top,
          bottom: box.bottom,
          left: box.left,
          right: box.right,
          visibleTop: top,
          visibleBottom: bottom,
          visibleLeft: left,
          visibleRight: right,
          hitTop: button.contains(
            document.elementFromPoint(box.left + box.width / 2, box.top + 1),
          ),
          hitBottom: button.contains(
            document.elementFromPoint(box.left + box.width / 2, box.bottom - 1),
          ),
        };
      });
      expect(geometry.top).toBeGreaterThanOrEqual(geometry.visibleTop);
      expect(geometry.bottom).toBeLessThanOrEqual(geometry.visibleBottom);
      expect(geometry.left).toBeGreaterThanOrEqual(geometry.visibleLeft);
      expect(geometry.right).toBeLessThanOrEqual(geometry.visibleRight);
      expect(geometry.hitTop).toBe(true);
      expect(geometry.hitBottom).toBe(true);
      await info.attach("terminal-control-bounds", {
        body: JSON.stringify(geometry),
        contentType: "application/json",
      });
      await page.screenshot({
        path: info.outputPath(`terminal-${viewport.name}-${theme}.png`),
      });
      await correction.focus();
      await page.keyboard.press("Enter");
      const opener = page.getByRole("button", {
        name: "Verify existing source",
        exact: true,
      });
      await expect(opener).toBeFocused();
      await page.keyboard.press("Enter");
      await expect(
        page.getByRole("dialog", {
          name: "Verify existing source",
          exact: true,
        }),
      ).toBeVisible();
      const input = page.getByLabel("Existing source identifier", {
        exact: true,
      });
      await input.scrollIntoViewIfNeeded();
      const painted = await input.evaluate((element) => {
        const box = element.getBoundingClientRect();
        return {
          top: element.contains(
            document.elementFromPoint(box.left + box.width / 2, box.top + 1),
          ),
          bottom: element.contains(
            document.elementFromPoint(box.left + box.width / 2, box.bottom - 1),
          ),
        };
      });
      expect(painted.top).toBe(true);
      expect(painted.bottom).toBe(true);
      await page.screenshot({
        path: info.outputPath(`new-review-${viewport.name}-${theme}.png`),
      });
      expect(state.writes()).toBe(1);
      expect(state.attemptReads()).toBe(2);
    });
test("a confirmed local refusal can lead to a separately reviewed corrected source without persisting either input", async ({
  page,
}) => {
  const state = await fixture(page, {
    terminal: true,
    unassignedSource: true,
    projectionState: "unknown",
  });
  await select(page);
  await review(page);
  await page
    .getByRole("button", { name: "Verify source", exact: true })
    .click();
  await expect(
    page.getByText("Original request was not dispatched", { exact: true }),
  ).toBeVisible();
  await page
    .getByRole("button", {
      name: "Review and start a new verification",
      exact: true,
    })
    .click();
  expect(state.writes()).toBe(1);
  await page
    .getByRole("button", { name: "Verify existing source", exact: true })
    .click();
  const source = page.getByLabel("Existing source identifier", { exact: true });
  await expect(source).toHaveValue("");
  await source.fill("owned-corrected-source");
  await page
    .getByRole("button", { name: "Review source", exact: true })
    .click();
  await page
    .getByLabel(
      "I understand this reads the existing source and records a local verification; it does not submit or ingest a source.",
      { exact: true },
    )
    .check();
  await page
    .getByRole("button", { name: "Verify source", exact: true })
    .click();
  await expect.poll(() => state.writes()).toBe(2);
  const packets = await page.evaluate(() =>
    Object.entries(sessionStorage)
      .filter(([key]) => key.startsWith("forge.learning-observation"))
      .map(([, value]) => value)
      .join("\n"),
  );
  expect(packets).not.toContain("owned-corrected-source");
  expect(packets).not.toContain("owned-procedure-source");
  expect((state.bodies()[1] as { source_id: string }).source_id).toBe(
    "owned-corrected-source",
  );
});
test("an original terminal refusal cannot change into a verified attempt during correction", async ({
  page,
}) => {
  const state = await fixture(page, {
    terminal: true,
    changedTerminalState: true,
  });
  await select(page);
  await review(page);
  await page
    .getByRole("button", { name: "Verify source", exact: true })
    .click();
  await expect(
    page.getByText("Original request was not dispatched", { exact: true }),
  ).toBeVisible();
  await page
    .getByRole("button", {
      name: "Review and start a new verification",
      exact: true,
    })
    .click();
  await expect(page.getByRole("alert")).toBeVisible();
  expect(state.eventReads()).toBe(0);
  expect(state.writes()).toBe(1);
  await expect(
    page.getByRole("button", { name: "Verify existing source", exact: true }),
  ).toHaveCount(0);
});
