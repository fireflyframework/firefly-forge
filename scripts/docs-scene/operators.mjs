/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
 * https://www.apache.org/licenses/LICENSE-2.0 */
import * as THREE from "three";
import { factoryFrame } from "./factory-sequence.mjs";
import { operatorPose, legAngles, armAngles } from "./operator-motion.mjs";
const wrap = (angle) => Math.atan2(Math.sin(angle), Math.cos(angle));
const turn = (from, to, share) => from + wrap(to - from) * share;
export function addCafeteria(world, layout) {
  const area = new THREE.Group();
  area.name = "documentation_cafeteria";
  world.add(area);
  const floor = new THREE.MeshStandardMaterial({
    color: 0x272820,
    roughness: 1,
  });
  const shell = new THREE.MeshStandardMaterial({
    color: 0xc5c5b6,
    roughness: 0.9,
  });
  const frame = new THREE.MeshStandardMaterial({
    color: 0x62645b,
    roughness: 0.9,
  });
  const paper = new THREE.MeshStandardMaterial({
    color: 0xf3f1eb,
    roughness: 0.9,
  });
  const coffee = new THREE.MeshStandardMaterial({
    color: 0x343025,
    roughness: 1,
  });
  const skin = new THREE.MeshStandardMaterial({
    color: 0xbcae9b,
    roughness: 0.95,
  });
  function mesh(parent, geometry, material, x = 0, y = 0, z = 0) {
    const item = new THREE.Mesh(geometry, material);
    item.position.set(x, y, z);
    parent.add(item);
    return item;
  }
  function box(parent, material, x, y, z, width, height, depth) {
    return mesh(
      parent,
      new THREE.BoxGeometry(width, height, depth),
      material,
      x,
      y,
      z,
    );
  }
  function mug(parent) {
    const cup = new THREE.Group();
    parent.add(cup);
    mesh(cup, new THREE.CylinderGeometry(0.085, 0.075, 0.15, 16), paper);
    mesh(
      cup,
      new THREE.CircleGeometry(0.071, 16),
      coffee,
      0,
      0.076,
    ).rotation.x = -Math.PI / 2;
    mesh(cup, new THREE.TorusGeometry(0.05, 0.012, 8, 16), paper, 0.09, 0);
    return cup;
  }
  // The bay joins the original plinth at z=1.85, at its exact -0.08 top.
  box(area, floor, -2.5, -0.24, 3.65, 9.2, 0.32, 3.6).name = "cafeteria_floor";
  box(area, frame, -2.5, 0.1, 5.37, 9.2, 0.36, 0.12);
  box(area, frame, -7.03, 0.1, 3.65, 0.12, 0.36, 3.5);
  const inside = new THREE.Group();
  area.add(inside);
  inside.position.y = -0.08;
  const tableX = -4.7,
    tableZ = 3.65;
  box(inside, shell, tableX, 0.79, tableZ, 2.25, 0.09, 1.04).name =
    "cafeteria_table";
  for (const x of [-0.9, 0.9])
    for (const z of [-0.34, 0.34])
      box(inside, frame, tableX + x, 0.37, tableZ + z, 0.07, 0.74, 0.07);
  // Counter, shelf, and coffee maker define the room without signage.
  box(inside, frame, 0.7, 0.35, 4.65, 1.8, 0.7, 0.58);
  box(inside, shell, 0.7, 0.74, 4.65, 1.96, 0.1, 0.7);
  box(inside, frame, 0.25, 1.04, 4.73, 0.48, 0.52, 0.34);
  box(inside, paper, 0.25, 1.15, 4.53, 0.29, 0.16, 0.05);
  box(inside, shell, 1.45, 1.28, 5.2, 1.08, 0.08, 0.25);
  for (const x of [0.85, 1.1]) mug(inside).position.set(x, 0.87, 4.48);
  const humans = [];
  for (let index = 0; index < 2; index++) {
    const start = new THREE.Vector3(
      tableX + (index === 0 ? -1.42 : 1.42),
      0,
      tableZ,
    );
    const destination = new THREE.Vector3(
      tableX + (index === 0 ? -0.6 : 2.1),
      0,
      1.67,
    );
    const aisle = new THREE.Vector3(start.x, 0, 2.65);
    const firstLength = start.distanceTo(aisle),
      secondLength = aisle.distanceTo(destination);
    const pathLength = firstLength + secondLength;
    const clothes = new THREE.MeshStandardMaterial({
      color: index === 0 ? 0x474a42 : 0x7a8460,
      roughness: 0.95,
    });
    const person = new THREE.Group();
    person.name = `operator_${index}`;
    inside.add(person);
    const body = new THREE.Group();
    person.add(body);
    mesh(body, new THREE.CapsuleGeometry(0.14, 0.24, 4, 12), clothes, 0, 0.24);
    const head = mesh(
      body,
      new THREE.SphereGeometry(0.14, 16, 12),
      skin,
      0,
      0.64,
    );
    head.name = "head";
    mesh(
      body,
      new THREE.SphereGeometry(0.145, 16, 8, 0, Math.PI * 2, 0, Math.PI / 2),
      frame,
      0,
      0.65,
    );
    const arms = [],
      legs = [];
    for (const side of [-1, 1]) {
      const shoulder = new THREE.Group();
      body.add(shoulder);
      shoulder.position.set(side * 0.2, 0.37, 0);
      mesh(
        shoulder,
        new THREE.CylinderGeometry(0.048, 0.043, 0.22, 10),
        clothes,
        0,
        -0.11,
      );
      const elbow = new THREE.Group();
      shoulder.add(elbow);
      elbow.position.y = -0.22;
      mesh(
        elbow,
        new THREE.CylinderGeometry(0.043, 0.035, 0.24, 10),
        clothes,
        0,
        -0.12,
      );
      const hand = new THREE.Group();
      elbow.add(hand);
      hand.position.y = -0.24;
      mesh(hand, new THREE.SphereGeometry(0.046, 10, 8), skin);
      arms.push({ shoulder, elbow, hand });
      const hip = new THREE.Group();
      person.add(hip);
      hip.position.x = side * 0.085;
      mesh(
        hip,
        new THREE.CylinderGeometry(0.063, 0.055, 0.4, 10),
        clothes,
        0,
        -0.2,
      );
      const knee = new THREE.Group();
      knee.name = `knee_${side}`;
      hip.add(knee);
      knee.position.y = -0.4;
      mesh(
        knee,
        new THREE.CylinderGeometry(0.05, 0.04, 0.44, 10),
        clothes,
        0,
        -0.22,
      );
      const foot = box(person, frame, side * 0.085, 0.035, 0, 0.11, 0.07, 0.21);
      foot.name = `foot_${side}`;
      legs.push({ hip, knee, foot });
    }
    const handCup = mug(arms[1].hand);
    handCup.name = "hand_cup";
    handCup.position.y = 0.08;
    const tableCup = mug(inside);
    tableCup.position.set(tableX + (index === 0 ? -0.6 : 0.6), 0.91, tableZ);
    const chair = new THREE.Group();
    inside.add(chair);
    chair.position.copy(start);
    chair.rotation.y = index === 0 ? -Math.PI / 2 : Math.PI / 2;
    box(chair, frame, 0, 0.55, 0, 0.45, 0.08, 0.45);
    box(chair, frame, 0, 0.81, 0.24, 0.45, 0.48, 0.065);
    for (const x of [-0.18, 0.18])
      for (const z of [-0.17, 0.17])
        box(chair, frame, x, 0.255, z, 0.045, 0.51, 0.045);
    // A small inspection terminal sits on the production side of the bay.
    const console = new THREE.Group();
    inside.add(console);
    console.position.set(destination.x, 0, 1.13);
    box(console, frame, 0, 0.42, 0, 0.1, 0.84, 0.1);
    box(console, shell, 0, 1.08, 0, 0.55, 0.38, 0.1);
    box(console, frame, 0, 1.08, 0.059, 0.41, 0.24, 0.02);
    box(console, shell, 0, 0.84, 0.12, 0.48, 0.06, 0.24);
    const indicators = [];
    for (let i = 0; i < layout.length; i++)
      indicators.push(
        box(
          console,
          new THREE.MeshStandardMaterial({ color: 0x62645b, roughness: 0.9 }),
          -0.17 + i * 0.056,
          1.08,
          0.075,
          0.035,
          0.12,
          0.015,
        ),
      );
    humans.push({
      aisle,
      firstLength,
      secondLength,
      pathLength,
      indicators,
      person,
      body,
      head,
      arms,
      legs,
      handCup,
      tableCup,
      start,
      destination,
      index,
    });
  }
  function render(time, dark, frameState = factoryFrame(time, layout)) {
    floor.color.set(dark ? 0x272820 : 0xd8d4ca);
    frame.color.set(dark ? 0x62645b : 0x777b70);
    for (const human of humans) {
      human.indicators.forEach((material, index) =>
        material.material.color.set(
          layout[index].id === frameState.activeStation ? 0xffb34a : 0x62645b,
        ),
      );
      const pose = operatorPose(time, human.index),
        walk = pose.mode === "walking" || pose.mode === "returning";
      const walked =
        human.pathLength *
        (pose.mode === "returning" ? 1 - pose.travel : pose.travel);
      const gait = (walked / 0.64) * Math.PI * 2;
      const seatedYaw = human.index === 0 ? -Math.PI / 2 : Math.PI / 2;
      const along = pose.travel * human.pathLength;
      const onFirst = along < human.firstLength;
      const from = onFirst ? human.start : human.aisle,
        to = onFirst ? human.aisle : human.destination;
      const share = onFirst
        ? along / human.firstLength
        : (along - human.firstLength) / human.secondLength;
      human.person.position.lerpVectors(from, to, share);
      let routeYaw = Math.atan2(-(to.x - from.x), -(to.z - from.z));
      if (pose.mode === "returning") routeYaw += Math.PI;
      let yaw = seatedYaw;
      if (pose.mode === "rising") yaw = turn(seatedYaw, 0, pose.standing);
      if (pose.mode === "walking")
        yaw = turn(routeYaw, 0, Math.max(0, (pose.travel - 0.88) / 0.12));
      if (pose.mode === "checking") yaw = 0;
      if (pose.mode === "returning")
        yaw = turn(
          turn(0, routeYaw, Math.min(1, (pose.phase - 24) / 0.3)),
          seatedYaw,
          Math.max(0, (0.12 - pose.travel) / 0.12),
        );
      human.person.rotation.y = yaw;
      const hipHeight =
        0.56 +
        0.29 * pose.standing +
        (walk ? 0.012 * Math.abs(Math.sin(gait)) : 0);
      human.body.position.y = hipHeight;
      human.head.rotation.x = 0.07 * pose.coffee;
      for (let i = 0; i < 2; i++) {
        const leg = human.legs[i],
          phase =
            (((gait + i * Math.PI) % (Math.PI * 2)) + Math.PI * 2) %
            (Math.PI * 2);
        let footZ = -0.27 * (1 - pose.standing),
          footY = 0.035;
        if (walk) {
          if (phase < Math.PI) footZ = -0.16 + (0.32 * phase) / Math.PI;
          else {
            const share = (phase - Math.PI) / Math.PI;
            footZ = 0.16 - 0.32 * share * share * (3 - 2 * share);
            footY += 0.095 * Math.sin(share * Math.PI);
          }
        }
        const angles = legAngles(hipHeight, footY, footZ);
        leg.hip.position.y = hipHeight;
        leg.hip.rotation.x = angles.hipAngle;
        leg.knee.rotation.x = angles.kneeAngle;
        leg.foot.position.y = footY;
        leg.foot.position.z = footZ - 0.045;
        const arm = human.arms[i];
        arm.shoulder.rotation.x = walk
          ? -0.2 * Math.sin(gait + i * Math.PI)
          : 0.45;
        arm.elbow.rotation.x = walk ? 0.22 : 0.8;
      }
      const right = human.arms[1];
      if (pose.mode === "seated") {
        const smooth = (value) => value * value * (3 - 2 * value);
        const lift = smooth(Math.min(1, pose.coffee / 0.35));
        const inward = smooth(Math.max(0, (pose.coffee - 0.35) / 0.65));
        const cupY = 0.91 + 0.25 * lift,
          cupZ = -0.34 + 0.27 * inward;
        const angles = armAngles(hipHeight + 0.37, cupY - 0.08, cupZ);
        right.shoulder.rotation.x = angles.shoulderAngle;
        right.elbow.rotation.x = angles.elbowAngle;
      }
      if (pose.mode === "checking") {
        right.shoulder.rotation.x = 1.15 + 0.06 * Math.sin(time * 2);
        right.elbow.rotation.x = 0.9;
      }
      human.handCup.visible = pose.mode === "seated";
      human.tableCup.visible = pose.mode !== "seated";
      const armAngle = right.shoulder.rotation.x + right.elbow.rotation.x;
      human.handCup.rotation.x = -armAngle;
      human.handCup.position.y = 0.08 * Math.cos(armAngle);
      human.handCup.position.z = -0.08 * Math.sin(armAngle);
    }
  }
  render(0, false);
  return render;
}
