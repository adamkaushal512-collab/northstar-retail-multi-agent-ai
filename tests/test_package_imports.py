"""Validate that the repository skeleton's package structure imports.

Standard library only, so it runs without installing anything:
    PYTHONPATH=src python3 -m unittest discover -s tests -v
"""

import importlib
import os
import subprocess
import sys
import unittest

PACKAGES = [
    "northstar",
    "northstar.api",
    "northstar.orchestration",
    "northstar.orchestration.deterministic",
    "northstar.orchestration.single_agent",
    "northstar.orchestration.multi_agent",
    "northstar.agents",
    "northstar.integrations",
    "northstar.security",
    "northstar.evidence",
    "northstar.state",
    "northstar.observability",
    "northstar.llm",
]

ALTERNATIVES = [
    "northstar.orchestration.deterministic",
    "northstar.orchestration.single_agent",
    "northstar.orchestration.multi_agent",
]


class PackageImportTests(unittest.TestCase):
    def test_all_packages_import(self):
        for name in PACKAGES:
            with self.subTest(package=name):
                module = importlib.import_module(name)
                self.assertTrue(module.__doc__, f"{name} needs a docstring")

    def test_alternatives_import_independently(self):
        """Each alternative imports alone, without loading the other two."""
        src = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
        for name in ALTERNATIVES:
            others = [o for o in ALTERNATIVES if o != name]
            code = (
                "import importlib, sys\n"
                f"importlib.import_module({name!r})\n"
                f"loaded = [o for o in {others!r} if o in sys.modules]\n"
                "sys.exit(1 if loaded else 0)\n"
            )
            with self.subTest(alternative=name):
                result = subprocess.run(
                    [sys.executable, "-c", code],
                    env={**os.environ, "PYTHONPATH": src},
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
