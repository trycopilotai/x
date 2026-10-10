"""Behavioral tests for the registry, dispatch and process handlers."""

from __future__ import annotations

from pathlib import Path, PureWindowsPath
import unittest

from tests.support import context as _context
from x.dispatch import ControlPlane
from x.execution import bash_handler, executable_handler, npx_handler
from x.help import HelpFormat, render_help
from x.model import CommandSpec, FlagSpec, ProcessRequest, SpecError
from x.wsl import WslOptions, windows_to_wsl_path, wsl_bash_request


def _success(_invocation) -> int:
    return 0


class RegistryTest(unittest.TestCase):
    def test_rejects_duplicate_alias_after_normalization(self):
        def normalize(value: str) -> str:
            return value.replace("-", "_").lower()

        commands = (
            CommandSpec("first", "First.", _success, aliases=("same-name",)),
            CommandSpec("second", "Second.", _success, aliases=("same_name",)),
        )

        with self.assertRaisesRegex(
            SpecError, "the name 'same_name' is claimed by both first and second"
        ):
            ControlPlane(commands=commands, normalizer=normalize)

    def test_rejects_duplicate_platform_handlers(self):
        commands = (
            CommandSpec(
                "help",
                "Help.",
                _success,
                platform_handlers=(
                    ("windows", _success),
                    ("windows", _success),
                ),
            ),
        )

        with self.assertRaisesRegex(SpecError, "duplicate platform handler"):
            ControlPlane(commands=commands)

    def test_preserves_order_and_resolves_alias(self):
        seen = []

        def handler(invocation) -> int:
            seen.append((invocation.name, invocation.argv))
            return 7

        commands = (
            CommandSpec("help", "Help.", _success),
            CommandSpec("build", "Build.", handler, aliases=("b",)),
        )
        plane = ControlPlane(commands=commands)
        context = _context(Path.cwd())

        self.assertEqual(plane.dispatch(("b", "one", "two"), context), 7)
        self.assertEqual(seen, [("build", ("one", "two"))])
        self.assertEqual(
            [command.name for command in plane.commands], ["help", "build"]
        )

    def test_parses_only_leading_selected_global_flags(self):
        seen = []

        def handler(invocation) -> int:
            seen.append((dict(invocation.flags), invocation.argv))
            return 0

        plane = ControlPlane(
            commands=(CommandSpec("help", "Help.", handler),),
            flags=(FlagSpec(("-s", "--silent"), "silent"),),
        )
        context = _context(Path.cwd())

        self.assertEqual(plane.dispatch(("--silent", "help", "-s"), context), 0)
        self.assertEqual(seen, [({"silent": "1"}, ("-s",))])

    def test_missing_global_flag_value_is_invalid_input(self):
        plane = ControlPlane(
            commands=(CommandSpec("help", "Help.", _success),),
            flags=(FlagSpec(("--profile",), "profile", takes_value=True),),
        )
        context = _context(Path.cwd())

        self.assertEqual(plane.dispatch(("--profile",), context), 2)
        self.assertEqual(
            context.stderr.getvalue(), "x: missing value for global flag --profile\n"
        )

    def test_unknown_command_uses_stderr_and_exit_two(self):
        plane = ControlPlane(
            commands=(CommandSpec("help", "Help.", _success),),
            unknown_command_message="bad: {name}",
        )
        context = _context(Path.cwd())

        self.assertEqual(plane.dispatch(("missing",), context), 2)
        self.assertEqual(context.stdout.getvalue(), "")
        self.assertEqual(context.stderr.getvalue(), "bad: missing\n")

    def test_unknown_handler_receives_the_argv_as_typed(self):
        seen = []

        def unknown(invocation) -> int:
            seen.append((invocation.name, invocation.argv, dict(invocation.flags)))
            return 5

        plane = ControlPlane(
            commands=(CommandSpec("help", "Help.", _success),),
            flags=(FlagSpec(("-s", "--silent"), "silent"),),
            unknown=unknown,
        )
        context = _context(Path.cwd())

        self.assertEqual(plane.dispatch(("-s", "legacy", "one"), context), 5)
        # The flag is not parsed away: whatever takes over an
        # undeclared command gets to read the whole line itself.
        self.assertEqual(seen, [("legacy", ("-s", "legacy", "one"), {})])
        self.assertEqual(context.stderr.getvalue(), "")

    def test_platform_restriction_returns_missing_backend_status(self):
        plane = ControlPlane(
            commands=(
                CommandSpec(
                    "help",
                    "Help.",
                    _success,
                    platforms=("linux",),
                ),
            )
        )
        context = _context(Path.cwd(), platform="windows")

        self.assertEqual(plane.dispatch(("help",), context), 127)
        self.assertIn("unsupported platform: windows", context.stderr.getvalue())

    def test_selects_platform_specific_handler(self):
        seen = []

        def generic(_invocation) -> int:
            seen.append("generic")
            return 0

        def windows(_invocation) -> int:
            seen.append("windows")
            return 0

        plane = ControlPlane(
            commands=(
                CommandSpec(
                    "help",
                    "Help.",
                    generic,
                    platform_handlers=(("windows", windows),),
                ),
            )
        )
        context = _context(Path.cwd(), platform="windows")

        self.assertEqual(plane.dispatch(("help",), context), 0)
        self.assertEqual(seen, ["windows"])


