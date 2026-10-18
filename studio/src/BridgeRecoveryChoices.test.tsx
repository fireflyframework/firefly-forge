/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import {
  BridgeRecoveryChoices,
  bridgeRecoveryValues,
} from "./BridgeRecoveryChoices";
it("shows explicit recovery/checkpoint choices and the supported implementation scope", () => {
  const html = renderToStaticMarkup(<BridgeRecoveryChoices config={{}} />);
  expect(html).toContain("Original model result recovery");
  expect(html).toContain("Implementation checkpoints");
  expect(html).toContain("static file");
  expect(html).toContain("<select");
  expect(html).toContain('value="" selected=""');
  expect(html).toContain(">Use current default</option>");
  expect(html).toContain('value="required">Required</option>');
  expect(html).toContain('aria-describedby="bridge-completion-recovery-help"');
  expect(html).toContain('aria-describedby="bridge-checkpoints-help"');
  expect(html).toContain("Required retains the original result.");
  expect(html).toContain("Required saves implementation checkpoints.");
});
it("preserves absent selections and refuses checkpoints without required recovery", () => {
  const data = new FormData();
  data.set("completion_recovery", "");
  data.set("checkpoints", "");
  expect(bridgeRecoveryValues(data)).toEqual({});
  data.set("checkpoints", "required");
  expect(() => bridgeRecoveryValues(data)).toThrow();
  data.set("completion_recovery", "required");
  expect(bridgeRecoveryValues(data)).toEqual({
    completion_recovery: "required",
    checkpoints: "required",
  });
  data.set("checkpoints", "disabled");
  expect(bridgeRecoveryValues(data)).toEqual({
    completion_recovery: "required",
    checkpoints: "disabled",
  });
  data.set("completion_recovery", "legacy");
  expect(bridgeRecoveryValues(data).completion_recovery).toBe("legacy");
});
