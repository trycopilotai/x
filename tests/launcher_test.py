"""Tests of the Node launcher, run as a real process."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import unittest


ROOT = Path(__file__).resolve().parent.parent
LAUNCHER = ROOT / "bin" / "x.js"
NODE = shutil.which("node")

CONFIG = """\
[x]
version = "{version}"

[commands.argv]
description = "Print the arguments as JSON."
run = ["{{python}}", "-c", "import json, sys; print(json.dumps(sys.argv[1:]))"]

[commands.status]
description = "Exit with the given status."
run = ["{{python}}", "-c", "import sys; sys.exit(int(sys.argv[1]))"]

[commands.stdin]
description = "Copy standard input to standard output."
run = ["{{python}}", "-c", "import sys; sys.stdout.write(sys.stdin.read())"]

[commands.env]
description = "Print one environment variable."
run = ["{{python}}", "-c", "import os, sys; print(os.environ.get(sys.argv[1], '<unset>'))"]

[commands.wait]
description = "Print this process id, then wait to be signalled."
run = ["{{python}}", "-c", "import os, time; print(os.getpid(), flush=True); time.sleep(60)"]

[commands.trap]
description = "Exit with 40 plus the number of the first signal caught."
run = ["{{python}}", "-c", '''
import signal, sys, time
for name in ("SIGTERM", "SIGHUP", "SIGINT"):
    signal.signal(getattr(signal, name), lambda number, frame: sys.exit(40 + number))
print("ready", flush=True)
time.sleep(60)
''']

