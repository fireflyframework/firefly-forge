/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { useEffect, useLayoutEffect, useRef, useState } from "react";
import type {
  AnimationAction,
  Object3D,
  WebGLRenderer,
  MeshStandardMaterial,
} from "three";
import "./FactoryScene.css";
import { FactoryMotion, syncStationClips } from "./FactoryMotion";

export type FactoryStation = {
  id: string;
  label: string;
  state: "waiting" | "active" | "done" | "failed" | "paused" | "skipped";
  detail: string;
};
export type FactorySceneProps = {
  stations: FactoryStation[];
  theme: "light" | "dark";
  onSelect: (id: string) => void;
  selectedId: string | null;
  suspended?: boolean;
};
type Model = {
  version: number;
  object: Record<string, unknown>;
  layout: Array<{ id: string; x: number }>;
};
type Controller = { update: () => void; dispose: () => void };

async function loadModel(signal: AbortSignal): Promise<Model> {
  const compressed = typeof DecompressionStream !== "undefined";
  const response = await fetch(
    "/assets/forge-operational-model.json" + (compressed ? ".gz" : ""),
    { signal },
  );
  if (!response.ok) throw Error("The 3D factory asset is unavailable.");
  const data =
    compressed && !response.headers.get("content-encoding")
      ? await new Response(
          response.body!.pipeThrough(new DecompressionStream("gzip")),
        ).json()
      : await response.json();
  if (data.version !== 1 || !data.object || !Array.isArray(data.layout))
    throw Error("The 3D factory asset has an unsupported format.");
  return data as Model;
}

