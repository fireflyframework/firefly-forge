/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
 * https://www.apache.org/licenses/LICENSE-2.0 */
const dwell = 1.2,
  transit = 0.8,
  reset = 0.8;
const ease = (value) => value * value * (3 - 2 * value);
export function factoryFrame(time, layout) {
  const duration = layout.length * (dwell + transit);
  const phase = ((time % duration) + duration) % duration;
  const index = Math.min(
    Math.floor(phase / (dwell + transit)),
    layout.length - 1,
  );
  const elapsed = phase - index * (dwell + transit);
  if (elapsed < dwell)
    return {
      mode: "dwell",
      stationIndex: index,
      activeStation: layout[index].id,
      dwellFraction: elapsed / dwell,
      carrierX: layout[index].x,
      opacity: 1,
    };
  if (index < layout.length - 1) {
    const share = ease((elapsed - dwell) / transit);
    return {
      mode: "transit",
      stationIndex: index + 1,
      activeStation: null,
      dwellFraction: 0,
      carrierX:
        layout[index].x + (layout[index + 1].x - layout[index].x) * share,
      opacity: 1,
    };
  }
  const share = Math.max(0, Math.min(1, (elapsed - dwell) / reset));
  return {
    mode: "reset",
    stationIndex: 0,
    activeStation: null,
    dwellFraction: 0,
    carrierX: share < 0.5 ? layout.at(-1).x : layout[0].x,
    opacity: Math.abs(share - 0.5) * 2,
  };
}
export function applyFactoryFrame(frame, actions, mixer) {
  for (const [id, { action, duration }] of actions) {
    if (id === frame.activeStation) {
      action.enabled = true;
      action.play();
      action.paused = true;
      action.time = frame.dwellFraction * duration;
    } else action.stop();
  }
  mixer.update(0);
}
