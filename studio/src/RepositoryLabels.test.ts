/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { describe, expect, it } from "vitest";
import { repositoryLabels } from "./RepositoryLabels";
const repository = (id: string, url: string, name = "Inventory") => ({
  id,
  name,
  url,
  provider: "git",
  default_branch: "main",
});
describe("repository option labels", () => {
  it("leaves a unique name short and disambiguates duplicates using safe source and branch", () => {
    const labels = repositoryLabels([
      repository(
        "a".repeat(32),
        "https://alice:private@example.com/team/api?token=private#private",
      ),
      repository("b".repeat(32), "git@example.com:team/web.git"),
      repository(
        "c".repeat(32),
        "https://example.com/team/unique",
        "Another product",
      ),
    ]);
    expect(labels.get("a".repeat(32))).toBe(
      "Inventory · example.com/team/api · git · main",
    );
    expect(labels.get("b".repeat(32))).toBe(
      "Inventory · example.com/team/web · git · main",
    );
    expect(labels.get("c".repeat(32))).toBe("Another product");
    expect([...labels.values()].join(" ")).not.toMatch(/private|alice/);
  });
  it("uses a local basename and sufficiently unique identifiers for the same source", () => {
    const records = [
      repository(
        "12345678aaaa" + "a".repeat(20),
        "file:///private/work/stockroom",
      ),
      repository(
        "12345678bbbb" + "b".repeat(20),
        "file:///private/work/stockroom",
      ),
    ];
    const values = [...repositoryLabels(records).values()];
    expect(values[0]).toContain("stockroom");
    expect(values[0]).not.toContain("private/work");
    expect(values[0]).not.toBe(values[1]);
  });
});
