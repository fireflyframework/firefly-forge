/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { GitHubProjectsWizard, type BoardDraft } from "./GitHubProjectsWizard";
import {
  boardMappingStates,
  type BoardAccount,
  type BoardDiscovery,
} from "./GitHubProjectsData";
it("names board statuses plainly and keeps seven effective choices and consent explicit", () => {
  const account = {
    connection_name: "delivery",
    viewer: { id: "U_1", login: "octocat" },
    repository: { id: "R_1", name_with_owner: "team/product", private: true },
  } as BoardAccount;
  const discovery = {
    board: { title: "Delivery" },
    fields: [
      {
        id: "STATUS",
        name: "Status",
        options: [{ id: "REVIEW", name: "In review" }],
      },
    ],
  } as BoardDiscovery;
  const draft: BoardDraft = {
    action: "connect",
    kind: "user",
    owner: "octocat",
    number: "9",
    title: "Delivery",
    createAck: false,
    field: "STATUS",
    options: {},
    enabled: true,
    includeExisting: false,
    manageAck: false,
  };
  const props = {
    step: 2,
    onStep: () => {},
    busy: false,
    choices: [],
    name: "delivery",
    onName: () => {},
    account,
    discovery,
    onDraft: () => {},
    onVerify: () => {},
    onDiscover: () => {},
    onCreate: () => {},
    onSave: () => {},
    creationHeld: false,
    configurationHeld: false,
    error: "",
  };
  const initial = renderToStaticMarkup(
    <GitHubProjectsWizard {...props} draft={draft} />,
  );
  expect(initial).toContain("Board statuses");
  expect(initial).toContain("Map board statuses");
  expect(initial).toContain("forge run status");
  expect(initial).toContain("GitHub board status");
  expect(initial).toContain("GitHub board field");
  expect(initial).not.toContain('id="board-state-succeeded"');
  expect(initial).toContain(
    "Also used when a run finishes successfully and awaits product review.",
  );
  expect(initial).toContain('aria-describedby="board-field-context"');
  expect(initial).toContain('id="board-field-context"');
  expect(initial).toContain("Private board");
  expect(initial).toContain(
    "Allow forge to update only this field for linked issues and pull requests.",
  );
  expect(initial).toContain("0 of 7 mapped");
  expect(initial).not.toContain(
    "Scroll for all choices and final confirmation.",
  );
  expect(initial).toMatch(/disabled="">Save board settings/);
  const complete = renderToStaticMarkup(
    <GitHubProjectsWizard
      {...props}
      draft={{
        ...draft,
        options: Object.fromEntries(
          boardMappingStates.map((state) => [state, "REVIEW"]),
        ),
      }}
    />,
  );
  expect(complete).toContain("7 of 7 mapped");
  expect(complete).toMatch(/disabled="">Save board settings/);
});
