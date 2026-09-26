"""Behavioral tests for the pi_copy.py safe evidence-copy helper.

Symlinks are preserved literally and never followed; budgets use the bounded
no-follow walk. All trees are tiny; no GB allocation and no minute-long waits.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from runtime_helpers import RUNTIME

sys.path.insert(0, str(RUNTIME))
import pi_copy  # noqa: E402
from pi_size import measure  # noqa: E402


def run_copy(*args, timeout: float = 60) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(RUNTIME / "pi_copy.py"),
                           *[str(arg) for arg in args]],
                          capture_output=True, text=True, timeout=timeout)


class CopyTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="codex-pi-copy-")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    # ------------------------------------------------------------------
    def test_symlinks_are_preserved_literally_and_cycles_do_not_recurse(self):
        external = self.tmp / "external-large"
        external.mkdir()
        (external / "big.bin").write_bytes(b"E" * 100000)
        source = self.tmp / "source"
        source.mkdir()
        (source / "a.txt").write_bytes(b"A" * 10)
        os.symlink(external, source / "to_external")
        os.symlink("missing-target", source / "dangling")
        os.symlink(str(source), source / "self_cycle")
        os.symlink("../external-large", source / "relative_external")
        dest = self.tmp / "dest"

        proc = run_copy(source, dest, "--max-bytes", "1000")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["status"], "copied")
        self.assertEqual(payload["copiedBytes"], 10)
        self.assertEqual(payload["symlinksCopied"], 4)
        self.assertTrue((dest / "a.txt").is_file())
        self.assertTrue((dest / "to_external").is_symlink())
        self.assertEqual(os.readlink(dest / "to_external"), str(external))
        self.assertTrue((dest / "dangling").is_symlink())
        self.assertEqual(os.readlink(dest / "dangling"), "missing-target")
        self.assertTrue((dest / "self_cycle").is_symlink())
        self.assertEqual(os.readlink(dest / "self_cycle"), str(source))
        self.assertTrue((dest / "relative_external").is_symlink())
        self.assertEqual(os.readlink(dest / "relative_external"), "../external-large")
        self.assertFalse((dest / "external-large").exists(),
                         "the external target must never be expanded into the copy")
        measured = measure(dest)
        self.assertTrue(measured["complete"])
        self.assertEqual(measured["bytes"], 10, "only regular copied bytes count")
        self.assertEqual(measured["symlinksSkipped"], 4)

    def test_directory_recursion_and_overwrite_rejection(self):
        source = self.tmp / "tree"
        (source / "nested" / "deep").mkdir(parents=True)
        (source / "top.txt").write_text("top", encoding="utf-8")
        (source / "nested" / "deep" / "leaf.txt").write_text("leaf", encoding="utf-8")
        dest = self.tmp / "tree-copy"
        proc = run_copy(source, dest, "--max-bytes", "1000000")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual((dest / "top.txt").read_text(encoding="utf-8"), "top")
        self.assertEqual((dest / "nested" / "deep" / "leaf.txt").read_text(encoding="utf-8"),
                         "leaf")

        before = {path.relative_to(dest): path.read_bytes() for path in dest.rglob("*")
                  if path.is_file()}
        overwrite = run_copy(source, dest, "--max-bytes", "1000000")
        self.assertNotEqual(overwrite.returncode, 0)
        self.assertEqual(json.loads(overwrite.stdout)["status"], "refused")
        after = {path.relative_to(dest): path.read_bytes() for path in dest.rglob("*")
                 if path.is_file()}
        self.assertEqual(before, after, "an existing destination must stay byte-identical")

        inside = run_copy(source, source / "nested" / "inside", "--max-bytes", "1000000")
        self.assertNotEqual(inside.returncode, 0)
        self.assertEqual(json.loads(inside.stdout)["status"], "refused")
        self.assertFalse((source / "nested" / "inside").exists())

        dangling_dest = self.tmp / "dangling-dest"
        os.symlink(self.tmp / "missing-target", dangling_dest)
        dangling = run_copy(source, dangling_dest, "--max-bytes", "1000000")
        self.assertNotEqual(dangling.returncode, 0, "a dangling destination symlink still exists")

        missing_parent = run_copy(source, self.tmp / "no-such-parent" / "dest",
                                  "--max-bytes", "1000000")
        self.assertNotEqual(missing_parent.returncode, 0)

    def test_over_budget_and_unknown_preflight_refuse_before_write(self):
        source = self.tmp / "budget-source"
        source.mkdir()
        (source / "payload.bin").write_bytes(b"P" * 500)
        dest = self.tmp / "budget-dest"
        over = run_copy(source, dest, "--max-bytes", "100")
        self.assertEqual(over.returncode, 3, over.stderr)
        payload = json.loads(over.stdout)
        self.assertEqual(payload["status"], "budget_exceeded")
        self.assertEqual(payload["violation"]["phase"], "preflight")
        self.assertEqual(payload["violation"]["observedBytes"], 500)
        self.assertFalse(dest.exists(), "a refused preflight must not create partial evidence")

        link_source = self.tmp / "linked-source"
        os.symlink(source, link_source)
        unknown = run_copy(link_source, self.tmp / "unknown-dest", "--max-bytes", "10000")
        self.assertEqual(unknown.returncode, 4, unknown.stderr)
        self.assertEqual(json.loads(unknown.stdout)["status"], "unknown")
        self.assertFalse((self.tmp / "unknown-dest").exists())

        without_budget = run_copy(link_source, self.tmp / "linked-dest")
        self.assertEqual(without_budget.returncode, 2,
                         "the CLI must require an explicit positive --max-bytes")
        self.assertIn("--max-bytes", without_budget.stderr)
        self.assertFalse((self.tmp / "linked-dest").exists())

    def test_mid_copy_breach_retains_partial_evidence(self):
        source = self.tmp / "grow-source"
        source.mkdir()
        (source / "payload.bin").write_bytes(b"P" * 200)
        calls = []

        def under_reporting(path):
            measurement = measure(path)
            calls.append(path)
            if len(calls) == 1:
                measurement["bytes"] = 1  # passes preflight; real copy crosses the cap
            return measurement

        dest = self.tmp / "grow-dest"
        with contextlib.redirect_stdout(io.StringIO()):
            payload, code = pi_copy.perform_copy(str(source), str(dest), 100,
                                                 measure_fn=under_reporting)
        self.assertEqual(code, 3)
        self.assertEqual(payload["status"], "budget_exceeded")
        self.assertTrue(payload["partial"])
        self.assertEqual(payload["violation"]["phase"], "copy")
        self.assertTrue(dest.is_dir(), "partial evidence is retained")

    def test_final_verification_reports_violation_and_unknown_without_false_success(self):
        source = self.tmp / "final-source"
        source.mkdir()
        (source / "payload.bin").write_bytes(b"P" * 10)
        dest = self.tmp / "final-dest"
        real = measure

        def violating(path):
            calls.append(path)
            if len(calls) == 1:
                return real(path)
            return {"path": str(path), "exists": True, "type": "directory", "bytes": 9999,
                    "complete": True, "unknown": False, "reason": None, "entries": 1}

        calls = []
        with contextlib.redirect_stdout(io.StringIO()):
            payload, code = pi_copy.perform_copy(str(source), str(dest), 100, measure_fn=violating)
        self.assertEqual(code, 3)
        self.assertEqual(payload["status"], "budget_exceeded")
        self.assertEqual(payload["violation"]["phase"], "final")
        self.assertTrue(dest.is_dir())

        def unknown(path):
            calls.append(path)
            if len(calls) == 1:
                return real(path)
            return {"path": str(path), "exists": True, "type": "directory", "bytes": None,
                    "complete": False, "unknown": True, "reason": "time bound reached",
                    "entries": 2}

        calls = []
        dest_unknown = self.tmp / "final-unknown"
        with contextlib.redirect_stdout(io.StringIO()):
            payload, code = pi_copy.perform_copy(str(source), str(dest_unknown), 100, measure_fn=unknown)
        self.assertEqual(code, 4)
        self.assertEqual(payload["status"], "unknown")
        self.assertTrue(payload["unknown"])
        self.assertTrue(dest_unknown.is_dir(), "partial evidence is retained on unknown verification")

    def test_usage_validation(self):
        source = self.tmp / "usage-source"
        source.mkdir()
        (source / "x").write_text("x", encoding="utf-8")
        missing = run_copy(source, self.tmp / "missing-budget-dest")
        self.assertEqual(missing.returncode, 2)
        self.assertIn("--max-bytes", missing.stderr)
        zero = run_copy(source, self.tmp / "zero-dest", "--max-bytes", "0")
        self.assertEqual(zero.returncode, 2)
        self.assertIn("positive", zero.stderr)


if __name__ == "__main__":
    unittest.main()