export function FactoryScene(props: FactorySceneProps) {
  const viewport = useRef<HTMLDivElement>(null);
  const current = useRef(props);
  const controller = useRef<Controller | null>(null);
  const [rendererType, setRendererType] = useState<"loading" | "webgl" | "svg">(
    "loading",
  );
  const [reason, setReason] = useState("");
  const [generation, setGeneration] = useState(0);
  const [reducedMotion, setReducedMotion] = useState(false);
  useEffect(() => {
    const query = matchMedia("(prefers-reduced-motion: reduce)");
    const update = () => setReducedMotion(query.matches);
    update();
    query.addEventListener("change", update);
    return () => query.removeEventListener("change", update);
  }, []);
  useEffect(() => {
    const container = viewport.current;
    if (!container) return;
    const abort = new AbortController();
    let disposed = false;
    let renderer: WebGLRenderer | undefined;
    let release: () => void = () => {};
    setRendererType("loading");
    setReason("");
    void (async () => {
      const [THREE, model] = await Promise.all([
        import("three"),
        loadModel(abort.signal),
      ]);
      if (disposed) return;
      const canvas = document.createElement("canvas");
      const context = canvas.getContext("webgl2", {
        alpha: true,
        antialias: true,
        powerPreference: "low-power",
      });
      if (!context) throw Error("3D rendering is unavailable in this browser.");
      renderer = new THREE.WebGLRenderer({
        canvas,
        context,
        alpha: true,
        antialias: true,
      });
      renderer.setPixelRatio(Math.min(devicePixelRatio, 2));
      renderer.outputColorSpace = THREE.SRGBColorSpace;
      renderer.toneMapping = THREE.ACESFilmicToneMapping;
      renderer.setClearColor(0x10110f, 0);
      renderer.shadowMap.type = THREE.PCFSoftShadowMap;
      function disposeObjects(object: Object3D) {
        const geometries = new Set<import("three").BufferGeometry>(),
          materials = new Set<import("three").Material>(),
          textures = new Set<import("three").Texture>();
        object.traverse((node) => {
          if (node instanceof THREE.Mesh) {
            geometries.add(node.geometry);
            for (const material of Array.isArray(node.material)
              ? node.material
              : [node.material]) {
              materials.add(material);
              for (const value of Object.values(material))
                if (value instanceof THREE.Texture) textures.add(value);
            }
          }
        });
        geometries.forEach((item) => item.dispose());
        textures.forEach((item) => item.dispose());
        materials.forEach((item) => item.dispose());
      }
      const world = await new THREE.ObjectLoader().parseAsync(model.object);
      if (disposed) {
        disposeObjects(world);
        renderer.dispose();
        renderer.forceContextLoss();
        return;
      }
      const scene = new THREE.Scene();
      scene.add(world);
      release = () => {
        disposeObjects(scene);
        renderer?.dispose();
        renderer?.forceContextLoss();
      };
      const camera = new THREE.OrthographicCamera(-16, 16, 8, -8, 0.1, 120);
      const hemi = new THREE.HemisphereLight(0xf3f1eb, 0x34372e, 1.7);
      scene.add(hemi);
      const key = new THREE.DirectionalLight(0xfff1db, 3.3);
      key.position.set(-8, 18, 10);
      key.castShadow = true;
      key.shadow.mapSize.set(2048, 2048);
      Object.assign(key.shadow.camera, {
        left: -22,
        right: 22,
        top: 14,
        bottom: -14,
        near: 1,
        far: 70,
      });
      key.shadow.bias = -0.0003;
      key.shadow.normalBias = 0.035;
      scene.add(key);
      const fill = new THREE.DirectionalLight(0xe3e9df, 1.35);
      fill.position.set(10, 9, -9);
      scene.add(fill);
      const groundMaterial = new THREE.MeshStandardMaterial({
        color: 0x10110f,
        roughness: 1,
      });
      const ground = new THREE.Mesh(
        new THREE.PlaneGeometry(40, 14),
        groundMaterial,
      );
      ground.rotation.x = -Math.PI / 2;
      ground.position.set(0, -0.42, -1);
      ground.receiveShadow = true;
      scene.add(ground);
      const mixer = new THREE.AnimationMixer(world),
        actions = new Map<string, AnimationAction>();
      for (const clip of world.animations)
        actions.set(
          clip.name,
          mixer.clipAction(clip).setLoop(THREE.LoopRepeat, Infinity),
        );
      const groups = new Map<string, Object3D>(),
        indicators = new Map<string, MeshStandardMaterial>();
      for (const item of model.layout) {
        const group = world.getObjectByName("station_" + item.id);
        if (group) groups.set(item.id, group);
        const lamp = new THREE.MeshStandardMaterial({
          color: 0xb4bdb8,
          emissive: 0x000000,
          roughness: 0.35,
        });
        const marker = new THREE.Mesh(
          new THREE.CylinderGeometry(0.12, 0.12, 0.045, 24),
          lamp,
        );
        marker.position.set(item.x, 0.21, 0.95);
        scene.add(marker);
        indicators.set(item.id, lamp);
      }
      const treadTextures: Array<{
        from: number;
        to: number;
        texture: import("three").CanvasTexture;
      }> = [];
      for (let index = 0; index < model.layout.length - 1; index++) {
        const image = document.createElement("canvas");
        image.width = 128;
        image.height = 32;
        const ctx = image.getContext("2d")!;
        ctx.fillStyle = "#272820";
        ctx.fillRect(0, 0, 128, 32);
        ctx.fillStyle = "#62645b";
        for (let x = 0; x < 128; x += 16) ctx.fillRect(x, 0, 2, 32);
        const texture = new THREE.CanvasTexture(image);
        texture.colorSpace = THREE.SRGBColorSpace;
        texture.wrapS = texture.wrapT = THREE.RepeatWrapping;
        texture.repeat.set(2, 1);
        const belt = new THREE.Mesh(
          new THREE.BoxGeometry(3.62, 0.025, 0.82),
          new THREE.MeshStandardMaterial({ map: texture, roughness: 0.9 }),
        );
        belt.position.set(
          (model.layout[index].x + model.layout[index + 1].x) / 2,
          0.65,
          0,
        );
        scene.add(belt);
        treadTextures.push({
          from: model.layout[index].x,
          to: model.layout[index + 1].x,
          texture,
        });
      }
      const carrier = new THREE.Group();
      carrier.name = "receipted_output";
      const tray = new THREE.Mesh(
        new THREE.BoxGeometry(0.78, 0.09, 0.56),
        new THREE.MeshStandardMaterial({ color: 0xffb34a, roughness: 0.45 }),
      );
      const paper = new THREE.Mesh(
        new THREE.BoxGeometry(0.68, 0.025, 0.49),
        new THREE.MeshStandardMaterial({ color: 0xf3f1eb, roughness: 0.65 }),
      );
      paper.position.y = 0.065;
      carrier.add(tray, paper);
      carrier.position.set(model.layout[0].x, 0.76, 0);
      scene.add(carrier);
      const motion = new FactoryMotion(model.layout);
      let lastTheme = "";
      let frame: number | undefined,
        lastTime = performance.now(),
        lost = false,
        offscreen = false;
      const motionQuery = matchMedia("(prefers-reduced-motion: reduce)");
      const isVisible = () =>
        !window.document.hidden && !offscreen && !lost && !disposed;
      const sourceMaterials = new Map<MeshStandardMaterial, string>();
      world.traverse((node) => {
        if (node instanceof THREE.Mesh) {
          node.castShadow = node.receiveShadow = true;
          for (const material of Array.isArray(node.material)
            ? node.material
            : [node.material])
            if (material instanceof THREE.MeshStandardMaterial)
              sourceMaterials.set(
                material,
                material.userData.sourceColor ?? material.color.getHexString(),
              );
        }
      });
      function fit() {
        if (!renderer || !isVisible()) return;
        const width = container!.clientWidth,
          height = container!.clientHeight;
        if (!width || !height) return;
        renderer.setSize(width, height, false);
        renderer.shadowMap.enabled = width >= 700;
        const focused =
          width < 700 && current.current.selectedId
            ? groups.get(current.current.selectedId)
            : null;
        const bounds = new THREE.Box3().setFromObject(focused ?? world),
          center = bounds.getCenter(new THREE.Vector3());
        camera.position.copy(center).add(new THREE.Vector3(10, 14, 27));
        camera.lookAt(center);
        camera.updateMatrixWorld(true);
        const corners = [];
        for (const x of [bounds.min.x, bounds.max.x])
          for (const y of [bounds.min.y, bounds.max.y])
            for (const z of [bounds.min.z, bounds.max.z])
              corners.push(
                new THREE.Vector3(x, y, z).applyMatrix4(
                  camera.matrixWorldInverse,
                ),
              );
        const horizontal =
          Math.max(...corners.map((p) => p.x)) -
          Math.min(...corners.map((p) => p.x));
        const vertical =
          Math.max(...corners.map((p) => p.y)) -
          Math.min(...corners.map((p) => p.y));
        const span = Math.max(vertical, horizontal / (width / height)) * 1.18;
        camera.left = (-span * width) / height / 2;
        camera.right = -camera.left;
        camera.top = span / 2;
        camera.bottom = -camera.top;
        camera.updateProjectionMatrix();
      }
      function paint(time: number) {
        frame = undefined;
        if (!renderer || !isVisible()) return;
        const delta = Math.min((time - lastTime) / 1000, 0.05);
        lastTime = time;
        const from = motion.x;
        motion.advance(delta);
        carrier.position.x = motion.x;
        syncStationClips(actions, motion.workingStation);
        if (motion.workingStation) mixer.update(delta);
        if (motion.x !== from)
          for (const belt of treadTextures)
            if (
              Math.max(from, motion.x) >= belt.from &&
              Math.min(from, motion.x) <= belt.to
            )
              belt.texture.offset.x -= Math.sign(motion.x - from) * delta * 0.6;
        renderer.render(scene, camera);
        if (motion.needsMotion) frame = requestAnimationFrame(paint);
      }
      function schedule() {
        if (!isVisible()) return;
        lastTime = performance.now();
        if (frame === undefined) frame = requestAnimationFrame(paint);
      }
      function update() {
        if (disposed || lost) return;
        const { stations, theme } = current.current;
        if (lastTheme !== theme) {
          lastTheme = theme;
          groundMaterial.color.set(theme === "light" ? "#f3f1eb" : "#10110f");
          hemi.intensity = theme === "light" ? 2.3 : 1.65;
          renderer!.toneMappingExposure = theme === "light" ? 1.03 : 1.32;
          const light: Record<string, string> = {
            brickFirefly: "#b5823c",
            brickThird: "#a9b1ab",
            shell: "#c5c5b6",
            graphite: "#474a42",
            charcoal: "#272820",
            trimThird: "#62645b",
          };
          for (const [material, color] of sourceMaterials)
            material.color.set(
              theme === "light"
                ? (light[material.name] ?? "#" + color)
                : "#" + color,
            );
        }
        const styles = getComputedStyle(container!);
        for (const item of model.layout) {
          const state =
            stations.find((station) => station.id === item.id)?.state ??
            "waiting";
          const lamp = indicators.get(item.id)!;
          lamp.color.set(
            state === "active" || current.current.selectedId === item.id
              ? styles.getPropertyValue("--accent-ink").trim() || "#ffb34a"
              : state === "done"
                ? styles.getPropertyValue("--success-ink").trim() || "#7a8460"
                : state === "failed"
                  ? styles.getPropertyValue("--danger-ink").trim() || "#b46148"
                  : "#b4bdb8",
          );
          lamp.emissive.copy(lamp.color);
          lamp.emissiveIntensity = state === "active" ? 0.35 : 0.04;
        }

        motion.update(stations, {
          hidden: window.document.hidden,
          offscreen,
          suspended: current.current.suspended,
          reduced: motionQuery.matches,
        });
        carrier.visible = motion.targetStation !== null;
        carrier.position.x = motion.x;
        syncStationClips(actions, motion.workingStation);
        fit();
        schedule();
      }
      const resize = new ResizeObserver(() => {
        fit();
        schedule();
      });
      resize.observe(container);
      const intersection = new IntersectionObserver((entries) => {
        offscreen = !entries[0].isIntersecting;
        visibility();
      });
      intersection.observe(container);
      function visibility() {
        if (!isVisible()) {
          if (frame !== undefined) cancelAnimationFrame(frame);
          frame = undefined;
        }
        update();
      }
      function contextLost(event: Event) {
        event.preventDefault();
        lost = true;
        if (frame !== undefined) cancelAnimationFrame(frame);
        frame = undefined;
        setReason(
          "The 3D context was lost. Recorded stage states remain available below.",
        );
        setRendererType("svg");
      }
      const ray = new THREE.Raycaster(),
        pointer = new THREE.Vector2();
      function select(event: PointerEvent) {
        if (lost) return;
        const box = canvas.getBoundingClientRect();
        pointer.set(
          ((event.clientX - box.left) / box.width) * 2 - 1,
          (-(event.clientY - box.top) / box.height) * 2 + 1,
        );
        ray.setFromCamera(pointer, camera);
        for (const hit of ray.intersectObjects(world.children, true)) {
          let object: Object3D | null = hit.object;
          while (object) {
            if (object.userData.stage && groups.has(object.userData.stage)) {
              current.current.onSelect(object.userData.stage);
              return;
            }
            object = object.parent;
          }
        }
      }
      canvas.setAttribute("aria-hidden", "true");
      canvas.addEventListener("pointerup", select);
      canvas.addEventListener("webglcontextlost", contextLost);
      window.document.addEventListener("visibilitychange", visibility);
      motionQuery.addEventListener("change", update);
      container.appendChild(canvas);
      release = () => {
        resize.disconnect();
        intersection.disconnect();
        canvas.removeEventListener("pointerup", select);
        canvas.removeEventListener("webglcontextlost", contextLost);
        window.document.removeEventListener("visibilitychange", visibility);
        motionQuery.removeEventListener("change", update);
        if (frame !== undefined) cancelAnimationFrame(frame);
        mixer.stopAllAction();
        mixer.uncacheRoot(world);
        disposeObjects(scene);
        renderer?.dispose();
        renderer?.forceContextLoss();
        canvas.remove();
      };
      controller.current = { update, dispose: release };
      setRendererType("webgl");
      update();
    })().catch((error) => {
      if (!disposed) {
        release();
        renderer?.dispose();
        renderer?.forceContextLoss();
        setReason(
          error instanceof Error
            ? error.message
            : "3D rendering is unavailable.",
        );
        setRendererType("svg");
      }
    });
    return () => {
      disposed = true;
      abort.abort();
      release();
      controller.current = null;
    };
  }, [generation]);
  useLayoutEffect(() => {
    current.current = props;
    controller.current?.update();
  }, [props]);
  return (
    <figure
      className="factory-live"
      aria-label="Operational factory"
      data-renderer={rendererType}
      data-theme={props.theme}
      data-reduced-motion={reducedMotion}
    >
      <div ref={viewport} className="factory-live-viewport">
        {rendererType !== "webgl" && (
          <img
            src={`/assets/forge-factory-${props.theme}.svg`}
            alt="Firefly factory geometry. Controls below show actual recorded stage states."
          />
        )}
        <div className="factory-live-caption">
          <span>
            {rendererType === "webgl"
              ? "3D production floor"
              : rendererType === "loading"
                ? "Loading the 3D production floor"
                : "Static factory view"}
          </span>
          <span>
            {props.suspended
              ? "Updates unavailable; showing last recorded state"
              : reducedMotion
                ? "Motion reduced"
                : props.stations.some((station) => station.state === "paused")
                  ? "Paused"
                  : props.stations.some((station) => station.state === "active")
                    ? "Recorded work is active"
                    : "No active machinery"}
          </span>
        </div>
      </div>
      {rendererType === "svg" && (
        <div className="factory-live-fallback" role="status">
          <span>{reason} Stage controls remain available.</span>
          <button
            type="button"
            onClick={() => setGeneration((value) => value + 1)}
          >
            Try 3D again
          </button>
        </div>
      )}
      <figcaption
        className="factory-live-stations"
        aria-label="Recorded execution stages"
      >
        {props.stations.map((station, index) => (
          <button
            key={station.id}
            type="button"
            onClick={() => props.onSelect(station.id)}
            onKeyDown={(event) => {
              if (event.key === "ArrowRight" || event.key === "ArrowLeft") {
                event.preventDefault();
                const buttons =
                  event.currentTarget.parentElement?.querySelectorAll<HTMLButtonElement>(
                    "button",
                  );
                buttons?.[
                  (index +
                    (event.key === "ArrowRight" ? 1 : -1) +
                    props.stations.length) %
                    props.stations.length
                ]?.focus();
              }
            }}
            aria-pressed={props.selectedId === station.id}
            className={`factory-live-station ${station.state}`}
            title={station.detail}
          >
            <span>{station.label}</span>
            <strong>{station.state}</strong>
            <small>{station.detail}</small>
          </button>
        ))}
        {!props.stations.length && (
          <p>Choose a run to inspect its recorded execution stages.</p>
        )}
      </figcaption>
    </figure>
  );
}
