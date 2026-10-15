"""Package metadata stays consistent."""

import tomllib
import unittest
from pathlib import Path

import xeneon_edge_ctl

ROOT = Path(__file__).resolve().parent.parent


class VersionTests(unittest.TestCase):
    def test_package_version_matches_pyproject(self) -> None:
        project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
        self.assertEqual(xeneon_edge_ctl.__version__, project["version"])


if __name__ == "__main__":
    unittest.main()
