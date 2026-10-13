/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
 * https://www.apache.org/licenses/LICENSE-2.0 */
import test from "node:test";
import { modelFileName } from "./docs-scene/build-assets.mjs";
import assert from "node:assert/strict";
import { motionState } from "./docs-scene/lifecycle.mjs";

test("motion requires consent, visibility, and ordinary motion preference", () => {
  const ready = {
    playing: true,
    reduced: false,
    visible: true,
    hidden: false,
    disposed: false,
  };
  assert.equal(motionState(ready), "running");
  for (const [key, value, expected] of [
    ["playing", false, "paused"],
    ["reduced", true, "reduced"],
    ["visible", false, "hidden"],
    ["hidden", true, "hidden"],
    ["disposed", true, "disposed"],
  ])
    assert.equal(motionState({ ...ready, [key]: value }), expected);
  assert.equal(
    motionState({ ...ready, reduced: true, playing: false }),
    "reduced",
  );
});

import { spawnSync } from "node:child_process";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
test("build emits a self-contained local bundle and the actual Three MIT license", () => {
  const built = spawnSync(process.execPath, ["scripts/build-docs-scene.mjs"], {
    cwd: root,
    encoding: "utf8",
  });
  assert.equal(built.status, 0, built.stderr);
  const output = resolve(root, "build/docs-scene");
  const bundle = readFileSync(resolve(output, "scene.js"), "utf8");
  assert.ok(bundle.length > 100000);
  assert.ok(!/from ["']three["']/.test(bundle));
  const license = readFileSync(
    resolve(output, "THIRD_PARTY_NOTICES.txt"),
    "utf8",
  );
  assert.ok(license.includes("Three.js 0.186.1"));
  assert.ok(
    license.includes(
      readFileSync(resolve(root, "studio/node_modules/three/LICENSE"), "utf8"),
    ),
  );
  assert.deepEqual(
    readFileSync(
      resolve(
        output,
        modelFileName(
          readFileSync(
            resolve(root, "assets/factory/forge-operational-model.json"),
          ),
        ),
      ),
    ),
    readFileSync(resolve(root, "assets/factory/forge-operational-model.json")),
  );
});

test("a model-only edit versions both the fetch target and resulting bundle", () => {
  assert.notEqual(
    modelFileName(Buffer.from("model-one")),
    modelFileName(Buffer.from("model-two")),
  );
  const name = modelFileName(
    readFileSync(resolve(root, "assets/factory/forge-operational-model.json")),
  );
  assert.ok(
    readFileSync(resolve(root, "build/docs-scene/scene.js"), "utf8").includes(
      name,
    ),
  );
});
