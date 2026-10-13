/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
 * https://www.apache.org/licenses/LICENSE-2.0 */
import * as THREE from "three";
import { factoryFrame, applyFactoryFrame } from "./factory-sequence.mjs";
import { addCafeteria } from "./operators.mjs";
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
  let envelope;
  function resize(width, height) {
    if (!width || !height) return;
    renderer.setSize(width, height, false);
    const bounds = envelope ?? new THREE.Box3().setFromObject(world),
      center = bounds.getCenter(new THREE.Vector3());
    camera.position.copy(center).add(new THREE.Vector3(24, 17, 20));
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
    const span = Math.max(vertical, horizontal / (width / height)) * 1.05;
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
    setBounds(bounds) {
      envelope = bounds;
    },
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
  const actions = new Map(
    object.animations.map((clip) => [
      clip.name,
      {
        action: mixer.clipAction(clip).setLoop(THREE.LoopOnce, 1),
        duration: clip.duration,
      },
    ]),
  );
  const amber = new THREE.MeshStandardMaterial({
    color: 0xffb34a,
    roughness: 0.55,
    transparent: true,
  });
  const paper = new THREE.MeshStandardMaterial({
    color: 0xf3f1eb,
    roughness: 0.8,
    transparent: true,
  });
  const carrier = new THREE.Group();
  view.world.add(carrier);
  mesh(carrier, new THREE.BoxGeometry(0.78, 0.09, 0.56), amber, 0, 0);
  mesh(carrier, new THREE.BoxGeometry(0.68, 0.025, 0.49), paper, 0, 0.065);
  carrier.position.set(model.layout[0].x, 0.76, 0);
  const people = addCafeteria(view.world, model.layout);
  // Include mechanical keyframe extents, not only the initial rest pose.
  const envelope = new THREE.Box3();
  for (const [id] of actions)
    for (let fraction = 0; fraction <= 1; fraction += 0.125) {
      applyFactoryFrame(
        { activeStation: id, dwellFraction: fraction },
        actions,
        mixer,
      );
      envelope.union(new THREE.Box3().setFromObject(view.world));
    }
  applyFactoryFrame(factoryFrame(0, model.layout), actions, mixer);
  view.setBounds(envelope.expandByScalar(0.08));
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
  let lastTheme;
  return {
    ...view,
    render(time, dark) {
      const frame = factoryFrame(time, model.layout);
      applyFactoryFrame(frame, actions, mixer);
      people(time, dark, frame);
      carrier.position.x = frame.carrierX;
      carrier.visible = frame.opacity > 0.001;
      amber.opacity = frame.opacity;
      paper.opacity = frame.opacity;
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
