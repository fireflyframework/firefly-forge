/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { renderToStaticMarkup } from "react-dom/server";
import { expect, it } from "vitest";
import { FrameworkReleaseRow } from "./KnowledgeFrameworks";
import {
  corpus,
  counts,
  discovery,
} from "../tests/framework-knowledge-fixture";

it("keeps current indexed counts separate from the target release progress", () => {
  const html = renderToStaticMarkup(
    <FrameworkReleaseRow
      value={{
        ...discovery.status,
        current_corpus: {
          ...corpus,
          framework_version: "26.10.6",
          release_tag: "v26.10.6",
        },
        counts: { ...counts, documents: 7, files: 7 },
        latest_stable: discovery.release,
        state: "refreshing",
        active_operation: {
          request_id: "3".repeat(32),
          framework: "pyfly",
          corpus,
          state: "running",
          stage: "submitting",
          counts: { ...counts, files: 45, total_files: 202, documents: 31 },
          spent_budget: { requests: 5, bytes: 1234, seconds: 2 },
          remaining_budget: { requests: 20, bytes: 8000, seconds: 60 },
          sanitized_error: null,
          uncertain_sources: 0,
          allowed_actions: [],
          revision: "4".repeat(64),
        },
      }}
      busy={false}
      onCheck={() => {}}
      onBrowse={() => {}}
      onSearch={() => {}}
    />,
  );
  expect(html).toContain("Indexed release");
  expect(html).toContain("26.10.6");
  expect(html).toContain("Latest stable release");
  expect(html).toContain("26.10.11");
  expect(html).toContain("7 documents");
  expect(html).toContain("31 documents");
  expect(html).toContain("Refresh target");
  expect(html).not.toContain("100%");
});

it("does not label an unindexed available release ready or offer invented budget controls", () => {
  const html = renderToStaticMarkup(
    <FrameworkReleaseRow
      value={{
        ...discovery.status,
        latest_stable: discovery.release,
        allowed_actions: ["check_updates", "renew_budget", "cancel"],
      }}
      busy={false}
      onCheck={() => {}}
      onBrowse={() => {}}
      onSearch={() => {}}
    />,
  );
  expect(html).toContain("No verified corpus");
  expect(html).toContain("Check updates");
  expect(html).not.toContain(">Search<");
  expect(html).not.toContain("Renew budget");
  expect(html).not.toContain(">Cancel<");
});

it("keeps an uncertain refresh held and exposes only the original operation readback", () => {
  const html = renderToStaticMarkup(
    <FrameworkReleaseRow
      value={discovery.status}
      busy={false}
      pendingRequest={"5".repeat(32)}
      onRead={() => {}}
      onCheck={() => {}}
      onBrowse={() => {}}
      onSearch={() => {}}
    />,
  );
  expect(html).toContain("Unconfirmed refresh response");
  expect(html).toContain("Read original operation");
  expect(html).toContain("5".repeat(32));
  expect(html).not.toContain("Start refresh");
});

it("does not claim latest parity without a checked version and matching immutable commit", () => {
  const value = {
    ...discovery.status,
    current_corpus: corpus,
    state: "current" as const,
  };
  const render = (latest: typeof discovery.release | null) =>
    renderToStaticMarkup(
      <FrameworkReleaseRow
        value={{ ...value, latest_stable: latest }}
        busy={false}
        onCheck={() => {}}
        onBrowse={() => {}}
        onSearch={() => {}}
      />,
    );
  expect(render(null)).toContain("Verified corpus available");
  expect(render({ ...discovery.release, commit: "8".repeat(40) })).toContain(
    "Release reference differs from latest check",
  );
  expect(render(discovery.release)).toContain(
    "Indexed release matches latest check",
  );
});

it("keeps activated defaults distinct from indexed references and discovery backoff", () => {
  const html = renderToStaticMarkup(
    <FrameworkReleaseRow
      value={{
        ...discovery.status,
        current_corpus: {
          ...corpus,
          corpus_ref: "7".repeat(32),
          framework_version: "26.10.6",
          release_tag: "v26.10.6",
          commit: "8".repeat(40),
        },
        counts: { ...counts, files: 7, total_files: 7, documents: 7 },
        generation_target: {
          version: 1,
          request_id: "7".repeat(32),
          framework: "pyfly",
          corpus,
          validation_run_id: "6".repeat(32),
          validation_source_tree: "9".repeat(40),
          validation_digest: "a".repeat(64),
          activation_actor_digest: "b".repeat(64),
          activated_at: "2026-10-26T00:00:00Z",
          revision: "c".repeat(64),
        },
        discovery_retry_at: "2026-10-26T10:00:00Z",
        discovery_error: "Official release service temporarily unavailable",
      }}
      busy={false}
      onCheck={() => {}}
      onBrowse={() => {}}
      onSearch={() => {}}
    />,
  );
  expect(html).toContain("Activated generation default");
  expect(html).toContain("26.10.6");
  expect(html).toContain("26.10.11");
  expect(html).toContain("Native validation run");
  expect(html).toContain("6".repeat(32));
  expect(html).toContain("Next update check");
  expect(html).toContain("Official release service temporarily unavailable");
  expect(html).toContain('title="2026-10-26T10:00:00Z"');
});