class HelpTest(unittest.TestCase):
    def test_rows_are_exact_ordered_and_skip_hidden_commands(self):
        commands = (
            CommandSpec("help", "Print help.", _success),
            CommandSpec("build", "Build all.", _success, aliases=("b",)),
            CommandSpec("internal", "Not listed.", _success, hidden=True),
        )

        self.assertEqual(
            render_help(commands),
            "help\tPrint help.\nbuild\tBuild all.\n",
        )
        self.assertEqual(
            render_help(commands, HelpFormat(include_aliases=True)),
            "help\tPrint help.\nbuild (b)\tBuild all.\n",
        )
        self.assertEqual(
            render_help(commands, HelpFormat(include_hidden=True)),
            "help\tPrint help.\nbuild\tBuild all.\ninternal\tNot listed.\n",
        )


class ExecutionTest(unittest.TestCase):
    def test_executable_handler_preserves_argv_cwd_streams_and_environment(self):
        requests: list[ProcessRequest] = []

        def runner(request: ProcessRequest) -> int:
            requests.append(request)
            return 23

        root = Path.cwd()
        context = _context(
            root,
            environ={"KEEP": "yes", "REMOVE": "value"},
            runner=runner,
        )
        plane = ControlPlane(
            commands=(
                CommandSpec(
                    "run",
                    "Run.",
                    executable_handler(
                        ("tool", "fixed"),
                        remove_env=("REMOVE",),
                        extra_env={"ADD": "value"},
                    ),
                ),
            ),
            default_command="run",
        )

        self.assertEqual(plane.dispatch(("run", "space value", "雪"), context), 23)
        request = requests[0]
        self.assertEqual(request.argv, ("tool", "fixed", "space value", "雪"))
        self.assertEqual(request.cwd, root)
        self.assertEqual(dict(request.environ), {"KEEP": "yes", "ADD": "value"})
        self.assertIs(request.stdin, context.stdin)
        self.assertIs(request.stdout, context.stdout)
        self.assertIs(request.stderr, context.stderr)

    def test_npx_handler_removes_the_flags_npm_leaks_to_children(self):
        requests = []

        def runner(request: ProcessRequest) -> int:
            requests.append(request)
            return 0

        context = _context(
            Path.cwd(),
            environ={
                "npm_config_package": "wrong",
                "npm_config_call": "wrong",
                "KEEP": "yes",
            },
            runner=runner,
        )
        plane = ControlPlane(
            commands=(
                CommandSpec(
                    "agent",
                    "Agent.",
                    npx_handler(("--yes", "package")),
                ),
            ),
            default_command="agent",
        )

        self.assertEqual(plane.dispatch(("agent", "arg"), context), 0)
        self.assertEqual(requests[0].argv, ("npx", "--yes", "package", "arg"))
        self.assertEqual(dict(requests[0].environ), {"KEEP": "yes"})

    def test_missing_executable_returns_127(self):
        def runner(_request: ProcessRequest) -> int:
            raise FileNotFoundError

        context = _context(Path.cwd(), runner=runner)
        plane = ControlPlane(
            commands=(
                CommandSpec(
                    "run",
                    "Run.",
                    executable_handler(("missing-tool",)),
                ),
            ),
            default_command="run",
        )

        self.assertEqual(plane.dispatch(("run",), context), 127)
        self.assertEqual(
            context.stderr.getvalue(),
            "x: executable not found: missing-tool\n",
        )

    def test_a_program_that_cannot_be_started_is_reported_in_one_line(self):
        cases = (
            (PermissionError(13, "Permission denied"), "permission denied"),
            (OSError(8, "Exec format error"), "not a program"),
            (OSError(26, "Text file busy"), "Text file busy"),
        )
        for error, expected in cases:
            with self.subTest(expected):

                def runner(_request: ProcessRequest, error=error) -> int:
                    raise error

                context = _context(Path.cwd(), runner=runner)
                plane = ControlPlane(
                    commands=(
                        CommandSpec("run", "Run.", executable_handler(("tools/go.sh",))),
                    ),
                    default_command="run",
                )

                self.assertEqual(plane.dispatch(("run",), context), 126)
                message = context.stderr.getvalue()
                self.assertTrue(message.startswith("x: cannot run tools/go.sh: "))
                self.assertIn(expected, message)
                self.assertEqual(message.count("\n"), 1)

    def test_child_exit_127_does_not_report_missing_executable(self):
        def runner(_request: ProcessRequest) -> int:
            return 127

        context = _context(Path.cwd(), runner=runner)
        plane = ControlPlane(
            commands=(
                CommandSpec(
                    "run",
                    "Run.",
                    executable_handler(("available-tool",)),
                ),
            ),
            default_command="run",
        )

        self.assertEqual(plane.dispatch(("run",), context), 127)
        self.assertEqual(context.stderr.getvalue(), "")

    def test_bash_handler_puts_fixed_arguments_before_the_callers(self):
        requests = []

        def runner(request: ProcessRequest) -> int:
            requests.append(request)
            return 0

        context = _context(Path.cwd(), runner=runner)
        plane = ControlPlane(
            commands=(
                CommandSpec(
                    "legacy",
                    "Legacy.",
                    bash_handler("tools/legacy.sh", ("--silent", "legacy")),
                ),
            ),
            default_command="legacy",
        )

        self.assertEqual(plane.dispatch(("legacy", "one"), context), 0)
        self.assertEqual(
            requests[0].argv,
            ("bash", "tools/legacy.sh", "--silent", "legacy", "one"),
        )

    def test_windows_bash_handler_uses_wsl_exec_without_shell_string(self):
        requests = []

        def runner(request: ProcessRequest) -> int:
            requests.append(request)
            return 0

        context = _context(
            Path("C:/work/repo"),
            platform="windows",
            runner=runner,
        )
        plane = ControlPlane(
            commands=(
                CommandSpec(
                    "legacy",
                    "Legacy.",
                    bash_handler(
                        "tools/legacy.sh",
                        options=WslOptions(distro="Ubuntu"),
                    ),
                ),
            ),
            default_command="legacy",
        )

        self.assertEqual(plane.dispatch(("legacy", "space value", "$HOME"), context), 0)
        self.assertEqual(
            requests[0].argv,
            (
                "wsl.exe",
                "-d",
                "Ubuntu",
                "--cd",
                "/mnt/c/work/repo",
                "--exec",
                "bash",
                "tools/legacy.sh",
                "space value",
                "$HOME",
            ),
        )


class WindowsPathTest(unittest.TestCase):
    def test_wsl_starts_in_the_translated_folder_it_is_given(self):
        context = _context(Path("C:/work/repo"), platform="windows")

        at_root = wsl_bash_request(context, "go.sh", ("a",), WslOptions())
        in_folder = wsl_bash_request(
            context, "go.sh", ("a",), WslOptions(), PureWindowsPath(r"C:\work\repo\web")
        )

        self.assertEqual(at_root.argv[:3], ("wsl.exe", "--cd", "/mnt/c/work/repo"))
        self.assertEqual(
            in_folder.argv, ("wsl.exe", "--cd", "/mnt/c/work/repo/web", "--exec", "bash", "go.sh", "a")
        )

    def test_windows_path_conversion(self):
        self.assertEqual(
            windows_to_wsl_path(r"C:\work\Team Space\repo"),
            (None, "/mnt/c/work/Team Space/repo"),
        )
        self.assertEqual(
            windows_to_wsl_path(r"\\wsl.localhost\Ubuntu\srv\repo"),
            ("Ubuntu", "/srv/repo"),
        )


if __name__ == "__main__":
    unittest.main()
