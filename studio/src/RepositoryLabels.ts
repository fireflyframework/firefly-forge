/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import type { Repository } from "./api";
function sourceLabel(value: string) {
  try {
    const url = new URL(value);
    if (url.protocol === "file:")
      return (
        url.pathname.split("/").filter(Boolean).at(-1) || "Repository root"
      );
    if (["https:", "http:", "ssh:", "git:"].includes(url.protocol))
      return `${url.host}${url.pathname.replace(/\.git\/?$/, "").replace(/\/$/, "")}`;
  } catch {
    /* Git also accepts local paths and SSH's user@host:path notation. */
  }
  const ssh = value.match(/^[^@/\s]+@([^:/?#\s]+):([^?#\s]+)/);
  if (ssh) return `${ssh[1]}/${ssh[2].replace(/\.git$/, "")}`;
  if (!value.includes("://") && !/[?#@]/.test(value))
    return value.split(/[\\/]/).filter(Boolean).at(-1) || "Repository root";
  return "Repository";
}
export function repositoryLabels(repositories: Repository[]) {
  const result = new Map<string, string>();
  for (const repository of repositories) {
    const peers = repositories.filter(
      (item) =>
        item.name.trim().toLocaleLowerCase() ===
        repository.name.trim().toLocaleLowerCase(),
    );
    if (peers.length === 1) {
      result.set(repository.id, repository.name);
      continue;
    }
    const describe = (item: Repository) =>
      `${item.name} · ${sourceLabel(item.url)} · ${item.provider} · ${item.default_branch}`;
    const label = describe(repository),
      identical = peers.filter((item) => describe(item) === label);
    let suffix = "";
    if (identical.length > 1) {
      let length = 8;
      const maximum = Math.max(...identical.map((item) => item.id.length));
      while (
        length < maximum &&
        identical.some(
          (item) =>
            item.id !== repository.id &&
            item.id.slice(0, length) === repository.id.slice(0, length),
        )
      )
        length += 4;
      suffix = ` · ${repository.id.slice(0, length)}`;
    }
    result.set(repository.id, label + suffix);
  }
  return result;
}
