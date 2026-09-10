from __future__ import annotations

try:
    import _bootstrap  # noqa: F401
except ModuleNotFoundError:
    from . import _bootstrap  # noqa: F401

import os
import tempfile
import unittest
from pathlib import Path

from scripts.a_share_panic_index.providers.probe import run_source_probe
from tests.helpers import make_database, settings


@unittest.skipUnless(os.environ.get("RUN_LIVE_TESTS") == "1", "需要显式启用真实网络测试")
class TestLiveSources(unittest.TestCase):
    def test_live_probe_performs_real_requests(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = run_source_probe(
                settings(), make_database(root), root / "source_probe.json"
            )
            self.assertEqual(result["probe_mode"], "live")
            self.assertTrue(any(item["available"] for item in result["results"]))


if __name__ == "__main__":
    unittest.main()
