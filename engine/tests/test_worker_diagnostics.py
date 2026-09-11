from __future__ import annotations

try:
    import _bootstrap  # noqa: F401
except ModuleNotFoundError:
    from . import _bootstrap  # noqa: F401

import unittest

from scripts.a_share_panic_index.worker_diagnostics import run_worker_diagnostics


class TestWorkerDiagnostics(unittest.TestCase):
    def test_real_spawn_workers_cover_all_outcomes_and_are_reaped(self):
        result = run_worker_diagnostics(timeout_seconds=1.5)

        self.assertTrue(result["ok"])
        self.assertEqual(result["start_method"], "spawn")
        self.assertEqual(
            set(result["cases"]),
            {"success", "abnormal_exit", "hard_timeout"},
        )
        self.assertEqual(result["cases"]["success"]["value"], "spawn-ok")
        self.assertEqual(result["cases"]["abnormal_exit"]["exit_code"], 23)
        self.assertEqual(
            result["cases"]["hard_timeout"]["exception_type"],
            "ProviderTimeout",
        )
        self.assertEqual(len(set(result["worker_pids"])), 3)
        self.assertEqual(result["residual_pids"], [])


if __name__ == "__main__":
    unittest.main()
