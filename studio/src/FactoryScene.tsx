/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { useEffect, useId, useLayoutEffect, useRef, useState } from "react";
import { ChevronRight, Focus, Maximize2 } from "lucide-react";
import type {
  AnimationAction,
  Object3D,
  WebGLRenderer,
  MeshStandardMaterial,
} from "three";
import "./FactoryScene.css";
import { factoryViewportHeight, overviewCameraOffset } from "./FactoryViewport";
import {
  FactoryMotion,
  syncStationClips,
  aggregateActiveStations,
} from "./FactoryMotion";

export type FactoryStation = {
  id: string;
  label: string;
  state:
    "waiting" | "active" | "done" | "failed" | "paused" | "skipped" | "unknown";
  detail: string;
  counts?: {
    active: number;
    waiting: number;
    needs_action: number;
    unknown: number;
    done: number;
  };
};
export type FactorySceneProps = {
  stations: FactoryStation[];
  theme: "light" | "dark";
  onSelect: (id: string) => void;
  selectedId: string | null;
  suspended?: boolean;
  aggregate?: boolean;
  observationLabel?: string;
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
  const rail = useRef<HTMLDivElement>(null);
  const railId = useId();
  const [moreStages, setMoreStages] = useState(false);
  useLayoutEffect(() => {
    const node = rail.current;
    if (!node) return;
    const measure = () =>
      setMoreStages(node.scrollWidth - node.clientWidth - node.scrollLeft > 2);
    measure();
    const resize = new ResizeObserver(measure);
    resize.observe(node);
    node.addEventListener("scroll", measure, { passive: true });
    return () => {
      resize.disconnect();
      node.removeEventListener("scroll", measure);
    };
  }, [props.stations.length]);
  useLayoutEffect(() => {
    const node = viewport.current;
    const main = node?.closest<HTMLElement>("main");
    if (!node || !main || !node.closest(".production-floor")) return;
    let pending = 0;
    const measure = () => {
      node.style.setProperty(
        "--factory-available-height",
        `${factoryViewportHeight(main.clientHeight, main.clientWidth)}px`,
      );
    };
    const schedule = () => {
      if (!pending)
        pending = requestAnimationFrame(() => {
          pending = 0;
          measure();
        });
    };
    measure();
    const resize = new ResizeObserver(schedule);
    resize.observe(main);
    window.addEventListener("resize", schedule);
    return () => {
      resize.disconnect();
      window.removeEventListener("resize", schedule);
      if (pending) cancelAnimationFrame(pending);
    };
  }, []);
  const current = useRef({ ...props, focusSelected: false });
  const controller = useRef<Controller | null>(null);
  const [rendererType, setRendererType] = useState<"loading" | "webgl" | "svg">(
    "loading",
  );
  const [reason, setReason] = useState("");
  const [generation, setGeneration] = useState(0);
  const [reducedMotion, setReducedMotion] = useState(false);
  const [focusSelected, setFocusSelected] = useState(
    () =>
      typeof window !== "undefined" &&
      window.matchMedia("(max-width: 600px)").matches,
  );
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
      const geometryCorners = new Map<Object3D, import("three").Vector3[]>();
      function corners(root: Object3D) {
        const retained = geometryCorners.get(root);
        if (retained) return retained;
        root.updateWorldMatrix(true, true);
        const points: import("three").Vector3[] = [];
        root.traverse((node) => {
          if (!(node instanceof THREE.Mesh)) return;
          node.geometry.computeBoundingBox();
          const box = node.geometry.boundingBox;
          if (!box) return;
          for (const x of [box.min.x, box.max.x])
            for (const y of [box.min.y, box.max.y])
              for (const z of [box.min.z, box.max.z])
                points.push(
                  new THREE.Vector3(x, y, z).applyMatrix4(node.matrixWorld),
                );
        });
        geometryCorners.set(root, points);
        return points;
      }
      function fit() {
        if (!renderer || !isVisible()) return;
        const width = container!.clientWidth,
          height = container!.clientHeight;
        if (!width || !height) return;
        renderer.setSize(width, height, false);
        renderer.shadowMap.enabled = width >= 700;
        const focused =
          current.current.focusSelected && current.current.selectedId
            ? groups.get(current.current.selectedId)
            : null;
        const points = corners(focused ?? world);
        const bounds = new THREE.Box3().setFromPoints(points),
          center = bounds.getCenter(new THREE.Vector3());
        camera.position
          .copy(center)
          .add(
            focused
              ? new THREE.Vector3(12, 20, 30)
              : new THREE.Vector3(
                  ...overviewCameraOffset(points, width / height),
                ),
          );
        camera.lookAt(center);
        camera.updateMatrixWorld(true);
        // Fit the visible geometry rather than empty corners of the long factory's world box.
        const projected = new THREE.Box3();
        for (const point of points)
          projected.expandByPoint(
            point.clone().applyMatrix4(camera.matrixWorldInverse),
          );
        const horizontal = projected.max.x - projected.min.x;
        const vertical = projected.max.y - projected.min.y;
        const target = projected.getCenter(new THREE.Vector3());
        const span = Math.max(vertical, horizontal / (width / height)) * 1.1;
        camera.left = target.x - (span * width) / height / 2;
        camera.right = target.x + (span * width) / height / 2;
        camera.top = target.y + span / 2;
        camera.bottom = target.y - span / 2;
        camera.updateProjectionMatrix();
      }
      function paint(time: number) {
        frame = undefined;
        if (!renderer || !isVisible()) return;
        const delta = Math.min((time - lastTime) / 1000, 0.05);
        lastTime = time;
        const aggregate = current.current.aggregate;
        const gates = {
          hidden: window.document.hidden,
          offscreen,
          suspended: current.current.suspended,
          reduced: motionQuery.matches,
        };
        motion.update(current.current.stations, gates);
        const active = aggregateActiveStations(current.current.stations, gates);
        const from = motion.x;
        if (!aggregate) motion.advance(delta);
        carrier.position.x = motion.x;
        syncStationClips(actions, aggregate ? active : motion.workingStation);
        if (aggregate ? active.size > 0 : motion.workingStation)
          mixer.update(delta);
        if (!aggregate && motion.x !== from)
          for (const belt of treadTextures)
            if (
              Math.max(from, motion.x) >= belt.from &&
              Math.min(from, motion.x) <= belt.to
            )
              belt.texture.offset.x -= Math.sign(motion.x - from) * delta * 0.6;
        renderer.render(scene, camera);
        if (aggregate ? active.size > 0 : motion.needsMotion)
          frame = requestAnimationFrame(paint);
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
        carrier.visible =
          !current.current.aggregate && motion.targetStation !== null;
        carrier.position.x = motion.x;
        syncStationClips(
          actions,
          current.current.aggregate
            ? aggregateActiveStations(stations, {
                hidden: window.document.hidden,
                offscreen,
                suspended: current.current.suspended,
                reduced: motionQuery.matches,
              })
            : motion.workingStation,
        );
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
    current.current = { ...props, focusSelected };
    controller.current?.update();
  }, [props, focusSelected]);
  return (
    <figure
      className="factory-live"
      aria-label="Operational factory"
      data-renderer={rendererType}
      data-theme={props.theme}
      data-reduced-motion={reducedMotion}
      data-camera={focusSelected ? "selected-stage" : "overview"}
      data-carrier-mode={props.aggregate ? "hidden" : "single-run"}
    >
      <div ref={viewport} className="factory-live-viewport">
        {rendererType === "webgl" && (
          <button
            type="button"
            className="secondary factory-live-camera"
            aria-pressed={focusSelected}
            disabled={!props.selectedId}
            onPointerUp={(event) => event.stopPropagation()}
            onClick={(event) => {
              event.stopPropagation();
              setFocusSelected((value) => !value);
            }}
          >
            {focusSelected ? <Maximize2 size={16} /> : <Focus size={16} />}
            {focusSelected ? "Show full floor" : "Focus selected stage"}
          </button>
        )}
        {rendererType !== "webgl" && (
          <img
            src={`/assets/forge-factory-${props.theme}.svg`}
            alt="Firefly factory geometry. Controls below show actual recorded stage states."
          />
        )}
        <div className="factory-live-caption">
          <span>
            {rendererType === "webgl"
              ? focusSelected
                ? `${props.stations.find((station) => station.id === props.selectedId)?.label ?? "Selected stage"} · stage view`
                : "Production floor"
              : rendererType === "loading"
                ? "Loading the 3D production floor"
                : "Static factory view"}
          </span>
          <span>
            {props.observationLabel ??
              (props.suspended
                ? "Updates unavailable; showing last recorded state"
                : reducedMotion
                  ? "Motion reduced"
                  : props.aggregate
                    ? props.stations.some(
                        (station) => station.state === "active",
                      )
                      ? "Recorded tasks are active across runs"
                      : "No recorded active tasks"
                    : props.stations.some(
                          (station) => station.state === "paused",
                        )
                      ? "Paused"
                      : props.stations.some(
                            (station) => station.state === "active",
                          )
                        ? "Recorded work is active"
                        : "No active machinery")}
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
      <figcaption className="factory-stage-navigation">
        <div
          className="factory-live-stations"
          aria-label="Recorded execution stages"
          role="group"
          ref={rail}
          id={railId}
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
              <strong>
                {props.aggregate && station.counts
                  ? station.counts.active ||
                    station.counts.waiting ||
                    station.counts.needs_action
                    ? `${station.counts.active} active${station.counts.waiting ? ` · ${station.counts.waiting} queued` : ""}${station.counts.needs_action ? ` · ${station.counts.needs_action} need action` : ""}`
                    : station.counts.unknown
                      ? `${station.counts.unknown} unknown`
                      : station.counts.done
                        ? `${station.counts.done} completed`
                        : "No stage records"
                  : station.state}
              </strong>
              <small>{station.detail}</small>
            </button>
          ))}
          {!props.stations.length && (
            <p>Choose a run to inspect its recorded execution stages.</p>
          )}
        </div>
        {moreStages && (
          <button
            type="button"
            className="quiet factory-stage-more"
            aria-label="Show more stages"
            aria-controls={railId}
            title="Show more stages"
            onClick={() =>
              rail.current?.scrollBy({
                left: rail.current.clientWidth * 0.75,
                behavior: reducedMotion ? "instant" : "smooth",
              })
            }
          >
            <ChevronRight size={18} />
            <span>More</span>
          </button>
        )}
      </figcaption>
    </figure>
  );
}
