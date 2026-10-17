/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { useState } from "react";
export function bridgeRecoveryValues(data: FormData): Record<string, string> {
  const recovery = String(data.get("completion_recovery") ?? ""),
    checkpoints = String(data.get("checkpoints") ?? "");
  if (
    !["", "legacy", "required"].includes(recovery) ||
    !["", "disabled", "required"].includes(checkpoints) ||
    (checkpoints === "required" && recovery !== "required")
  )
    throw new Error(
      "Required implementation checkpoints need required original result recovery.",
    );
  return {
    ...(recovery ? { completion_recovery: recovery } : {}),
    ...(checkpoints ? { checkpoints } : {}),
  };
}
export function BridgeRecoveryChoices({
  config,
}: {
  config: Record<string, string | boolean>;
}) {
  const [recovery, setRecovery] = useState(
    String(config.completion_recovery ?? ""),
  );
  const [checkpoints, setCheckpoints] = useState(
    String(config.checkpoints ?? ""),
  );
  return (
    <fieldset className="bridge-recovery-choices">
      <legend>Original model results</legend>
      <div className="field">
        <label htmlFor="bridge-completion-recovery">
          Original model result recovery
        </label>
        <select
          id="bridge-completion-recovery"
          name="completion_recovery"
          value={recovery}
          onChange={(e) => setRecovery(e.target.value)}
        >
          <option value="">Keep current default (unset)</option>
          <option value="legacy">Legacy</option>
          <option value="required">Required: retain the original result</option>
        </select>
      </div>
      <div className="field">
        <label htmlFor="bridge-checkpoints">Implementation checkpoints</label>
        <select
          id="bridge-checkpoints"
          name="checkpoints"
          value={checkpoints}
          onChange={(e) => setCheckpoints(e.target.value)}
        >
          <option value="">Keep current default (unset)</option>
          <option value="disabled">Disabled</option>
          <option value="required">
            Required: save implementation checkpoints
          </option>
        </select>
      </div>
      <p className="subtle">
        Checkpoints support the Bridge implementation with text and static file
        tools. Required checkpoints need required result recovery. Configure the
        separate review profile independently.
      </p>
      {checkpoints === "required" && recovery !== "required" && (
        <p className="alert" role="alert">
          Select required result recovery before saving required checkpoints.
        </p>
      )}
    </fieldset>
  );
}
