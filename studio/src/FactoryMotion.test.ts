/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { describe, expect, it } from "vitest";
import {
  AnimationClip,
  AnimationMixer,
  Object3D,
  NumberKeyframeTrack,
} from "three";
import type { FactoryStation } from "./FactoryScene";
import { FactoryMotion, syncStationClips } from "./FactoryMotion";
const layout = [
  { id: "prepare", x: 0 },
  { id: "implement", x: 4 },
  { id: "verify", x: 8 },
];
function stations(
  active: string,
  state: FactoryStation["state"] = "active",
): FactoryStation[] {
  return layout.map(({ id }) => ({
    id,
    label: id,
    state: id === active ? state : id === "prepare" ? "done" : "waiting",
    detail: "Actual fixture receipt",
  }));
}
function arrive(motion: FactoryMotion) {
  for (let i = 0; i < 20; i++) motion.advance(0.05);
}
describe("recorded carrier and machinery synchronization", () => {
  it("does not activate a real station clip before carrier arrival", () => {
    const object = new Object3D(),
      mixer = new AnimationMixer(object);
    const action = mixer.clipAction(
      new AnimationClip("implement", 1, [
        new NumberKeyframeTrack(".position[y]", [0, 1], [0, 1]),
      ]),
    );
    const actions = new Map([["implement", action]]),
      motion = new FactoryMotion(layout);
    motion.update(stations("prepare"));
    motion.update(stations("implement"));
    for (let i = 0; i < 10; i++) {
      motion.advance(0.05);
      syncStationClips(actions, motion.workingStation);
      mixer.update(0.05);
      expect(action.isScheduled()).toBe(false);
      expect(action.time).toBe(0);
      expect(object.position.y).toBe(0);
    }
    expect(motion.x).toBeGreaterThan(0);
    expect(motion.x).toBeLessThan(4);
    arrive(motion);
    syncStationClips(actions, motion.workingStation);
    mixer.update(0.05);
    expect(motion.x).toBe(4);
    expect(action.isScheduled()).toBe(true);
    expect(object.position.y).toBeGreaterThan(0);
    const pose = object.position.y;
    motion.update(stations("implement", "paused"));
    syncStationClips(actions, motion.workingStation);
    mixer.update(20);
    expect(object.position.y).toBe(pose);
    expect(motion.x).toBe(4);
  });
  it.each(["failed", "paused"] as const)(
    "retains %s at its actual machine instead of the preceding completed station",
    (state) => {
      const motion = new FactoryMotion(layout);
      motion.update(stations("prepare"));
      motion.update(stations("implement"));
      motion.advance(0.05);
      motion.update(stations("implement", state));
      expect(motion.targetStation).toBe("implement");
      expect(motion.x).toBe(4);
      expect(motion.workingStation).toBeNull();
      motion.advance(1000);
      expect(motion.x).toBe(4);
      expect(motion.needsMotion).toBe(false);
    },
  );
  it.each(["hidden", "offscreen", "suspended"] as const)(
    "freezes %s without resetting travel or machinery pose",
    (gate) => {
      const motion = new FactoryMotion(layout);
      motion.update(stations("prepare"));
      motion.update(stations("implement"));
      motion.advance(0.05);
      const x = motion.x;
      motion.update(stations("implement"), { [gate]: true });
      motion.advance(10000);
      expect(motion.x).toBe(x);
      expect(motion.needsMotion).toBe(false);
      expect(motion.workingStation).toBeNull();
      motion.update(stations("implement"));
      motion.advance(0.05);
      expect(motion.x).toBeGreaterThan(x);
      expect(motion.x).toBeLessThan(4);
    },
  );
  it("places reduced-motion receipts at their actual station without travel or machinery", () => {
    const motion = new FactoryMotion(layout);
    motion.update(stations("prepare"), { reduced: true });
    motion.update(stations("implement"), { reduced: true });
    expect(motion.x).toBe(4);
    expect(motion.workingStation).toBeNull();
    expect(motion.needsMotion).toBe(false);
    motion.advance(10);
    expect(motion.x).toBe(4);
    motion.update(stations("verify"), { reduced: true });
    expect(motion.x).toBe(8);
    motion.update(stations("verify"));
    expect(motion.travelling).toBe(false);
    expect(motion.workingStation).toBe("verify");
  });
  it("starts a new run at its first receipt after a waiting or empty reset", () => {
    const motion = new FactoryMotion(layout);
    motion.update(stations("verify"));
    motion.update(
      layout.map(({ id }) => ({
        id,
        label: id,
        state: "waiting",
        detail: "New run waiting",
      })),
    );
    expect(motion.targetStation).toBeNull();
    motion.update(stations("implement"));
    expect(motion.x).toBe(4);
    expect(motion.travelling).toBe(false);
    motion.update([]);
    motion.update(stations("prepare"));
    expect(motion.x).toBe(0);
    expect(motion.travelling).toBe(false);
  });
  it("does not reset for same-target polls and follows only the latest receipt without overshoot", () => {
    const motion = new FactoryMotion(layout);
    motion.update(stations("prepare"));
    motion.update(stations("implement"));
    motion.advance(0.05);
    const x = motion.x;
    motion.update(stations("implement"));
    expect(motion.x).toBe(x);
    motion.advance(0.05);
    const next = motion.x;
    motion.update(stations("verify"));
    expect(motion.x).toBe(next);
    expect(motion.workingStation).toBeNull();
    let previous = next;
    for (let i = 0; i < 20; i++) {
      motion.advance(0.05);
      expect(motion.x).toBeGreaterThanOrEqual(previous);
      expect(motion.x).toBeLessThanOrEqual(8);
      previous = motion.x;
    }
    expect(motion.x).toBe(8);
    expect(motion.workingStation).toBe("verify");
    motion.update(stations("implement"));
    arrive(motion);
    expect(motion.x).toBe(4);
    expect(motion.workingStation).toBe("implement");
  });
  it("never completes WAL stages on a clock and stops at the latest completed receipt", () => {
    const motion = new FactoryMotion(layout),
      actual = stations("implement"),
      original = structuredClone(actual);
    motion.update(actual);
    for (let i = 0; i < 100; i++) motion.advance(0.05);
    expect(actual).toEqual(original);
    expect(motion.targetStation).toBe("implement");
    motion.update(
      layout.map(({ id }) => ({
        id,
        label: id,
        state: "done",
        detail: "Actual completion receipt",
      })),
    );
    expect(motion.targetStation).toBe("verify");
    expect(motion.x).toBe(8);
    expect(motion.needsMotion).toBe(false);
    expect(motion.workingStation).toBeNull();
    motion.advance(10000);
    expect(motion.x).toBe(8);
  });
});
