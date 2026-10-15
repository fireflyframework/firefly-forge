# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""A required aggregate check must fail when any prerequisite did not succeed."""

import json
import runpy
import unittest
from pathlib import Path

require_success = runpy.run_path(str(Path(__file__).resolve().parents[1] / "scripts/check_ci_gates.py"))[
    "require_success"
]


class RequiredGateTests(unittest.TestCase):
    def test_complete_success(self):
        require_success(json.dumps({name: {"result": "success", "outputs": {}} for name in self.names}))

    names = ("python", "studio", "documentation", "container")

    def test_each_failed_cancelled_or_skipped_job_blocks_packaging(self):
        for name in self.names:
            for result in ("failure", "cancelled", "skipped", "in_progress", "SUCCESS", ""):
                with self.subTest(name=name, result=result):
                    values = {key: {"result": "success"} for key in self.names}
                    values[name] = {"result": result}
                    with self.assertRaises(ValueError):
                        require_success(json.dumps(values))

    def test_missing_or_unknown_job_cannot_make_the_required_check_green(self):
        for missing in self.names:
            with self.subTest(missing=missing):
                values = {key: {"result": "success"} for key in self.names if key != missing}
                with self.assertRaises(ValueError):
                    require_success(json.dumps(values))
        values = {key: {"result": "success"} for key in self.names}
        values["unexpected"] = {"result": "success"}
        with self.assertRaises(ValueError):
            require_success(json.dumps(values))

    def test_malformed_or_ambiguous_results_fail_closed(self):
        for value in ("", "null", "[]", "{}", "not json", '"success"', "x" * 20_000):
            with self.subTest(value=value[:20]), self.assertRaises(ValueError):
                require_success(value)
        for malformed in (None, [], "success", {"result": True}, {"result": ["success"]}):
            with self.subTest(malformed=malformed):
                values = {key: {"result": "success"} for key in self.names}
                values["python"] = malformed
                with self.assertRaises(ValueError):
                    require_success(json.dumps(values))

    def test_duplicate_job_or_result_keys_are_not_a_success_receipt(self):
        valid = {key: {"result": "success"} for key in self.names}
        duplicate = json.dumps(valid)[:-1] + ',"python":{"result":"success"}}'
        with self.assertRaises(ValueError):
            require_success(duplicate)
        ambiguous = json.dumps(valid).replace('"result": "success"', '"result":"failure","result":"success"', 1)
        with self.assertRaises(ValueError):
            require_success(ambiguous)
