/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
 * https://www.apache.org/licenses/LICENSE-2.0 */
export function motionState(state) {
  if (state.disposed) return "disposed";
  if (state.reduced) return "reduced";
  if (!state.playing) return "paused";
  if (state.hidden || !state.visible) return "hidden";
  return "running";
}

export async function mountScene(host, build) {
  const button = host.querySelector("[data-scene-toggle]");
  const status = host.querySelector("[data-scene-status]");
  const viewport = host.querySelector("[data-scene-viewport]");
  const preference = matchMedia("(prefers-reduced-motion: reduce)");
  const state = {
    playing: true,
    reduced: preference.matches,
    hidden: document.hidden,
    visible: false,
    disposed: false,
  };
  const abort = new AbortController();
  let scene,
    frame,
    last = 0,
    elapsed = 0;
  const theme = () => document.body.dataset.mdColorScheme === "slate";
  function paint(time) {
    frame = undefined;
    if (state.disposed || state.hidden || !state.visible || !scene) return;
    const moving = motionState(state) === "running";
    if (!moving || time - last >= 1000 / 30) {
      if (moving) elapsed += Math.min((time - last) / 1000, 0.06);
      last = time;
      scene.render(elapsed, theme());
    }
    if (moving) frame = requestAnimationFrame(paint);
  }
  function update() {
    if (frame !== undefined) cancelAnimationFrame(frame);
    frame = undefined;
    host.dataset.motion = motionState(state);
    button.disabled = state.reduced || !scene || state.disposed;
    button.setAttribute(
      "aria-pressed",
      String(state.playing && !state.reduced),
    );
    button.setAttribute(
      "aria-label",
      state.reduced
        ? "Motion reduced"
        : state.playing
          ? "Pause illustration"
          : "Play illustration",
    );
    button.textContent = state.reduced
      ? "Motion reduced"
      : state.playing
        ? "Pause"
        : "Play";
    status.textContent = state.reduced
      ? "Static illustration · reduced motion"
      : state.playing
        ? "Illustration · no live activity"
        : "Illustration paused";
    if (scene && !state.disposed && state.visible && !state.hidden) {
      last = performance.now();
      frame = requestAnimationFrame(paint);
    }
  }
  function resize() {
    if (scene && !state.disposed)
      scene.resize(viewport.clientWidth, viewport.clientHeight);
    update();
  }
  const observer = new IntersectionObserver(([entry]) => {
    state.visible = entry.isIntersecting;
    update();
  });
  observer.observe(host);
  const sizes = new ResizeObserver(resize);
  sizes.observe(viewport);
  const themes = new MutationObserver(update);
  themes.observe(document.body, {
    attributes: true,
    attributeFilter: ["data-md-color-scheme"],
  });
  button.addEventListener(
    "click",
    () => {
      state.playing = !state.playing;
      update();
    },
    { signal: abort.signal },
  );
  preference.addEventListener(
    "change",
    () => {
      state.reduced = preference.matches;
      update();
    },
    { signal: abort.signal },
  );
  document.addEventListener(
    "visibilitychange",
    () => {
      state.hidden = document.hidden;
      update();
    },
    { signal: abort.signal },
  );
  function dispose() {
    if (state.disposed) return;
    state.disposed = true;
    if (frame !== undefined) cancelAnimationFrame(frame);
    abort.abort();
    observer.disconnect();
    sizes.disconnect();
    themes.disconnect();
    scene?.dispose();
    host.dataset.motion = "disposed";
  }
  window.addEventListener(
    "pagehide",
    (event) => {
      if (event.persisted) {
        state.hidden = true;
        update();
      } else dispose();
    },
    { signal: abort.signal },
  );
  window.addEventListener(
    "pageshow",
    (event) => {
      if (event.persisted) {
        state.hidden = document.hidden;
        state.reduced = preference.matches;
        resize();
      }
    },
    { signal: abort.signal },
  );
  try {
    scene = await build(viewport, abort.signal);
    if (state.disposed) {
      scene.dispose();
      return;
    }
    host.dataset.renderer = "webgl";
    scene.canvas.addEventListener(
      "webglcontextlost",
      (event) => {
        event.preventDefault();
        dispose();
        host.dataset.renderer = "fallback";
        status.textContent = "Static illustration · 3D is unavailable";
        button.textContent = "Static view";
        button.setAttribute("aria-label", "Static illustration");
        button.disabled = true;
      },
      { signal: abort.signal },
    );
    resize();
  } catch {
    dispose();
    host.dataset.renderer = "fallback";
    status.textContent = "Static illustration · 3D is unavailable";
    button.textContent = "Static view";
    button.setAttribute("aria-label", "Static illustration");
    button.disabled = true;
  }
  return dispose;
}
