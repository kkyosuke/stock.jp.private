from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
PUBLIC_SHA = "a" * 40


class PublicSubmoduleReadinessTest(unittest.TestCase):
    def setUp(self) -> None:
        scratch = ROOT / ".usagi/test-tmp"
        scratch.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=scratch)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        for path in ("scripts", "stock.jp/scripts", "bin", "tmp"):
            (self.root / path).mkdir(parents=True)
        (self.root / "stock.jp/.git").touch()
        shutil.copy2(ROOT / "scripts/update-public-submodule.sh", self.root / "scripts")
        for name in ("setup-workspace.sh", "validate-public-auto-update.sh"):
            self.executable("scripts/" + name, "#!/bin/sh\nexit 0\n")
        # Isolate remote Git and setup effects; exercise the real update script.
        self.executable(
            "bin/git",
            "#!/bin/sh\n"
            'case "$*" in\n'
            '  *"--show-toplevel"*) printf "%s\\n" "$TEST_ROOT" ;;\n'
            '  *"rev-parse"*) printf "%s\\n" "$TEST_PUBLIC_SHA" ;;\n'
            '  *"ls-remote"*) printf "%s\\trefs/heads/main\\n" "$TEST_PUBLIC_SHA" ;;\n'
            "esac\n",
        )
        (self.root / "bin/python3").symlink_to(sys.executable)
        (self.root / "stock.jp/scripts/operation_state.py").write_text(
            'print(\'{"valid": true, "errors": []}\')\n'
        )
        (self.root / "stock.jp/scripts/operation_bootstrap.py").write_text(
            "import os\n"
            'print(os.environ["TEST_READINESS"])\n'
            'raise SystemExit(int(os.environ["TEST_READINESS_EXIT"]))\n'
        )

    def executable(self, path: str, body: str) -> None:
        target = self.root / path
        target.write_text(body)
        target.chmod(0o755)

    def run_update(self, readiness: dict, exit_code: int = 0) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["sh", str(self.root / "scripts/update-public-submodule.sh")],
            env={
                **os.environ,
                "PATH": str(self.root / "bin") + os.pathsep + os.environ["PATH"],
                "TMPDIR": str(self.root / "tmp"),
                "GITHUB_OUTPUT": str(self.root / "github-output"),
                "TEST_ROOT": str(self.root),
                "TEST_PUBLIC_SHA": PUBLIC_SHA,
                "TEST_READINESS": json.dumps(readiness),
                "TEST_READINESS_EXIT": str(exit_code),
            },
            capture_output=True,
            text=True,
            check=False,
        )

    def test_failed_readiness_reports_blockers_and_preserves_exit_code(self) -> None:
        result = self.run_update(
            {"paper_go": False, "blockers": ["full-universe coverage is below minimum"]},
            exit_code=7,
        )
        self.assertEqual(result.returncode, 7)
        self.assertIn("full-universe coverage is below minimum", result.stdout)
        self.assertIn("readiness check failed (exit 7)", result.stderr)
        self.assertFalse((self.root / "github-output").exists())
        self.assertEqual(list((self.root / "tmp").iterdir()), [])

    def test_successful_readiness_publishes_validated_commit(self) -> None:
        result = self.run_update({"paper_go": True, "broker_submission": "HUMAN_ONLY"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            (self.root / "github-output").read_text(), f"public_sha={PUBLIC_SHA}\n"
        )

    def test_exit_zero_cannot_bypass_paper_or_submission_guard(self) -> None:
        for readiness in (
            {"paper_go": False, "broker_submission": "HUMAN_ONLY"},
            {"paper_go": True, "broker_submission": "AUTOMATIC"},
        ):
            with self.subTest(readiness=readiness):
                result = self.run_update(readiness)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse((self.root / "github-output").exists())


if __name__ == "__main__":
    unittest.main()