[commands.die]
description = "End by the named signal."
run = ["{{python}}", "-c", '''
import os, signal, sys
number = getattr(signal, sys.argv[1])
try:
    # Python ignores some signals, SIGPIPE among them, until told not to.
    signal.signal(number, signal.SIG_DFL)
except (OSError, ValueError):
    pass
os.kill(os.getpid(), number)
''']
"""


def _gone(pid: int) -> bool:
    """True once no process has this id any more."""
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        except PermissionError:
            return False
        time.sleep(0.05)
    return False


def _version() -> str:
    return json.loads((ROOT / "package.json").read_text(encoding="utf-8"))["version"]


@unittest.skipUnless(NODE, "node is not installed")
class LauncherTestCase(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.repo = Path(temporary.name).resolve()
        (self.repo / ".git").mkdir()
        (self.repo / "x.toml").write_bytes(
            CONFIG.format(version=_version()).encode("utf-8")
        )

    # Newer Node versions let the launcher replace itself with the
    # interpreter; older ones keep it as a parent that relays the
    # exit status and signals. Setting this runs the second path on
    # any Node by hiding the first from the launcher.
    relay_through_a_child = False

    def environment(self, **extra: str) -> dict[str, str]:
        environment = dict(os.environ)
        # The interpreter running this suite is new enough by
        # construction, which keeps the result independent of
        # whatever `python3` happens to be on this machine.
        environment["X_PYTHON"] = sys.executable
        if self.relay_through_a_child:
            preload = self.repo / "no-execve.cjs"
            preload.write_text("process.execve = undefined;\n", encoding="utf-8")
            environment["NODE_OPTIONS"] = "--require " + json.dumps(str(preload))
        environment.update(extra)
        return environment

    def launch(self, *argv: str, **kwargs) -> subprocess.CompletedProcess:
        kwargs.setdefault("env", self.environment())
        return subprocess.run(
            [NODE, str(LAUNCHER), *argv],
            cwd=self.repo,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            timeout=60,
            **kwargs,
        )

    def node(self, expression: str) -> object:
        script = (
            "const m = require(" + json.dumps(str(LAUNCHER)) + ");"
            "console.log(JSON.stringify(" + expression + "));"
        )
        completed = subprocess.run(
            [NODE, "-e", script],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=60,
            check=True,
        )
        return json.loads(completed.stdout)


class PassThroughTest(LauncherTestCase):
    def test_arguments_arrive_exactly_as_given(self):
        arguments = ["plain", "two words", "", "雪", "--flag=1", "$HOME", "a;b", "--"]

        completed = self.launch("argv", *arguments)

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(json.loads(completed.stdout), arguments)

    def test_exit_status_is_the_childs(self):
        for status in (0, 1, 2, 37, 127):
            with self.subTest(status):
                self.assertEqual(self.launch("status", str(status)).returncode, status)

    def test_standard_input_reaches_the_child(self):
        completed = self.launch("stdin", input="line one\nline two\n")

        self.assertEqual(completed.stdout, "line one\nline two\n")

    def test_flags_npm_leaks_are_removed_before_the_command_runs(self):
        environment = self.environment(
            npm_config_package="@scope/outer", npm_config_call="outer"
        )

        for name in ("npm_config_package", "npm_config_call"):
            with self.subTest(name):
                completed = self.launch("env", name, env=environment)

                self.assertEqual(completed.stdout.strip(), "<unset>")

    def test_caller_directory_is_the_folder_it_was_started_from(self):
        completed = self.launch("env", "X_CALLER_DIRECTORY")

        self.assertEqual(Path(completed.stdout.strip()), self.repo)

    def test_version_flag_reports_the_package_version(self):
        self.assertEqual(self.launch("--version").stdout.strip(), _version())

    def start(self, *argv: str) -> subprocess.Popen:
        # Standard input is not a terminal here, so an interrupt sent
        # to x is one the command has not already received.
        return subprocess.Popen(
            [NODE, str(LAUNCHER), *argv],
            cwd=self.repo,
            env=self.environment(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

    def finish(self, process: subprocess.Popen) -> None:
        try:
            process.wait(timeout=30)
        finally:
            if process.poll() is None:
                process.kill()
            process.stdout.close()
            process.stderr.close()

    @unittest.skipIf(os.name == "nt", "POSIX signals")
    def test_a_signal_sent_to_x_ends_the_command_and_x_the_same_way(self):
        process = self.start("wait")
        try:
            command = int(process.stdout.readline().strip())
            started = time.monotonic()
            process.send_signal(signal.SIGTERM)
        finally:
            self.finish(process)

        # Ended by the signal itself, not by an exit status that
        # merely describes one, and without waiting out the sleep.
        self.assertEqual(process.returncode, -signal.SIGTERM)
        self.assertLess(time.monotonic() - started, 20)
        # Only x was signalled. The command is gone because x passed
        # the signal on, not because x stopped watching it.
        self.assertNotEqual(command, process.pid)
        self.assertTrue(_gone(command))

    @unittest.skipIf(os.name == "nt", "POSIX signals")
    def test_each_forwarded_signal_reaches_the_command(self):
        for name in ("SIGTERM", "SIGHUP", "SIGINT"):
            with self.subTest(name):
                number = getattr(signal, name)
                process = self.start("trap")
                try:
                    self.assertEqual(process.stdout.readline().strip(), "ready")
                    process.send_signal(number)
                finally:
                    self.finish(process)

                # The command caught the signal and chose this status,
                # which it could only do if the signal reached it.
                self.assertEqual(process.returncode, 40 + number)

    def launcher_replaces_itself(self) -> bool:
        """Whether this Node, as these tests start it, hands over by exec."""
        completed = subprocess.run(
            [NODE, "-p", "typeof process.execve"],
            env=self.environment(),
            stdout=subprocess.PIPE,
            text=True,
            timeout=60,
            check=True,
        )
        return completed.stdout.strip() == "function" and os.name != "nt"

    @unittest.skipIf(os.name == "nt", "POSIX signals")
    def test_a_command_ended_by_a_signal_ends_x_by_that_signal(self):
        for name in ("SIGTERM", "SIGKILL", "SIGSEGV"):
            with self.subTest(name):
                completed = self.launch("die", name)

                self.assertEqual(completed.returncode, -getattr(signal, name))
                self.assertEqual(completed.stderr, "")

    @unittest.skipIf(os.name == "nt", "POSIX signals")
    def test_signals_node_keeps_or_ignores_become_a_status_when_it_stays_parent(self):
        replaced = self.launcher_replaces_itself()
        for name in ("SIGUSR1", "SIGPIPE"):
            with self.subTest(name):
                number = getattr(signal, name)
                completed = self.launch("die", name)

                # Replaced by the engine, x ends by the signal like
                # any other. Left as the parent, Node cannot end by
                # these two, and reports them as 128 plus the number.
                expected = 128 + number
                if replaced:
                    expected = -number
                self.assertEqual(completed.returncode, expected)
                self.assertEqual(completed.stderr, "")


class RelayedPassThroughTest(PassThroughTest):
    relay_through_a_child = True

    def test_the_relay_path_is_the_one_under_test(self):
        script = "console.log(typeof process.execve)"
        completed = subprocess.run(
            [NODE, "-e", script],
            env=self.environment(),
            stdout=subprocess.PIPE,
            text=True,
            timeout=60,
            check=True,
        )

        self.assertEqual(completed.stdout.strip(), "undefined")


class InterpreterDiscoveryTest(LauncherTestCase):
    def test_posix_candidates_try_plain_then_versioned_names(self):
        commands = [
            entry["command"] for entry in self.node("m.candidates('linux', {})")
        ]

        self.assertEqual(
            commands,
            ["python3", "python3.13", "python3.12", "python3.11", "python3.14", "python"],
        )

    def test_windows_candidates_start_with_the_py_launcher(self):
        entries = self.node("m.candidates('win32', {})")

        self.assertEqual(entries[0], {"command": "py", "args": ["-3"]})
        self.assertEqual(
            [entry["command"] for entry in entries], ["py", "python", "python3"]
        )

    def test_an_explicit_interpreter_is_the_only_candidate(self):
        entries = self.node("m.candidates('linux', {X_PYTHON: '/opt/py'})")

        self.assertEqual(
            entries, [{"command": "/opt/py", "args": [], "explicit": True}]
        )

    def test_minimum_version(self):
        for version, expected in (
            ("3.9.6", False),
            ("3.10.14", False),
            ("3.11.0", True),
            ("3.13.2", True),
            ("4.0.0", True),
        ):
            with self.subTest(version):
                self.assertIs(
                    self.node("m.supported(" + json.dumps(version) + ")"), expected
                )

    def test_a_missing_interpreter_exits_127_and_says_what_to_do(self):
        missing = str(self.repo / "no-such-python")

        completed = self.launch("--version", env=self.environment(X_PYTHON=missing))

        self.assertEqual(completed.returncode, 127)
        self.assertEqual(completed.stdout, "")
        self.assertIn("needs Python 3.11 or newer", completed.stderr)
        self.assertIn(missing + " (not found)", completed.stderr)
        self.assertIn("X_PYTHON=/path/to/python", completed.stderr)

    @unittest.skipIf(os.name == "nt", "the stub interpreter is a shell script")
    def test_an_interpreter_that_is_too_old_is_named_with_its_version(self):
        stub = self.repo / "old-python"
        stub.write_text("#!/bin/sh\necho 3.9.6\necho /somewhere/old-python\n")
        stub.chmod(stub.stat().st_mode | stat.S_IXUSR)

        completed = self.launch("--version", env=self.environment(X_PYTHON=str(stub)))

        self.assertEqual(completed.returncode, 127)
        self.assertIn(
            str(stub) + " (3.9.6 at /somewhere/old-python)", completed.stderr
        )


if __name__ == "__main__":
    unittest.main()
