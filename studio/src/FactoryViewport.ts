/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */

export function factoryViewportHeight(
  mainHeight: number,
  mainWidth: number,
): number {
  // Stage details can scroll below the model rather than consume its remaining height.
  return Math.max(
    180,
    Math.round(mainHeight * (mainWidth <= 600 ? 0.35 : 0.48)),
  );
}

type Point = { x: number; y: number; z: number };

export function overviewCameraOffset(
  points: readonly Point[],
  aspect: number,
): [number, number, number] {
  const radius = Math.hypot(18, 30);
  const elevation = Math.atan2(24, radius);
  function projectedAspect(yaw: number) {
    const right = [Math.cos(yaw), -Math.sin(yaw)];
    const up = [
      -Math.sin(elevation) * Math.sin(yaw),
      Math.cos(elevation),
      -Math.sin(elevation) * Math.cos(yaw),
    ];
    let minX = Infinity,
      maxX = -Infinity,
      minY = Infinity,
      maxY = -Infinity;
    for (const point of points) {
      const x = point.x * right[0] + point.z * right[1];
      const y = point.x * up[0] + point.y * up[1] + point.z * up[2];
      minX = Math.min(minX, x);
      maxX = Math.max(maxX, x);
      minY = Math.min(minY, y);
      maxY = Math.max(maxY, y);
    }
    return (maxX - minX) / (maxY - minY);
  }
  let low = 0,
    high = Math.PI / 3;
  for (let step = 0; step < 8; step++) {
    const middle = (low + high) / 2;
    if (projectedAspect(middle) > aspect) low = middle;
    else high = middle;
  }
  const yaw = (low + high) / 2;
  return [radius * Math.sin(yaw), 24, radius * Math.cos(yaw)];
}
