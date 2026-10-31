/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { expect, it } from "vitest";
import { readFileSync } from "node:fs";
import * as THREE from "three";
import { factoryViewportHeight, overviewCameraOffset } from "./FactoryViewport";

it("uses a meaningful share of the actual main area and grows on taller desktop screens", () => {
  const normal = factoryViewportHeight(760, 1192);
  const tall = factoryViewportHeight(940, 1672);
  expect(normal / 760).toBeGreaterThanOrEqual(0.45);
  expect(normal / 760).toBeLessThanOrEqual(0.55);
  expect(tall).toBeGreaterThan(normal);
  expect(tall).toBeGreaterThan(360);
});

it("keeps a focused phone scene proportional while allowing a short page to scroll", () => {
  const phone = factoryViewportHeight(660, 390);
  expect(phone / 660).toBeGreaterThanOrEqual(0.3);
  expect(phone / 660).toBeLessThanOrEqual(0.4);
  expect(factoryViewportHeight(260, 375)).toBeGreaterThanOrEqual(180);
});

it("fills both dimensions with the real factory geometry instead of enlarging empty ground", () => {
  const asset = JSON.parse(
    readFileSync(
      new URL("../public/assets/forge-operational-model.json", import.meta.url),
      "utf8",
    ),
  );
  asset.object.images = [];
  asset.object.textures = [];
  for (const material of asset.object.materials)
    for (const key of Object.keys(material))
      if (key.toLowerCase().endsWith("map")) delete material[key];
  const world = new THREE.ObjectLoader().parse(asset.object);
  world.updateWorldMatrix(true, true);
  const points: THREE.Vector3[] = [];
  world.traverse((node) => {
    if (!(node instanceof THREE.Mesh)) return;
    node.geometry.computeBoundingBox();
    const box = node.geometry.boundingBox!;
    for (const x of [box.min.x, box.max.x])
      for (const y of [box.min.y, box.max.y])
        for (const z of [box.min.z, box.max.z])
          points.push(
            new THREE.Vector3(x, y, z).applyMatrix4(node.matrixWorld),
          );
  });
  expect(points.length).toBeGreaterThan(1000);
  for (const [width, height] of [
    [1111, 365],
    [1591, 451],
    [711, 301],
  ]) {
    const camera = new THREE.OrthographicCamera();
    const center = new THREE.Box3()
      .setFromPoints(points)
      .getCenter(new THREE.Vector3());
    camera.position
      .copy(center)
      .add(new THREE.Vector3(...overviewCameraOffset(points, width / height)));
    camera.lookAt(center);
    camera.updateMatrixWorld(true);
    const bounds = new THREE.Box3().setFromPoints(
      points.map((point) =>
        point.clone().applyMatrix4(camera.matrixWorldInverse),
      ),
    );
    const span = bounds.getSize(new THREE.Vector3());
    const fittedHeight = Math.max(span.y, span.x / (width / height)) * 1.1;
    expect(span.y / fittedHeight).toBeGreaterThanOrEqual(0.85);
    expect(span.y / fittedHeight).toBeLessThanOrEqual(0.92);
    expect(span.x / ((fittedHeight * width) / height)).toBeGreaterThanOrEqual(
      0.85,
    );
    expect(span.x / ((fittedHeight * width) / height)).toBeLessThanOrEqual(
      0.92,
    );
  }
});
