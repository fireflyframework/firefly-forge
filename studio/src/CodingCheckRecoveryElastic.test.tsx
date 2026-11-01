// @vitest-environment happy-dom
/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import fixture from "../tests/coding-check-grant-fixture.json";
import { request, type ForgeRun } from "./api";
import { CodingCheckRecovery } from "./CodingCheckRecovery";
import type { IdentitySession } from "./Identity";

vi.mock("./api", async (original) => ({
  ...(await original<typeof import("./api")>()),
  request: vi.fn(),
}));
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

it("offers the actual remaining interactive capacity for a large reviewed suite without sending a grant", async () => {
  vi.stubGlobal("localStorage", { getItem: () => null, setItem: vi.fn() });
  vi.spyOn(HTMLDialogElement.prototype, "showModal").mockImplementation(
    function (this: HTMLDialogElement) {
      this.setAttribute("open", "");
    },
  );
  vi.spyOn(HTMLDialogElement.prototype, "close").mockImplementation(function (
    this: HTMLDialogElement,
  ) {
    this.removeAttribute("open");
  });
  vi.mocked(request).mockImplementation(async (_connection, path, options) => {
    expect(options?.method ?? "GET").toBe("GET");
    if (path.endsWith("/current"))
      return {
        protocol: "native-coding-configured-check-v1",
        run_id: "c".repeat(32),
        grant_id: null,
        revision: 0,
        status: "none",
        receipt_digest: null,
        expires_at_ms: null,
      } as never;
    const query = new URL(path, "https://factory.example").searchParams;
    const candidate = structuredClone(fixture.candidate);
    candidate.commands = Array.from({ length: 51 }, (_, command_index) => ({
      command_index,
      argv: ["check", String(command_index)],
      timeout: 30,
    }));
    candidate.command_indices = candidate.commands.map(
      (command) => command.command_index,
    );
    Object.assign(candidate, {
      recovery_request_id: query.get("recovery_request_id"),
      command_count: 51,
      independent_final_check_count: 51,
      shared_command_limit: 100,
      shared_commands_used: 0,
      shared_commands_remaining: 100,
      interactive_commands_remaining: 49,
    });
    candidate.channel.bind_port = 0;
    candidate.runner.configuration.command_limit = 100;
    return candidate as never;
  });
  render(
    <CodingCheckRecovery
      run={
        {
          id: "c".repeat(32),
          repository_id: "b".repeat(32),
          attempt: 4,
          active_task: "implement",
          status: "failed",
          agent: "codex",
          brief: "Controlled large suite",
        } as ForgeRun
      }
      session={
        {
          global_admin: true,
          user: { id: "a".repeat(32), authentication_method: "session" },
        } as IdentitySession
      }
      connection={{
        base: "https://factory.example/api/v1",
        token: "fixture-only",
      }}
    />,
  );
  const prepare = await screen.findByRole("button", {
    name: "Prepare recovery with configured checks",
  });
  await waitFor(() => expect(prepare.hasAttribute("disabled")).toBe(false));
  fireEvent.click(prepare);
  await screen.findByText(/Check 51 · Timeout/);
  fireEvent.click(screen.getByRole("button", { name: "Continue" }));
  const calls = (await screen.findByRole("spinbutton", {
    name: "Maximum check calls",
  })) as HTMLInputElement;
  expect(calls.max).toBe("49");
  expect(calls.value).toBe("49");
  expect(screen.getByText(/Up to 49 interactive calls/)).toBeTruthy();
  expect(vi.mocked(request).mock.calls).toHaveLength(2);
});
