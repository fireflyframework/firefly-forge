/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
 * https://www.apache.org/licenses/LICENSE-2.0 */
import {
  readFileSync,
  readdirSync,
  mkdtempSync,
  writeFileSync,
  mkdirSync,
  statSync,
} from "node:fs";
import { join, resolve, sep, extname } from "node:path";
import { createServer } from "node:http";
import { tmpdir } from "node:os";
import { spawnSync } from "node:child_process";

export function mermaidPages(directory, prefix = "") {
  return readdirSync(directory, { withFileTypes: true }).flatMap((entry) => {
    const name = join(prefix, entry.name);
    if (entry.isDirectory())
      return mermaidPages(join(directory, entry.name), name);
    if (!entry.name.endsWith(".md")) return [];
    const count = [
      ...readFileSync(join(directory, entry.name), "utf8").matchAll(
        /^[ \t]*(?:`{3,}|~{3,})[ \t]*mermaid[ \t]*$/gm,
      ),
    ].length;
    return count
      ? [
          {
            path:
              name.replace(/(^|\/)index\.md$/, "$1").replace(/\.md$/, "/") ||
              "/",
            count,
          },
        ]
      : [];
  });
}

export async function buildRuntimeFixture(root) {
  const directory = mkdtempSync(join(tmpdir(), "docs-mermaid-runtime-"));
  const output = join(directory, "site");
  const server = createServer((request, response) => {
    try {
      let target = resolve(
        output,
        "." +
          decodeURIComponent(new URL(request.url, "http://127.0.0.1").pathname),
      );
      if (!target.startsWith(output + sep) && target !== output)
        throw new Error("Outside fixture");
      if (statSync(target).isDirectory()) target = join(target, "index.html");
      const types = {
        ".html": "text/html",
        ".js": "application/javascript",
        ".css": "text/css",
        ".json": "application/json",
        ".svg": "image/svg+xml",
        ".xml": "application/xml",
      };
      response.setHeader(
        "Content-Type",
        types[extname(target)] ?? "application/octet-stream",
      );
      response.end(readFileSync(target));
    } catch {
      response.writeHead(404);
      response.end();
    }
  });
  await new Promise((done) => server.listen(0, "127.0.0.1", done));
  const baseURL = `http://127.0.0.1:${server.address().port}/`;
  const close = () => {
    server.closeAllConnections();
    server.close();
  };
  const docs = join(directory, "docs");
  mkdirSync(docs);
  writeFileSync(
    join(docs, "index.md"),
    "# Diagram runtime fixture\n\n[Next diagram](next.md)\n\n```mermaid\nflowchart TD\n  Source[Owned source] --> Receipt[Verified receipt]\n```\n",
  );
  writeFileSync(
    join(docs, "next.md"),
    "# Second diagram\n\n[First diagram](index.md)\n\n```mermaid\nsequenceDiagram\n  Caller->>Worker: Execute approved work\n  Worker-->>Caller: Retain receipt\n```\n",
  );
  const config = join(directory, "mkdocs.yml");
  writeFileSync(
    config,
    `INHERIT: ${JSON.stringify(join(root, "mkdocs.yml"))}\ndocs_dir: ${JSON.stringify(docs)}\nsite_dir: ${JSON.stringify(output)}\nsite_url: ${JSON.stringify(baseURL)}\nhooks: []\nextra_css: []\nextra_javascript: []\nnav:\n  - First: index.md\n  - Second: next.md\ntheme:\n  name: material\n  font: false\n  logo: null\n  favicon: assets/images/favicon.png\n  features:\n    - navigation.instant\n    - navigation.instant.prefetch\n`,
  );
  const built = spawnSync(
    process.env.BRIDGE_PYTHON ?? "python3",
    ["-m", "mkdocs", "build", "--strict", "-f", config],
    { cwd: root, encoding: "utf8" },
  );
  if (built.status !== 0) {
    close();
    throw new Error(built.stderr || built.stdout);
  }
  return { baseURL, close };
}

export async function renderedDiagrams(page) {
  const session = await page.context().newCDPSession(page);
  try {
    const { root } = await session.send("DOM.getDocument", {
      depth: -1,
      pierce: true,
    });
    const diagrams = [];
    async function visit(node, diagram = false) {
      const attributes = node.attributes ?? [];
      const classes = attributes[attributes.indexOf("class") + 1] ?? "";
      const inside = diagram || classes.split(" ").includes("mermaid");
      if (inside && node.localName === "svg") {
        const { object } = await session.send("DOM.resolveNode", {
          nodeId: node.nodeId,
        });
        const { result } = await session.send("Runtime.callFunctionOn", {
          objectId: object.objectId,
          functionDeclaration:
            "function() { const box=this.getBoundingClientRect(); const shape=this.querySelector('.node rect, .actor'); return {html:this.outerHTML, width:box.width, height:box.height, fill:shape ? getComputedStyle(shape).fill : null, error:Boolean(this.querySelector('.error-icon, .error-text')), labelPixels:[...this.querySelectorAll('.nodeLabel')].map(label => { const transform=(label.closest('foreignObject') ?? label).getScreenCTM(); return parseFloat(getComputedStyle(label).fontSize) * Math.hypot(transform.a, transform.b); })}; }",
          returnByValue: true,
        });
        diagrams.push(result.value);
      }
      for (const child of [
        ...(node.children ?? []),
        ...(node.shadowRoots ?? []),
      ])
        await visit(child, inside);
    }
    await visit(root);
    return diagrams;
  } catch (error) {
    // Instant navigation can detach a node while its CDP snapshot is read.
    if (
      error.message.includes("DOM.resolveNode") &&
      error.message.includes("No node with given id found")
    )
      return [];
    throw error;
  } finally {
    await session.detach();
  }
}
