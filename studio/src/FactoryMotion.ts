/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import type { AnimationAction } from "three";
import type { FactoryStation } from "./FactoryScene";

type MotionGates = {
  hidden?: boolean;
  offscreen?: boolean;
  suspended?: boolean;
  reduced?: boolean;
};

export class FactoryMotion {
  x = 0;
  targetStation: string | null = null;
  private initialized = false;
  private active = false;
  private frozen = false;
  private transition: { from: number; to: number; elapsed: number } | null =
    null;

  constructor(
    private readonly layout: ReadonlyArray<{ id: string; x: number }>,
  ) {}

  get workingStation(): string | null {
    return this.active && !this.frozen && !this.transition
      ? this.targetStation
      : null;
  }

  get needsMotion(): boolean {
    return (
      !this.frozen && (this.transition !== null || this.workingStation !== null)
    );
  }

  get travelling(): boolean {
    return !this.frozen && this.transition !== null;
  }

  update(stations: readonly FactoryStation[], gates: MotionGates = {}) {
    const blocked = stations.find(
      (s) => s.state === "paused" || s.state === "failed",
    );
    const receipt =
      blocked ??
      stations.find((s) => s.state === "active") ??
      [...stations].reverse().find((s) => s.state === "done");
    const position = this.layout.find((item) => item.id === receipt?.id);
    this.frozen = Object.values(gates).some(Boolean);
    this.active = receipt?.state === "active" && Boolean(position);
    const target = position ? receipt!.id : null;
    if (!position) {
      this.targetStation = null;
      this.transition = null;
      this.initialized = false;
      return;
    }
    if (!this.initialized || !this.active || gates.reduced) {
      this.x = position.x;
      this.transition = null;
    } else if (target !== this.targetStation) {
      this.transition =
        this.x === position.x
          ? null
          : { from: this.x, to: position.x, elapsed: 0 };
    }
    this.targetStation = target;
    this.initialized = true;
  }

  advance(delta: number) {
    if (!this.travelling || !this.transition) return;
    this.transition.elapsed += Math.min(Math.max(delta, 0), 0.05);
    const share = Math.min(this.transition.elapsed / 0.9, 1);
    const eased = share * share * (3 - 2 * share);
    this.x =
      this.transition.from +
      (this.transition.to - this.transition.from) * eased;
    if (share === 1) {
      this.x = this.transition.to;
      this.transition = null;
    }
  }
}

export function syncStationClips(
  actions: ReadonlyMap<string, AnimationAction>,
  workingStation: string | null,
) {
  for (const [id, action] of actions) {
    if (id === workingStation) {
      if (!action.isScheduled()) action.play();
      action.paused = false;
    } else action.paused = true;
  }
}
