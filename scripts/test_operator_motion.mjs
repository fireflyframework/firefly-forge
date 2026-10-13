/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
 * https://www.apache.org/licenses/LICENSE-2.0 */
import test from "node:test";
import assert from "node:assert/strict";
import { operatorPose, legAngles } from "./docs-scene/operator-motion.mjs";

test("operators start seated and alternate a complete visit every 32 seconds", () => {
  for (const index of [0, 1])
    assert.equal(operatorPose(0, index).mode, "seated");
  const stages = [
    operatorPose(16.5, 0),
    operatorPose(18, 0),
    operatorPose(21, 0),
    operatorPose(25, 0),
    operatorPose(27.5, 0),
  ];
  assert.deepEqual(
    stages.map((p) => p.mode),
    ["rising", "walking", "checking", "returning", "sitting"],
  );
  for (let t = 0; t < 128; t += 0.25) {
    const first = operatorPose(t, 0),
      second = operatorPose(t, 1);
    assert.ok(first.mode === "seated" || second.mode === "seated");
    for (const pose of [first, second]) {
      assert.ok(pose.travel >= 0 && pose.travel <= 1);
      assert.ok(pose.standing >= 0 && pose.standing <= 1);
      if (pose.mode !== "seated") assert.equal(pose.coffee, 0);
    }
  }
  assert.equal(operatorPose(50, 1).mode, "walking");
  assert.equal(operatorPose(28, 0).mode, "seated");
  assert.equal(operatorPose(64, 0).mode, "seated");
  assert.ok(operatorPose(2, 1).coffee > 0);
});

test("two joint legs reach seated and stepping foot targets without sinking below the floor", () => {
  for (const hip of [0.56, 0.85])
    for (const footY of [0.035, 0.11])
      for (const footZ of hip === 0.56
        ? [-0.29, -0.12, 0.12]
        : [-0.16, 0, 0.16]) {
        const { hipAngle, kneeAngle } = legAngles(hip, footY, footZ);
        const y =
          hip -
          0.4 * Math.cos(hipAngle) -
          0.44 * Math.cos(hipAngle + kneeAngle);
        const z =
          -0.4 * Math.sin(hipAngle) - 0.44 * Math.sin(hipAngle + kneeAngle);
        assert.ok(Math.abs(y - footY) < 0.005);
        assert.ok(Math.abs(z - footZ) < 0.005);
        assert.ok(y >= 0.03);
      }
});

import { createRequire } from "node:module";
const require = createRequire(
  new URL("../studio/package.json", import.meta.url),
);
const THREE = await import(
  new URL("../studio/node_modules/three/build/three.module.js", import.meta.url)
);
const { build } = require("esbuild");
test("the actual rig seats on the bay, sips coffee, walks with planted heels and returns", async () => {
  const bundle = await build({
    entryPoints: ["scripts/docs-scene/operators.mjs"],
    bundle: true,
    platform: "node",
    format: "esm",
    minify: true,
    write: false,
    nodePaths: ["studio/node_modules"],
  });
  const { addCafeteria } = await import(
    "data:text/javascript;base64," +
      Buffer.from(bundle.outputFiles[0].text).toString("base64")
  );
  const world = new THREE.Group();
  const render = addCafeteria(world, [
    { x: -12.3 },
    { x: -8.2 },
    { x: -4.1 },
    { x: 0 },
  ]);
  const person = world.getObjectByName("operator_0");
  const floor = new THREE.Box3().setFromObject(
    world.getObjectByName("cafeteria_floor"),
  ).max.y;
  const head = person.getObjectByName("head"),
    cup = person.getObjectByName("hand_cup");
  const distance = () =>
    head
      .getWorldPosition(new THREE.Vector3())
      .distanceTo(cup.getWorldPosition(new THREE.Vector3()));
  render(0, false);
  world.updateMatrixWorld(true);
  const rest = distance();
  assert.ok(person.getObjectByName("knee_-1").rotation.x < -0.8);
  render(2.6, false);
  world.updateMatrixWorld(true);
  assert.ok(distance() < rest * 0.65);
  for (const time of [17.1, 17.5, 18, 18.5, 19, 25, 26]) {
    render(time, false);
    world.updateMatrixWorld(true);
    const feet = [-1, 1].map(
      (side) =>
        new THREE.Box3().setFromObject(person.getObjectByName(`foot_${side}`))
          .min.y,
    );
    assert.ok(Math.min(...feet) >= floor - 0.002);
    assert.ok(Math.abs(Math.min(...feet) - floor) < 0.005);
  }
  render(28, false);
  world.updateMatrixWorld(true);
  assert.ok(person.getObjectByName("knee_-1").rotation.x < -0.8);
  assert.ok(Math.abs(person.position.x - -6.12) < 0.005);
});

test("cups clear the tabletop and both operators avoid the table along the entire loop", async () => {
  const bundle = await build({
    entryPoints: ["scripts/docs-scene/operators.mjs"],
    bundle: true,
    platform: "node",
    format: "esm",
    minify: true,
    write: false,
    nodePaths: ["studio/node_modules"],
  });
  const { addCafeteria } = await import(
    "data:text/javascript;base64," +
      Buffer.from(bundle.outputFiles[0].text).toString("base64")
  );
  const world = new THREE.Group();
  const render = addCafeteria(world, [
    { id: "prepare", x: -12.3 },
    { id: "implement", x: -8.2 },
    { id: "verify", x: -4.1 },
    { id: "checkpoint", x: 0 },
  ]);
  world.updateMatrixWorld(true);
  const table = new THREE.Box3().setFromObject(
    world.getObjectByName("cafeteria_table"),
  );
  for (let t = 0; t < 64; t += 0.05) {
    render(t, false);
    world.updateMatrixWorld(true);
    for (const index of [0, 1]) {
      const person = world.getObjectByName(`operator_${index}`),
        cup = person.getObjectByName("hand_cup");
      const body = person.getWorldPosition(new THREE.Vector3());
      assert.ok(
        !(
          body.x > table.min.x - 0.18 &&
          body.x < table.max.x + 0.18 &&
          body.z > table.min.z - 0.18 &&
          body.z < table.max.z + 0.18
        ),
        `body/table collision ${index} at ${t}`,
      );
      if (cup.visible) {
        const box = new THREE.Box3().setFromObject(cup);
        if (
          box.max.x > table.min.x &&
          box.min.x < table.max.x &&
          box.max.z > table.min.z &&
          box.min.z < table.max.z
        )
          assert.ok(
            box.min.y >= table.max.y - 0.002,
            `cup/table collision ${index} at ${t}: ${box.min.y} < ${table.max.y}`,
          );
      }
    }
  }
});
