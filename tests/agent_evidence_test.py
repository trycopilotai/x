"""Bind native agent output and replay its externally observed actions."""

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parent.parent


class AgentEvidenceTest(unittest.TestCase):
    def test_bound_files_and_observed_agent_steps(self):
        manifest = json.loads((ROOT / "evidence/agent-manifest.json").read_text())
        records = [
            manifest["skill"],
            manifest["output"],
            manifest["discovery"],
            manifest["plugin"],
        ]
        records.extend(manifest["fixtures"])
        for record in records:
            with self.subTest(path=record["path"]):
                actual = hashlib.sha256(
                    (ROOT / record["path"]).read_bytes()
                ).hexdigest()
                self.assertEqual(actual, record["sha256"])
        events = [
            json.loads(line)
            for line in (ROOT / manifest["output"]["path"]).read_text().splitlines()
        ]
        completed = [e["item"] for e in events if e.get("type") == "item.completed"]
        commands = [i for i in completed if i.get("type") == "command_execution"]
        self.assertTrue(any("cat skills/x/SKILL.md" in i["command"] for i in commands))
        for fragment, output in [
            ('hello "two words"', "hello two words\n"),
            ("node bin/x.js greet ", "greetings friend\n"),
            ("make greet ARGS=friend", "greetings friend\n"),
            ("--builtin makefile --check", ""),
        ]:
            observed = [i for i in commands if fragment in i["command"]]
            self.assertTrue(observed, fragment)
            self.assertEqual(observed[0]["exit_code"], 0)
            self.assertEqual(observed[0]["aggregated_output"], output)
        self.assertTrue(any(i.get("type") == "file_change" for i in completed))
        self.assertIs(manifest["agent"]["invoked_the_skill"], True)
        self.assertIs(manifest["output"]["edited"], True)

    @unittest.skipUnless(
        shutil.which("node") and shutil.which("make"), "node and make required"
    )
    def test_recorded_configuration_dispatch_and_makefile_replay(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = Path(directory)
            shutil.copytree(ROOT / "x", fixture / "x")
            shutil.copytree(ROOT / "bin", fixture / "bin")
            shutil.copyfile(ROOT / "__main__.py", fixture / "__main__.py")
            shutil.copyfile(ROOT / "evidence/fixtures/output.toml", fixture / "x.toml")
            environment = os.environ.copy()
            environment["X_PYTHON"] = sys.executable
            for arguments, expected in [
                (["node", "bin/x.js", "hello", "two words"], "hello two words\n"),
                (["node", "bin/x.js", "greet", "friend"], "greetings friend\n"),
                (["node", "bin/x.js", "--builtin", "makefile"], ""),
                (["make", "greet", "ARGS=friend"], "greetings friend\n"),
                (["node", "bin/x.js", "--builtin", "makefile", "--check"], ""),
            ]:
                result = subprocess.run(
                    arguments,
                    cwd=fixture,
                    env=environment,
                    text=True,
                    capture_output=True,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, expected)
            self.assertEqual(
                (fixture / "Makefile").read_bytes(),
                (ROOT / "evidence/fixtures/Makefile").read_bytes(),
            )
