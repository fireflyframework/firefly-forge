/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
 * https://www.apache.org/licenses/LICENSE-2.0 */
const smooth = (value) => value * value * (3 - 2 * value);
export function operatorPose(time, index) {
  const phase = (time % 64) - index * 32;
  let mode = "seated",
    standing = 0,
    travel = 0;
  if (phase >= 16 && phase < 17) {
    mode = "rising";
    standing = smooth(phase - 16);
  } else if (phase >= 17 && phase < 20) {
    mode = "walking";
    standing = 1;
    travel = smooth((phase - 17) / 3);
  } else if (phase >= 20 && phase < 24) {
    mode = "checking";
    standing = 1;
    travel = 1;
  } else if (phase >= 24 && phase < 27) {
    mode = "returning";
    standing = 1;
    travel = 1 - smooth((phase - 24) / 3);
  } else if (phase >= 27 && phase < 28) {
    mode = "sitting";
    standing = 1 - smooth(phase - 27);
  }
  return {
    mode,
    phase,
    standing,
    travel,
    coffee:
      mode === "seated"
        ? Math.pow(Math.max(0, Math.sin(time * 0.6 + index * 0.7)), 3)
        : 0,
  };
}
export function legAngles(hipHeight, footY, footZ) {
  const upper = 0.4,
    lower = 0.44;
  const distance = Math.min(
    upper + lower - 0.0001,
    Math.hypot(hipHeight - footY, footZ),
  );
  const axis = Math.atan2(-footZ, hipHeight - footY);
  const alpha = Math.acos(
    Math.max(
      -1,
      Math.min(
        1,
        (upper * upper + distance * distance - lower * lower) /
          (2 * upper * distance),
      ),
    ),
  );
  const beta = Math.acos(
    Math.max(
      -1,
      Math.min(
        1,
        (lower * lower + distance * distance - upper * upper) /
          (2 * lower * distance),
      ),
    ),
  );
  return { hipAngle: axis + alpha, kneeAngle: -alpha - beta };
}

export function armAngles(shoulderHeight, handY, handZ) {
  const upper = 0.22,
    lower = 0.24,
    down = shoulderHeight - handY;
  const distance = Math.min(upper + lower - 0.0001, Math.hypot(down, handZ));
  const axis = Math.atan2(-handZ, down);
  const alpha = Math.acos(
    Math.max(
      -1,
      Math.min(
        1,
        (upper * upper + distance * distance - lower * lower) /
          (2 * upper * distance),
      ),
    ),
  );
  const beta = Math.acos(
    Math.max(
      -1,
      Math.min(
        1,
        (lower * lower + distance * distance - upper * upper) /
          (2 * lower * distance),
      ),
    ),
  );
  return { shoulderAngle: axis - alpha, elbowAngle: alpha + beta };
}
