/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
 * https://www.apache.org/licenses/LICENSE-2.0 */
import test from "node:test";
import assert from "node:assert/strict";
import {
  factoryFrame,
  applyFactoryFrame,
} from "./docs-scene/factory-sequence.mjs";
const layout = [
  "prepare",
  "implement",
  "verify",
  "checkpoint",
  "publish",
  "deploy",
  "health",
].map((id, i) => ({ id, x: -12.3 + i * 4.1 }));
test("machines work only while the document dwells at their station", () => {
  for (let time = 0; time < 14; time += 0.01) {
    const frame = factoryFrame(time, layout);
    if (frame.mode === "dwell") {
      assert.equal(frame.activeStation, layout[frame.stationIndex].id);
      assert.ok(
        Math.abs(frame.carrierX - layout[frame.stationIndex].x) < 0.0001,
      );
      assert.ok(frame.dwellFraction >= 0 && frame.dwellFraction <= 1);
    } else assert.equal(frame.activeStation, null);
  }
  assert.equal(factoryFrame(1.6, layout).mode, "transit");
  assert.ok(factoryFrame(1.6, layout).carrierX > layout[0].x);
  assert.ok(factoryFrame(1.6, layout).carrierX < layout[1].x);
  assert.equal(factoryFrame(2.3, layout).activeStation, "implement");
});
test("the loop conceals its reset and resumes the same initial pose", () => {
  const before = factoryFrame(13.599999, layout),
    after = factoryFrame(13.600001, layout);
  assert.equal(before.activeStation, null);
  assert.equal(after.activeStation, null);
  assert.ok(before.opacity < 0.001 && after.opacity < 0.001);
  assert.equal(before.carrierX, layout.at(-1).x);
  assert.equal(after.carrierX, layout[0].x);
  assert.deepEqual(factoryFrame(14, layout), factoryFrame(0, layout));
});

const THREE = await import(
  new URL("../studio/node_modules/three/build/three.module.js", import.meta.url)
);
test("actual Three keyframes stay idle in transit and sample only the dwelling machine", () => {
  const world = new THREE.Group();
  const mixer = new THREE.AnimationMixer(world),
    actions = new Map();
  for (const id of ["prepare", "implement"]) {
    const group = new THREE.Group();
    group.name = id;
    world.add(group);
    const clip = new THREE.AnimationClip(id, 1, [
      new THREE.NumberKeyframeTrack(`${id}.rotation[z]`, [0, 1], [0, 1]),
    ]);
    actions.set(id, {
      action: mixer.clipAction(clip).setLoop(THREE.LoopOnce, 1),
      duration: clip.duration,
    });
  }
  applyFactoryFrame(factoryFrame(0.6, layout), actions, mixer);
  assert.ok(
    Math.abs(world.getObjectByName("prepare").rotation.z - 0.5) < 0.0001,
  );
  assert.equal(world.getObjectByName("implement").rotation.z, 0);
  applyFactoryFrame(factoryFrame(1.6, layout), actions, mixer);
  assert.equal(world.getObjectByName("prepare").rotation.z, 0);
  assert.equal(world.getObjectByName("implement").rotation.z, 0);
  applyFactoryFrame(factoryFrame(2.6, layout), actions, mixer);
  assert.equal(world.getObjectByName("prepare").rotation.z, 0);
  assert.ok(
    Math.abs(world.getObjectByName("implement").rotation.z - 0.5) < 0.0001,
  );
});
