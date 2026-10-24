/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { renderToStaticMarkup } from "react-dom/server";
import { expect, it } from "vitest";
import fixture from "../tests/git-delivery-contract-fixture.json";
import { deliveryList } from "./GitDeliveryData";
import { GitDeliveryHistory } from "./RunGitDeliveries";

it("labels a saved failed unit as partial and unverified beside its delivery receipt", () => {
  const value = deliveryList(fixture.deliveries.draft, fixture.run_id);
  Object.assign(value.items[0], { verification_status: "partial_unverified" });
  const html = renderToStaticMarkup(<GitDeliveryHistory value={value} />);
  expect(html).toContain("Partial work · unverified");
  expect(html).toContain("The implementation remains failed");
  expect(html).not.toContain("Review marked ready");
});

it("shows actual commit and tree identity while keeping a draft distinct from final readiness", () => {
  const html = renderToStaticMarkup(
    <GitDeliveryHistory
      value={deliveryList(fixture.deliveries.draft, fixture.run_id)}
    />,
  );
  expect(html).toContain("Draft review updated");
  expect(html).toContain("b".repeat(40));
  expect(html).toContain("c".repeat(40));
  expect(html).toContain("7 changed files");
  expect(html).toContain(
    "Final checks, approval and run completion are recorded separately",
  );
  expect(html).not.toContain("Review marked ready");
  expect(html).not.toContain("checkpoint_digest");
});

it("does not label an ongoing push as a completed draft update", () => {
  const html = renderToStaticMarkup(
    <GitDeliveryHistory
      value={deliveryList(fixture.deliveries.publishing, fixture.run_id)}
    />,
  );
  expect(html).toContain("Remote delivery in progress");
  expect(html).toContain("Inspect remote review");
  expect(html).not.toContain("Open draft review");
  expect(html).not.toContain("Draft review updated");
  expect(html).not.toContain("Review marked ready");
});

it("keeps an unknown remote outcome held without offering any replacement effect", () => {
  const html = renderToStaticMarkup(
    <GitDeliveryHistory
      value={deliveryList(fixture.deliveries.unknown, fixture.run_id)}
    />,
  );
  expect(html).toContain("Remote outcome needs review");
  expect(html).toContain("replacement push is not sent");
  expect(html).not.toContain("<button");
  expect(html).not.toContain("Review marked ready");
});

it("does not render unsafe review links or treat untrusted branch text as markup", () => {
  for (const url of [
    "javascript:alert(1)",
    "https://user:password@example.test/review/7",
  ]) {
    const value = deliveryList(
      {
        ...fixture.deliveries.draft,
        items: [
          {
            ...fixture.deliveries.draft.items[0],
            pr_url: url,
            branch: "<script>untrusted</script>",
          },
        ],
      },
      fixture.run_id,
    );
    const html = renderToStaticMarkup(<GitDeliveryHistory value={value} />);
    expect(html).not.toContain("href=");
    expect(html).not.toContain("<script>");
    expect(html).toContain("&lt;script&gt;");
  }
});

it("marks review ready only when that state was actually returned for the checkpoint", () => {
  const html = renderToStaticMarkup(
    <GitDeliveryHistory
      value={deliveryList(fixture.deliveries.ready, fixture.run_id)}
    />,
  );
  expect(html).toContain("Review marked ready");
  expect(html).toContain(
    "Final checks, approval and run completion are recorded separately",
  );
});
