/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { describe, expect, it } from "vitest";
import {
  connectionPayload,
  githubRepository,
  grantPayload,
  readConnector,
  readGrants,
  toolFields,
} from "./ExternalToolsSettings";
const connector = {
  id: "github",
  name: "GitHub",
  endpoint: "https://api.githubcopilot.com/mcp/readonly",
  tools: ["get_file_contents"],
  prerequisite: null,
  enabled: true,
  credential_configured: true,
  readiness: "project_grant_required",
  live_tool_verified: false,
  discovery: {
    digest: "d".repeat(64),
    credential_revision: "e".repeat(32),
    tools: {
      get_file_contents: {
        type: "object",
        properties: { owner: { type: "string" }, repo: { type: "string" } },
        required: ["owner", "repo"],
      },
    },
  },
  review: { digest: "d".repeat(64), tools: ["get_file_contents"] },
};
const project = {
  id: "a".repeat(32),
  definition: {
    name: "Stock",
    components: [{ repository_id: "b".repeat(32) }],
  },
};
const repo = {
  id: "b".repeat(32),
  name: "Stock API",
  url: "https://github.com/team/product.git",
};
describe("external tools preserve actual API authority", () => {
  it("matches the backend's exact registered GitHub authority and unnormalized path constraints", () => {
    for (const url of [
      "https://github.com:443/team/product.git",
      "https://GITHUB.COM/team/product.git",
      "https://github.com/team/./product.git",
      "https://github.com/team/%2e/product.git",
    ])
      expect(githubRepository({ ...repo, url })).toBe(false);
    expect(githubRepository(repo)).toBe(true);
  });
  it("omits a blank saved key and requires private access before enabling", () => {
    expect(
      connectionPayload(readConnector(connector), true, "", false),
    ).toEqual({ enabled: true });
    expect(() =>
      connectionPayload(
        readConnector({ ...connector, credential_configured: false }),
        true,
        "",
        false,
      ),
    ).toThrow(/key/);
    expect(() =>
      connectionPayload(
        readConnector(connector),
        true,
        "key with spaces",
        false,
      ),
    ).toThrow(/spaces/);
    expect(
      connectionPayload(readConnector(connector), false, "", true),
    ).toEqual({ enabled: false, clear_secret: true });
  });
  it("pins exact reviewed tools and project repository without mutating API records", () => {
    const before = JSON.stringify(connector);
    expect(
      grantPayload(
        readConnector(connector),
        project,
        repo,
        ["get_file_contents"],
        ["implementer"],
        [],
      ),
    ).toEqual({
      repository_id: repo.id,
      review_digest: "d".repeat(64),
      tools: ["get_file_contents"],
      roles: ["implementer"],
      stages: ["implement"],
    });
    expect(JSON.stringify(connector)).toBe(before);
    expect(() =>
      grantPayload(
        readConnector(connector),
        project,
        { ...repo, id: "c".repeat(32) },
        ["get_file_contents"],
        ["implementer"],
        [],
      ),
    ).toThrow(/project/);
    expect(() =>
      grantPayload(
        readConnector(connector),
        project,
        { ...repo, url: "https://github.com.evil/team/product" },
        ["get_file_contents"],
        ["implementer"],
        [],
      ),
    ).toThrow(/GitHub/);
  });
  it("rejects unreviewed tools and ambiguous overlapping active grants", () => {
    expect(() =>
      grantPayload(
        readConnector(connector),
        project,
        repo,
        ["delete_repository"],
        ["implementer"],
        [],
      ),
    ).toThrow(/reviewed/);
    const grants = readGrants(
      [
        {
          id: "c".repeat(32),
          active: true,
          credential_revision: "e".repeat(32),
          review_digest: "d".repeat(64),
          project_id: project.id,
          repository_id: repo.id,
          connector: "github",
          tools: connector.discovery.tools,
          roles: ["implementer"],
          stages: ["implement"],
          scope: { owner: "team", repo: "product" },
        },
      ],
      project.id,
    );
    expect(() =>
      grantPayload(
        readConnector(connector),
        project,
        repo,
        ["get_file_contents"],
        ["implementer"],
        grants,
      ),
    ).toThrow(/Revoke/);
    expect(() =>
      grantPayload(
        readConnector(connector),
        project,
        repo,
        ["get_file_contents"],
        ["docs"],
        grants,
      ),
    ).toThrow(/Revoke/);
    const separateTool = readConnector({
      ...connector,
      discovery: {
        ...connector.discovery,
        tools: {
          ...connector.discovery.tools,
          get_commit: connector.discovery.tools.get_file_contents,
        },
      },
      review: {
        ...connector.review,
        tools: ["get_file_contents", "get_commit"],
      },
    });
    expect(
      grantPayload(
        separateTool,
        project,
        repo,
        ["get_commit"],
        ["docs"],
        grants,
      ).tools,
    ).toEqual(["get_commit"]);
  });
  it("fails closed on foreign grants, malformed digests and unsupported connector state", () => {
    expect(() =>
      readConnector({
        ...connector,
        review: { ...connector.review, digest: "0".repeat(64) },
      }),
    ).toThrow(/review/);
    expect(() =>
      readConnector({ ...connector, readiness: "verified_by_model" }),
    ).toThrow(/unsupported/i);
    expect(() =>
      readGrants(
        [{ id: "c".repeat(32), project_id: "c".repeat(32) }],
        project.id,
      ),
    ).toThrow();
  });
  it("preserves grants for queued runs on an earlier key revision when granting a rotated connection", () => {
    const old = readGrants(
      [
        {
          id: "c".repeat(32),
          active: true,
          project_id: project.id,
          repository_id: repo.id,
          connector: "github",
          credential_revision: "f".repeat(32),
          review_digest: "0".repeat(64),
          tools: connector.discovery.tools,
          roles: ["implementer"],
          stages: ["implement"],
          scope: { owner: "team", repo: "product" },
        },
      ],
      project.id,
    );
    expect(
      grantPayload(
        readConnector(connector),
        project,
        repo,
        ["get_file_contents"],
        ["implementer"],
        old,
      ).review_digest,
    ).toBe(connector.review.digest);
  });
  it("bounds remote schema labels without executing or treating descriptions as authority", () => {
    const schema = {
      properties: {
        ["x".repeat(30000)]: {
          type: "string",
          description: "<script>globalThis.attacked=1</script>",
        },
      },
      required: [],
    };
    const fields = toolFields(schema);
    expect(fields[0].name.length).toBeLessThan(130);
    expect(fields[0].type).toBe("string");
    expect(fields[0].required).toBe(false);
    expect("attacked" in globalThis).toBe(false);
  });
});
