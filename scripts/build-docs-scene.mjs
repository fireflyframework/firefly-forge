/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
 * https://www.apache.org/licenses/LICENSE-2.0 */
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";
import { resolve, dirname, join } from "node:path";
import { mkdirSync, cpSync, readFileSync, writeFileSync } from "node:fs";
import { modelFileName } from "./docs-scene/build-assets.mjs";
const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const require = createRequire(join(root, "studio/package.json"));
const { build } = require("esbuild");
const output = join(root, "build/docs-scene");
mkdirSync(output, { recursive: true });
const model = readFileSync(
  join(root, "assets/factory/forge-operational-model.json"),
);
const modelName = modelFileName(model);
await build({
  define: { FORGE_DOC_MODEL_URL: JSON.stringify(modelName) },
  entryPoints: [join(root, "scripts/docs-scene/scene.mjs")],
  outfile: join(output, "scene.js"),
  bundle: true,
  format: "iife",
  target: "es2022",
  minify: true,
  legalComments: "eof",
  nodePaths: [join(root, "studio/node_modules")],
});
cpSync(join(root, "scripts/docs-scene/scene.css"), join(output, "scene.css"));
const three = join(root, "studio/node_modules/three");
if (
  JSON.parse(readFileSync(join(three, "package.json"), "utf8")).version !==
  "0.186.1"
)
  throw new Error("Documentation requires the pinned Three.js version");
writeFileSync(
  join(output, "THIRD_PARTY_NOTICES.txt"),
  "Documentation illustration · Three.js 0.186.1\n\n" +
    readFileSync(join(three, "LICENSE"), "utf8") +
    "\n\n" +
    readFileSync(join(root, "assets/brand/NOTICE"), "utf8"),
);
writeFileSync(join(output, modelName), model);
cpSync(
  join(root, "assets/factory/operational-model-provenance.json"),
  join(output, "operational-model-provenance.json"),
);

writeFileSync(
  join(output, "manifest.json"),
  JSON.stringify({
    files: [
      "scene.js",
      "scene.css",
      "THIRD_PARTY_NOTICES.txt",
      modelName,
      "operational-model-provenance.json",
    ],
  }),
);
