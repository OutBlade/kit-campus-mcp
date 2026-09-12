"""Run the real workflow shell block against controlled CLI exit codes."""

import os
import re
import shutil
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class WorkflowExitTests(unittest.TestCase):
    def test_only_temporary_outages_are_deferred(self):
        bash = (Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "Git/bin/bash.exe"
                if os.name == "nt" else Path(shutil.which("bash") or "/bin/bash"))
        if not bash.is_file():
            self.skipTest("bash is needed to verify the GitHub Actions shell block")
        workflow = (ROOT / ".github/workflows/kit-notify.yml").read_text(encoding="utf-8")
        block = re.search(
            r"      - name: Check KIT and notify.*?        run: \|\n(.*?)(?=\n      - name:)",
            workflow, re.S,
        ).group(1)
        script = textwrap.dedent(block)
        for code, expected, outcome in ((0, 0, "checked"), (75, 0, "deferred"), (1, 1, None), (2, 2, None)):
            with self.subTest(code=code), tempfile.TemporaryDirectory() as directory:
                env = dict(os.environ, CHECK_ONLY="true", PING="false",
                           GITHUB_OUTPUT="outputs.txt", GITHUB_STEP_SUMMARY="summary.md")
                result = subprocess.run(
                    [str(bash), "-e", "-c", f"python() {{ return {code}; }}\n" + script],
                    cwd=directory, env=env, capture_output=True, text=True,
                )
                self.assertEqual(result.returncode, expected, result.stderr)
                output = Path(directory) / "outputs.txt"
                if outcome is not None:
                    self.assertEqual(output.read_text().strip(), f"status={outcome}")
                else:
                    self.assertFalse(output.exists())
                if outcome == "deferred":
                    summary = (Path(directory) / "summary.md").read_text()
                    self.assertIn("not a successful results check", summary)
