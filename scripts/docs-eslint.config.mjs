/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
 * https://www.apache.org/licenses/LICENSE-2.0 */
import { createRequire } from "node:module";
const require = createRequire(
  new URL("../studio/package.json", import.meta.url),
);
const tseslint = require("typescript-eslint");
export default tseslint.config(...tseslint.configs.recommended);
