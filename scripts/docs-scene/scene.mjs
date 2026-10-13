/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
 * https://www.apache.org/licenses/LICENSE-2.0 */
import * as THREE from "three";
import { mountScene } from "./lifecycle.mjs";
const assetRoot = new URL(".", document.currentScript.src);

function disposeObjects(scene) {
  const geometry = new Set(),
    materials = new Set(),
    textures = new Set();
  scene.traverse((object) => {
    if (!object.isMesh) return;
    geometry.add(object.geometry);
    for (const material of Array.isArray(object.material)
      ? object.material
      : [object.material]) {
      materials.add(material);
      for (const value of Object.values(material))
        if (value?.isTexture) textures.add(value);
    }
  });
  textures.forEach((item) => item.dispose());
  materials.forEach((item) => item.dispose());
  geometry.forEach((item) => item.dispose());
}
function setup(viewport) {
  const canvas = document.createElement("canvas");
  canvas.setAttribute("aria-hidden", "true");
  const context = canvas.getContext("webgl2", {
    alpha: true,
    antialias: true,
    powerPreference: "low-power",
  });
  if (!context) throw new Error("WebGL unavailable");
  const renderer = new THREE.WebGLRenderer({ canvas, context, alpha: true });
  renderer.setPixelRatio(Math.min(devicePixelRatio, 1.5));
  renderer.outputColorSpace = THREE.SRGBColorSpace;
  renderer.toneMapping = THREE.ACESFilmicToneMapping;
  renderer.setClearColor(0, 0);
  const scene = new THREE.Scene();
  const world = new THREE.Group();
  scene.add(world);
  const camera = new THREE.OrthographicCamera(-8, 8, 5, -5, 0.1, 150);
  const hemi = new THREE.HemisphereLight(0xf3f1eb, 0x34372e, 2.3);
  scene.add(hemi);
  const key = new THREE.DirectionalLight(0xfff1db, 3.3);
  key.position.set(-8, 18, 10);
  scene.add(key);
  const fill = new THREE.DirectionalLight(0xe3e9df, 1.35);
  fill.position.set(10, 9, -9);
  scene.add(fill);
  viewport.append(canvas);
  function resize(width, height) {
    if (!width || !height) return;
    renderer.setSize(width, height, false);
    const bounds = new THREE.Box3().setFromObject(world),
      center = bounds.getCenter(new THREE.Vector3());
    camera.position.copy(center).add(new THREE.Vector3(10, 14, 27));
    camera.lookAt(center);
    camera.updateMatrixWorld(true);
    const corners = [];
    for (const x of [bounds.min.x, bounds.max.x])
      for (const y of [bounds.min.y, bounds.max.y])
        for (const z of [bounds.min.z, bounds.max.z])
          corners.push(
            new THREE.Vector3(x, y, z).applyMatrix4(camera.matrixWorldInverse),
          );
    const horizontal =
      Math.max(...corners.map((p) => p.x)) -
      Math.min(...corners.map((p) => p.x));
    const vertical =
      Math.max(...corners.map((p) => p.y)) -
      Math.min(...corners.map((p) => p.y));
    const span = Math.max(vertical, horizontal / (width / height)) * 1.15;
    camera.left = (-span * width) / height / 2;
    camera.right = -camera.left;
    camera.top = span / 2;
    camera.bottom = -camera.top;
    camera.updateProjectionMatrix();
  }
  return {
    canvas,
    renderer,
    scene,
    world,
    hemi,
    camera,
    resize,
    dispose() {
      disposeObjects(scene);
      renderer.dispose();
      renderer.forceContextLoss();
      canvas.remove();
    },
  };
}
function mesh(parent, geometry, material, x, y, z = 0) {
  const object = new THREE.Mesh(geometry, material);
  object.position.set(x, y, z);
  parent.add(object);
  return object;
}
async function build(viewport, signal) {
  // Authorized generated snapshot; no website constructors or brand-kit source.
  const response = await fetch(new URL(FORGE_DOC_MODEL_URL, assetRoot), {
    signal,
  });
  if (!response.ok) throw new Error("Factory model unavailable");
  const model = await response.json();
  if (model.version !== 1 || !model.object || model.layout?.length !== 7)
    throw new Error("Invalid factory model");
  const object = await new THREE.ObjectLoader().parseAsync(model.object);
  if (signal.aborted) {
    disposeObjects(object);
    throw new Error("Navigation interrupted");
  }
  let view;
  try {
    view = setup(viewport);
  } catch (error) {
    disposeObjects(object);
    throw error;
  }
  view.world.add(object);
  const mixer = new THREE.AnimationMixer(object);
  for (const clip of object.animations)
    mixer.clipAction(clip).setLoop(THREE.LoopRepeat, Infinity).play();
  const amber = new THREE.MeshStandardMaterial({
    color: 0xffb34a,
    roughness: 0.55,
  });
  const paper = new THREE.MeshStandardMaterial({
    color: 0xf3f1eb,
    roughness: 0.8,
  });
  const carrier = new THREE.Group();
  view.world.add(carrier);
  mesh(carrier, new THREE.BoxGeometry(0.78, 0.09, 0.56), amber, 0, 0);
  mesh(carrier, new THREE.BoxGeometry(0.68, 0.025, 0.49), paper, 0, 0.065);
  carrier.position.set(model.layout[0].x, 0.76, 0);
  // First-party adjacent break area; the authorized model remains unchanged.
  const furniture = new THREE.MeshStandardMaterial({
    color: 0x62645b,
    roughness: 0.9,
  });
  const skin = new THREE.MeshStandardMaterial({
    color: 0xbcae9b,
    roughness: 0.9,
  });
  const tableX = -2.5,
    tableZ = 3.7;
  mesh(
    view.world,
    new THREE.BoxGeometry(2.3, 0.09, 1.05),
    paper,
    tableX,
    0.76,
    tableZ,
  );
  for (const x of [-0.9, 0.9])
    for (const z of [-0.35, 0.35])
      mesh(
        view.world,
        new THREE.CylinderGeometry(0.045, 0.045, 0.72, 8),
        furniture,
        tableX + x,
        0.36,
        tableZ + z,
      );
  for (const x of [-0.6, 0.6]) {
    const mug = new THREE.Group();
    view.world.add(mug);
    mug.position.set(tableX + x, 0.89, tableZ);
    mesh(mug, new THREE.CylinderGeometry(0.09, 0.08, 0.16, 16), paper, 0, 0);
    mesh(
      mug,
      new THREE.CircleGeometry(0.073, 16),
      furniture,
      0,
      0.081,
    ).rotation.x = -Math.PI / 2;
    mesh(mug, new THREE.TorusGeometry(0.06, 0.013, 8, 16), paper, 0.095, 0);
  }
  const humans = [];
  for (let index = 0; index < 2; index++) {
    const start = new THREE.Vector3(
      tableX + (index === 0 ? -1.55 : 1.55),
      0,
      tableZ,
    );
    const destination = new THREE.Vector3(
      model.layout[index === 0 ? 2 : 3].x,
      0,
      1.45,
    );
    const clothes = new THREE.MeshStandardMaterial({
      color: index === 0 ? 0x474a42 : 0x7a8460,
      roughness: 0.95,
    });
    const person = new THREE.Group();
    view.world.add(person);
    person.position.copy(start);
    person.rotation.y = index === 0 ? -Math.PI / 2 : Math.PI / 2;
    const upper = new THREE.Group();
    person.add(upper);
    mesh(upper, new THREE.CapsuleGeometry(0.14, 0.24, 4, 12), clothes, 0, 0.88);
    mesh(upper, new THREE.SphereGeometry(0.14, 16, 12), skin, 0, 1.22);
    mesh(
      upper,
      new THREE.SphereGeometry(0.145, 16, 8, 0, Math.PI * 2, 0, Math.PI / 2),
      furniture,
      0,
      1.23,
    );
    for (const x of [-0.19, 0.19])
      mesh(
        upper,
        new THREE.CapsuleGeometry(0.045, 0.24, 4, 8),
        clothes,
        x,
        0.84,
        -0.08,
      ).rotation.x = -0.6;
    const legs = [];
    for (const x of [-0.085, 0.085]) {
      const thigh = mesh(
        person,
        new THREE.CylinderGeometry(0.065, 0.055, 0.36, 8),
        clothes,
        x,
        0.58,
        -0.13,
      );
      const shin = mesh(
        person,
        new THREE.CylinderGeometry(0.045, 0.045, 0.38, 8),
        clothes,
        x,
        0.2,
        -0.29,
      );
      legs.push({ thigh, shin });
    }
    const chair = new THREE.Group();
    view.world.add(chair);
    chair.position.copy(start);
    chair.rotation.copy(person.rotation);
    mesh(chair, new THREE.BoxGeometry(0.46, 0.08, 0.44), furniture, 0, 0.56);
    mesh(
      chair,
      new THREE.BoxGeometry(0.46, 0.42, 0.06),
      furniture,
      0,
      0.81,
      0.22,
    );
    for (const x of [-0.18, 0.18])
      for (const z of [-0.16, 0.16])
        mesh(
          chair,
          new THREE.BoxGeometry(0.035, 0.52, 0.035),
          furniture,
          x,
          0.26,
          z,
        );
    humans.push({ person, upper, legs, start, destination, index });
  }
  function people(time) {
    for (const human of humans) {
      const phase = (time % 48) - human.index * 24;
      const standing =
        phase >= 12 && phase < 24 ? Math.min(1, phase - 12, 24 - phase) : 0;
      let journey = 0;
      if (phase >= 13 && phase < 16) journey = (phase - 13) / 3;
      else if (phase >= 16 && phase < 20) journey = 1;
      else if (phase >= 20 && phase < 23) journey = 1 - (phase - 20) / 3;
      const eased = journey * journey * (3 - 2 * journey);
      human.person.position.lerpVectors(human.start, human.destination, eased);
      human.person.rotation.y =
        (human.index === 0 ? -Math.PI / 2 : Math.PI / 2) * (1 - eased);
      human.upper.position.y = 0.25 * standing;
      for (const { thigh, shin } of human.legs) {
        thigh.rotation.x = ((1 - standing) * Math.PI) / 2;
        thigh.position.y = 0.58 + 0.1 * standing;
        thigh.position.z = -0.13 * (1 - standing);
        shin.position.z = -0.29 * (1 - standing);
      }
    }
  }
  const sources = new Map();
  object.traverse((item) => {
    if (item.isMesh)
      for (const material of Array.isArray(item.material)
        ? item.material
        : [item.material])
        if (material.color && !sources.has(material))
          sources.set(material, material.color.clone());
  });
  const light = {
    brickFirefly: "#b5823c",
    brickThird: "#a9b1ab",
    shell: "#c5c5b6",
    graphite: "#474a42",
    charcoal: "#272820",
    trimThird: "#62645b",
  };
  let lastTime = 0,
    lastTheme;
  return {
    ...view,
    render(time, dark) {
      mixer.update(Math.max(0, time - lastTime) * 1.45);
      people(time);
      lastTime = time;
      carrier.position.x =
        model.layout[0].x +
        ((time / 9) % 1) * (model.layout.at(-1).x - model.layout[0].x);
      if (dark !== lastTheme) {
        lastTheme = dark;
        for (const [material, original] of sources)
          material.color.copy(
            dark ? original : new THREE.Color(light[material.name] ?? original),
          );
        view.hemi.intensity = dark ? 1.65 : 2.3;
        view.renderer.toneMappingExposure = dark ? 1.32 : 1.03;
      }
      view.renderer.render(view.scene, view.camera);
    },
    dispose() {
      mixer.stopAllAction();
      mixer.uncacheRoot(object);
      view.dispose();
    },
  };
}

for (const host of document.querySelectorAll("[data-docs-scene]"))
  mountScene(host, build);
