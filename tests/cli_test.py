"""End-to-end tests of the command line, run in-process."""

from __future__ import annotations

import json
import os
from pathlib import Path, PureWindowsPath
import shutil
import stat
import subprocess
import sys
import unittest

from tests.support import RepoTestCase
from x import __version__
from x.makefile import BANNER
from x.wsl import windows_to_wsl_path


ROOT = '[x]\nversion = "' + __version__ + '"\n'


class HandlerTest(RepoTestCase):
    def test_run_appends_the_callers_arguments(self):
        self.write(
            "x.toml",
            ROOT + '[commands.go]\ndescription = "Go."\nrun = ["tool", "fixed"]\n',
        )

        result = self.run_x("go", "space value", "雪", status=23)

        self.assertEqual(result.status, 23)
        self.assertEqual(result.requests[0].argv, ("tool", "fixed", "space value", "雪"))
        self.assertEqual(result.requests[0].cwd, self.root)
        self.assertEqual(result.stderr, "")

    def test_bash_runs_the_script_with_its_fixed_arguments_first(self):
        self.write(
            "x.toml",
            ROOT
            + '[commands.go]\ndescription = "Go."\n'
            + 'bash = "tools/go.sh"\nargs = ["--fast"]\n',
        )

        result = self.run_x("go", "now")

        self.assertEqual(
            result.requests[0].argv, ("bash", "tools/go.sh", "--fast", "now")
        )

    def test_bash_goes_through_wsl_on_windows(self):
        self.write(
            "x.toml", ROOT + '[commands.go]\ndescription = "Go."\nbash = "tools/go.sh"\n'
        )

        result = self.run_x("go", "now", platform="windows")

        argv = result.requests[0].argv
        self.assertEqual(argv[0], "wsl.exe")
        self.assertEqual(argv[-4:], ("--exec", "bash", "tools/go.sh", "now"))

    def test_bash_on_windows_starts_in_the_commands_own_folder(self):
        self.write(
            "x.toml",
            ROOT + '[commands.pkg]\ndescription = "P."\nbash = "go.sh"\ncwd = "pkg"\n',
        )
        self.write(
            "web/x.toml", '[commands.serve]\ndescription = "S."\nbash = "serve.sh"\n'
        )

        folder = self.run_x("serve", cwd="web", platform="windows").requests[0].argv
        relative = self.run_x("pkg", platform="windows").requests[0].argv

        # The folder after --cd is where the relative script is found.
        # It is the command's folder as WSL names it, which on a
        # Windows host is not the path Windows uses.
        def as_wsl(path: Path) -> str:
            return windows_to_wsl_path(str(PureWindowsPath(str(path))))[1]

        self.assertEqual(folder[folder.index("--cd") + 1], as_wsl(self.root / "web"))
        self.assertEqual(relative[relative.index("--cd") + 1], as_wsl(self.root / "pkg"))

    def test_npx_drops_the_flags_npm_leaks_to_children(self):
        self.write(
            "x.toml", ROOT + '[commands.go]\ndescription = "Go."\nnpx = ["--yes", "pkg"]\n'
        )

        result = self.run_x(
            "go",
            "arg",
            environ={"npm_config_package": "wrong", "npm_config_call": "wrong"},
        )

        self.assertEqual(result.requests[0].argv, ("npx", "--yes", "pkg", "arg"))
        self.assertNotIn("npm_config_package", result.requests[0].environ)
        self.assertNotIn("npm_config_call", result.requests[0].environ)

    def test_bazel_runs_through_bazelisk(self):
        self.write(
            "x.toml",
            ROOT + '[commands.go]\ndescription = "Go."\nbazel = ["build", "//..."]\n',
        )

        result = self.run_x("go", "--keep_going")

        self.assertEqual(
            result.requests[0].argv,
            ("npx", "--yes", "@bazel/bazelisk", "build", "//...", "--keep_going"),
        )

    def test_platform_table_replaces_the_handler_on_that_platform(self):
        self.write(
            "x.toml",
            ROOT
            + '[commands.go]\ndescription = "Go."\nrun = ["unix-tool"]\n'
            + '[commands.go.windows]\nrun = ["windows-tool"]\n',
        )

        self.assertEqual(self.run_x("go").requests[0].argv, ("unix-tool",))
        self.assertEqual(
            self.run_x("go", platform="windows").requests[0].argv, ("windows-tool",)
        )

    def test_unsupported_platform_is_refused_before_anything_runs(self):
        self.write(
            "x.toml",
            ROOT + '[commands.go]\ndescription = "Go."\nrun = ["t"]\nplatforms = ["macos"]\n',
        )

        result = self.run_x("go")

        self.assertEqual(result.status, 127)
        self.assertEqual(result.requests, [])
        self.assertIn("unsupported platform: linux", result.stderr)

    def test_environment_is_set_and_unset_per_command(self):
        self.write(
            "x.toml",
            ROOT
            + '[commands.go]\ndescription = "Go."\nrun = ["t"]\n'
            + 'unset_env = ["DROP"]\n'
            + '[commands.go.env]\nMODE = "fast"\nHOME_OF_IT = "{root}/data"\n',
        )

        result = self.run_x("go", environ={"DROP": "1", "KEEP": "1"})

        environment = result.requests[0].environ
        self.assertEqual(environment["MODE"], "fast")
        self.assertEqual(environment["HOME_OF_IT"], str(self.root) + "/data")
        self.assertEqual(environment["KEEP"], "1")
        self.assertNotIn("DROP", environment)

    def test_flags_placeholder_has_no_meaning_in_an_environment_value(self):
        self.write(
            "x.toml",
            ROOT
            + '[flags.quiet]\nnames = ["-q"]\nforward = ["--quiet"]\n'
            + '[commands.go]\ndescription = "Go."\nrun = ["t", "{flags}"]\n'
            + '[commands.go.env]\nLITERAL = "{flags}"\n',
        )

        result = self.run_x("-q", "go")

        self.assertEqual(result.requests[0].argv, ("t", "--quiet"))
        self.assertEqual(result.requests[0].environ["LITERAL"], "{flags}")

    def test_caller_directory_is_exported_under_every_configured_name(self):
        self.write(
            "x.toml",
            '[x]\nversion = "' + __version__ + '"\ncaller_env = ["A_DIR", "B_DIR"]\n'
            '[commands.go]\ndescription = "Go."\nrun = ["t"]\ncwd = "root"\n',
        )
        (self.root / "deep").mkdir()

        result = self.run_x("go", cwd="deep")

        environment = result.requests[0].environ
        self.assertEqual(environment["A_DIR"], str(self.root / "deep"))
        self.assertEqual(environment["B_DIR"], str(self.root / "deep"))
        self.assertNotIn("X_CALLER_DIRECTORY", environment)

    def test_working_directory_policies(self):
        self.write(
            "x.toml",
            ROOT
            + '[commands.at-root]\ndescription = "R."\nrun = ["t"]\ncwd = "root"\n'
            + '[commands.at-caller]\ndescription = "C."\nrun = ["t"]\ncwd = "caller"\n'
            + '[commands.at-path]\ndescription = "P."\nrun = ["t"]\ncwd = "pkg"\n',
        )
        (self.root / "pkg" / "deep").mkdir(parents=True)

        def cwd_of(name: str) -> Path:
            return self.run_x(name, cwd="pkg/deep").requests[0].cwd

        self.assertEqual(cwd_of("at-root"), self.root)
        self.assertEqual(cwd_of("at-caller"), self.root / "pkg" / "deep")
        self.assertEqual(cwd_of("at-path"), self.root / "pkg")


@unittest.skipIf(os.name == "nt", "POSIX executables and permissions")
class CannotRunTest(RepoTestCase):
    """What x says, in one line, when a program cannot be started."""

    def configure(self, extra: str = "") -> None:
        self.write(
            "x.toml",
            ROOT + '[commands.go]\ndescription = "Go."\nrun = ["tools/go.sh"]\n' + extra,
        )

    def script(self, text: str, mode: int = 0o755) -> Path:
        path = self.write("tools/go.sh", text)
        path.chmod(mode)
        return path

    def assert_one_line(self, result, status: int, text: str) -> None:
        self.assertEqual(result.status, status, result.stderr)
        self.assertEqual(result.stderr, "x: " + text + "\n")

    def test_a_script_that_runs_returns_its_own_status(self):
        self.configure()
        self.script("#!/bin/sh\nexit 7\n")

        self.assertEqual(self.run_real("go").status, 7)

    def test_missing_program(self):
        self.configure()

        self.assert_one_line(
            self.run_real("go"), 127, "executable not found: tools/go.sh"
        )

    def test_not_executable(self):
        self.configure()
        self.script("#!/bin/sh\nexit 0\n", mode=0o644)

        self.assert_one_line(
            self.run_real("go"),
            126,
            "cannot run tools/go.sh: permission denied (is it executable? try chmod +x)",
        )

    def test_no_interpreter_line(self):
        self.configure()
        self.script("exit 0\n")

        self.assert_one_line(
            self.run_real("go"),
            126,
            "cannot run tools/go.sh: not a program (a script needs a #! first line)",
        )

    def test_interpreter_line_names_a_program_that_is_missing(self):
        self.configure()
        self.script("#!/no/such/interpreter\nexit 0\n")

        self.assert_one_line(
            self.run_real("go"),
            126,
            "cannot run tools/go.sh: the interpreter on its #! line was not found",
        )

    def test_a_folder_where_the_program_should_be(self):
        self.configure()
        (self.root / "tools" / "go.sh").mkdir(parents=True)

        self.assert_one_line(
            self.run_real("go"), 126, "cannot run tools/go.sh: it is a folder"
        )

    def test_an_argument_no_process_can_be_given(self):
        self.write(
            "x.toml",
            ROOT + '[commands.go]\ndescription = "Go."\nrun = ["sh", "-c", "exit 0", "a\\u0000b"]\n',
        )

        result = self.run_real("go")

        self.assertEqual(result.status, 126)
        self.assertTrue(result.stderr.startswith("x: cannot run sh: "))
        self.assertEqual(result.stderr.count("\n"), 1)

    def test_missing_working_directory(self):
        self.configure('cwd = "nowhere"\n')
        self.script("#!/bin/sh\nexit 0\n")

        self.assert_one_line(
            self.run_real("go"),
            127,
            "working directory not found: " + str(self.root / "nowhere"),
        )


class DelegateTest(RepoTestCase):
    CONFIG = (
        '[x]\nversion = "' + __version__ + '"\nnaming = "snake"\n'
        '[flags.silent]\nnames = ["-s", "--silent"]\nforward = ["--silent"]\n'
        '[defaults]\nbash = "cli/main.sh"\nargs = ["{flags}", "{name}"]\n'
        '[defaults.windows]\nbash = "cli/main-wsl.sh"\nargs = ["{flags}", "{name}"]\n'
        '[unknown]\nbash = "cli/main.sh"\n'
        '[commands.report_serve]\ndescription = "Serve the report."\naliases = ["rs"]\n'
        '[commands.own]\ndescription = "Has its own handler."\nrun = ["tool"]\n'
    )

    def test_command_without_a_handler_uses_the_default_one(self):
        self.write("x.toml", self.CONFIG)

        result = self.run_x("report_serve", "8080")

        self.assertEqual(
            result.requests[0].argv,
            ("bash", "cli/main.sh", "report_serve", "8080"),
        )

    def test_alias_and_other_spellings_reach_the_canonical_name(self):
        self.write("x.toml", self.CONFIG)

        for spelling in ("rs", "Report.Serve", "reportServe", "report-serve"):
            with self.subTest(spelling):
                result = self.run_x(spelling)

                self.assertEqual(
                    result.requests[0].argv, ("bash", "cli/main.sh", "report_serve")
                )

    def test_global_flag_is_restated_where_the_template_says(self):
        self.write("x.toml", self.CONFIG)

        result = self.run_x("-s", "report_serve", "-s")

        self.assertEqual(
            result.requests[0].argv,
            ("bash", "cli/main.sh", "--silent", "report_serve", "-s"),
        )

    def test_double_dash_ends_the_global_flags(self):
        self.write("x.toml", self.CONFIG)

        result = self.run_x("--", "own", "-s")

        self.assertEqual(result.requests[0].argv, ("tool", "-s"))

    def test_default_handler_follows_the_platform(self):
        self.write("x.toml", self.CONFIG)

        result = self.run_x("report_serve", platform="windows")

        self.assertIn("cli/main-wsl.sh", result.requests[0].argv)

    def test_undeclared_command_is_handed_over_whole(self):
        self.write("x.toml", self.CONFIG)

        result = self.run_x("-s", "not_declared", "one", status=9)

        self.assertEqual(result.status, 9)
        self.assertEqual(
            result.requests[0].argv,
            ("bash", "cli/main.sh", "-s", "not_declared", "one"),
        )
        self.assertEqual(result.stderr, "")

    def test_without_an_unknown_table_an_undeclared_command_is_an_error(self):
        self.write(
            "x.toml", ROOT + '[commands.go]\ndescription = "Go."\nrun = ["t"]\n'
        )

        result = self.run_x("nope")

        self.assertEqual(result.status, 2)
        self.assertEqual(result.stderr, "x: unknown command: nope\n")
        self.assertEqual(result.requests, [])

    def test_command_without_any_handler_is_a_config_error(self):
        self.write("x.toml", ROOT + '[commands.go]\ndescription = "Go."\n')

        result = self.run_x("go")

        self.assertEqual(result.status, 2)
        self.assertIn("needs one of run, bash, npx, bazel, builtin", result.stderr)


class FolderTest(RepoTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.write(
            "x.toml",
            ROOT
            + '[commands.build]\ndescription = "Build all."\nrun = ["root-build"]\n'
            + 'aliases = ["b"]\n'
            + '[commands.test]\ndescription = "Test all."\nrun = ["root-test"]\n'
            + 'aliases = ["t"]\n',
        )
        self.write(
            "web/x.toml",
            '[commands.build]\ndescription = "Build the site."\nrun = ["web-build"]\n'
            '[commands.dev]\ndescription = "Serve the site."\nrun = ["web-dev"]\n'
            'aliases = ["t"]\n',
        )
        (self.root / "web" / "src").mkdir()

    def test_folder_command_replaces_the_root_one_below_its_folder(self):
        self.assertEqual(self.run_x("build").requests[0].argv, ("root-build",))
        self.assertEqual(
            self.run_x("build", cwd="web/src").requests[0].argv, ("web-build",)
        )

    def test_folder_command_runs_in_its_folder(self):
        result = self.run_x("dev", cwd="web/src")

        self.assertEqual(result.requests[0].cwd, self.root / "web")

    def test_root_command_still_runs_at_the_root(self):
        result = self.run_x("test", cwd="web/src")

        self.assertEqual(result.requests[0].argv, ("root-test",))
        self.assertEqual(result.requests[0].cwd, self.root)

    def test_folder_alias_is_taken_from_the_root_command(self):
        self.assertEqual(self.run_x("t").requests[0].argv, ("root-test",))
        self.assertEqual(self.run_x("t", cwd="web").requests[0].argv, ("web-dev",))

    def test_replaced_command_loses_its_aliases_too(self):
        result = self.run_x("b", cwd="web")

        self.assertEqual(result.status, 2)
        self.assertEqual(result.stderr, "x: unknown command: b\n")

    def test_two_commands_in_one_folder_file_cannot_share_a_name(self):
        self.write(
            "web/x.toml",
            '[commands.build]\ndescription = "B."\nrun = ["b"]\n'
            '[commands.dev]\ndescription = "D."\nrun = ["d"]\naliases = ["build"]\n',
        )

        result = self.run_x("help", cwd="web")

        # Replacing is something a nearer file does to a farther one.
        # Inside one file it is a mistake, and the file is named.
        self.assertEqual(result.status, 2)
        self.assertIn("web/x.toml", result.stderr.replace(os.sep, "/"))
        self.assertIn("the name 'build' is claimed by both build and dev", result.stderr)
        self.assertEqual(self.run_x("help").status, 0)

    def test_folder_commands_do_not_exist_outside_their_folder(self):
        result = self.run_x("dev")

        self.assertEqual(result.status, 2)

    def test_help_lists_what_is_visible_from_here(self):
        self.assertEqual(
            self.run_x("help").stdout,
            "build\tBuild all.\ntest\tTest all.\n",
        )
        self.assertEqual(
            self.run_x("help", cwd="web").stdout,
            "test\tTest all.\nbuild\tBuild the site.\ndev\tServe the site.\n",
        )


class BuiltinTest(RepoTestCase):
    def test_without_a_config_the_builtins_are_listed(self):
        result = self.run_x("help")

        self.assertEqual(result.status, 0)
        self.assertEqual(
            [line.split("\t")[0] for line in result.stdout.splitlines()],
            ["help", "version", "check", "init", "makefile", "resume"],
        )
        self.assertIn("x: no x.toml here", result.stderr)

    def test_no_arguments_runs_the_default_command(self):
        self.write(
            "x.toml",
            '[x]\nversion = "' + __version__ + '"\ndefault = "go"\n'
            '[commands.go]\ndescription = "Go."\nrun = ["t"]\n',
        )

        self.assertEqual(self.run_x().requests[0].argv, ("t",))

    def test_help_hides_builtins_and_hidden_commands_until_asked(self):
        self.write(
            "x.toml",
            ROOT
            + '[commands.go]\ndescription = "Go."\nrun = ["t"]\n'
            + '[commands.secret]\ndescription = "Hidden."\nrun = ["s"]\nhidden = true\n',
        )

        self.assertEqual(self.run_x("help").stdout, "go\tGo.\n")
        listed = [
            line.split("\t")[0] for line in self.run_x("help", "--all").stdout.splitlines()
        ]
        self.assertEqual(
            listed,
            ["go", "secret", "help", "version", "check", "init", "makefile", "resume"],
        )
        self.assertEqual(self.run_x("secret").requests[0].argv, ("s",))

    def test_help_for_one_command_and_for_a_missing_one(self):
        self.write(
            "x.toml", ROOT + '[commands.go]\ndescription = "Go."\nrun = ["t"]\naliases = ["g"]\n'
        )

        self.assertEqual(self.run_x("help", "g").stdout, "go\tGo.\n")
        missing = self.run_x("help", "nope")
        self.assertEqual(missing.status, 2)
        self.assertEqual(missing.stderr, "x: unknown command: nope\n")

    def test_help_json_describes_every_command(self):
        self.write(
            "x.toml",
            ROOT
            + '[commands.go]\ndescription = "Go."\nrun = ["t"]\naliases = ["g"]\n'
            + "[make]\n",
        )
        self.write("sub/x.toml", '[commands.local]\ndescription = "L."\nbash = "l.sh"\n')

        document = json.loads(self.run_x("help", "--json", cwd="sub").stdout)

        self.assertEqual(document["schema"], 1)
        self.assertEqual(document["version"], __version__)
        self.assertEqual(document["pinned"], __version__)
        self.assertEqual(document["root"], str(self.root))
        self.assertEqual(document["configs"], ["x.toml", "sub/x.toml"])
        by_name = {command["name"]: command for command in document["commands"]}
        self.assertEqual(by_name["go"]["aliases"], ["g"])
        self.assertEqual(by_name["go"]["kind"], "run")
        self.assertEqual(by_name["go"]["source"], "x.toml")
        self.assertTrue(by_name["go"]["make"])
        self.assertEqual(by_name["local"]["source"], "sub/x.toml")
        self.assertFalse(by_name["local"]["make"])
        self.assertTrue(by_name["version"]["builtin"])
        self.assertTrue(by_name["version"]["hidden"])
        self.assertFalse(by_name["go"]["builtin"])

    def test_help_json_gives_every_entry_the_same_keys(self):
        self.write(
            "x.toml", ROOT + '[commands.go]\ndescription = "Go."\nrun = ["t"]\n'
        )

        document = json.loads(self.run_x("help", "--json").stdout)

        keys = {frozenset(command) for command in document["commands"]}
        self.assertEqual(len(keys), 1)
        builtin = [c for c in document["commands"] if c["name"] == "check"][0]
        self.assertEqual(builtin["kind"], "builtin")
        self.assertIsNone(builtin["cwd"])
        self.assertIsNone(builtin["source"])
        self.assertIs(builtin["make"], False)
        self.assertIs(builtin["builtin"], True)

    def test_repository_command_wins_over_a_builtin_of_the_same_name(self):
        self.write(
            "x.toml", ROOT + '[commands.help]\ndescription = "Ours."\nrun = ["our-help"]\n'
        )

        self.assertEqual(self.run_x("help").requests[0].argv, ("our-help",))
        escaped = self.run_x("--builtin", "help")
        self.assertEqual(escaped.requests, [])
        self.assertEqual(escaped.stdout, "help\tOurs.\n")

    def test_builtin_handler_lets_a_repository_list_a_builtin_as_its_own(self):
        self.write(
            "x.toml",
            ROOT + '[commands.about]\ndescription = "Engine version."\nbuiltin = "version"\n',
        )

        self.assertEqual(self.run_x("about").stdout, __version__ + "\n")
        self.assertEqual(self.run_x("help").stdout, "about\tEngine version.\n")

    def test_unknown_builtin_names_are_errors(self):
        self.write(
            "x.toml", ROOT + '[commands.a]\ndescription = "A."\nbuiltin = "nope"\n'
        )

        self.assertIn("no built-in named 'nope'", self.run_x("a").stderr)
        (self.root / "x.toml").unlink()
        result = self.run_x("--builtin", "nope")
        self.assertEqual(result.status, 2)
        self.assertIn("no built-in named 'nope'", result.stderr)

    def test_version_flag_and_help_flag(self):
        self.assertEqual(self.run_x("--version").stdout, __version__ + "\n")
        self.assertEqual(
            self.run_x("--help").stdout.splitlines()[0],
            "help\tList commands, or describe one.",
        )

    def test_version_is_answered_even_when_the_config_does_not_load(self):
        self.write("x.toml", "[x\n")

        self.assertEqual(self.run_x("help").status, 2)
        flagged = self.run_x("--version")
        named = self.run_x("--builtin", "version")

        self.assertEqual((flagged.status, flagged.stdout), (0, __version__ + "\n"))
        self.assertEqual((named.status, named.stdout), (0, __version__ + "\n"))

    def test_errors_found_while_building_the_registry_name_the_root_file(self):
        self.write(
            "x.toml",
            ROOT
            + '[commands.a]\ndescription = "A."\nrun = ["a"]\naliases = ["c"]\n'
            + '[commands.c]\ndescription = "C."\nrun = ["c"]\n',
        )

        result = self.run_x("help")

        self.assertEqual(result.status, 2)
        self.assertEqual(
            result.stderr,
            "x: " + str(self.root / "x.toml") + ": the name 'c' is claimed by both a and c\n",
        )

        self.write(
            "x.toml",
            '[x]\nversion = "' + __version__ + '"\ndefault = "nothere"\n'
            '[commands.a]\ndescription = "A."\nrun = ["a"]\n',
        )
        missing = self.run_x("a")
        self.assertEqual(missing.status, 2)
        self.assertEqual(
            missing.stderr,
            "x: " + str(self.root / "x.toml") + ": the default command does not exist: nothere\n",
        )

    def test_config_errors_exit_two_with_one_line(self):
        self.write("x.toml", '[x]\nversion = "' + __version__ + '"\nbogus = true\n')

        result = self.run_x("help")

        self.assertEqual(result.status, 2)
        self.assertTrue(result.stderr.startswith("x: "))
        self.assertEqual(result.stderr.count("\n"), 1)
        self.assertIn("unknown key 'bogus'", result.stderr)


class RootFlagTest(RepoTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.write(
            "x.toml",
            ROOT
            + '[commands.go]\ndescription = "Go."\nrun = ["t"]\n'
            + '[commands.here]\ndescription = "H."\nrun = ["t"]\ncwd = "caller"\n',
        )
        # A separate checkout with no x.toml of its own, such as a
        # nested repository: the search from inside it finds nothing.
        self.elsewhere = self.root.parent / "elsewhere"
        self.elsewhere.mkdir()
        (self.elsewhere / ".git").mkdir()

    def run_from_elsewhere(self, *argv: str):
        return self.run_x(*argv, cwd=str(self.elsewhere))

    def test_without_it_another_checkout_sees_no_commands(self):
        result = self.run_from_elsewhere("go")

        self.assertEqual(result.status, 2)
        self.assertEqual(result.requests, [])

    def test_it_selects_the_config_but_not_the_callers_folder(self):
        result = self.run_from_elsewhere("--root", str(self.root), "go", "one")

        self.assertEqual(result.requests[0].argv, ("t", "one"))
        self.assertEqual(result.requests[0].cwd, self.root)
        self.assertEqual(
            result.requests[0].environ["X_CALLER_DIRECTORY"], str(self.elsewhere)
        )

    def test_a_caller_relative_command_still_runs_where_the_caller_is(self):
        result = self.run_from_elsewhere("--root", str(self.root), "here")

        self.assertEqual(result.requests[0].cwd, self.elsewhere)

    def test_it_combines_with_builtin(self):
        result = self.run_from_elsewhere("--root", str(self.root), "--builtin", "help")

        self.assertEqual(result.stdout, "go\tGo.\nhere\tH.\n")

    def test_it_needs_an_existing_folder(self):
        missing = self.run_x("--root", "no-such-folder", "go")
        bare = self.run_x("--root")

        self.assertEqual(missing.status, 2)
        self.assertIn("not a folder: no-such-folder", missing.stderr)
        self.assertEqual(bare.status, 2)
        self.assertIn("usage: --root <folder>", bare.stderr)

    def test_a_pinned_rerun_keeps_it(self):
        self.write("x.toml", '[x]\nversion = "9.9.9"\n')

        result = self.run_from_elsewhere("--root", str(self.root), "go")

        self.assertEqual(
            result.requests[0].argv,
            ("npx", "--yes", "@tryc.ai/x@9.9.9", "--pinned", "--root", str(self.root), "go"),
        )


class PinTest(RepoTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.write(
            "x.toml",
            '[x]\nversion = "9.9.9"\n[commands.go]\ndescription = "Go."\nrun = ["t"]\n',
        )

    def test_a_different_pin_reruns_at_the_pinned_version(self):
        result = self.run_x(
            "go", "one", environ={"npm_config_package": "wrong"}, status=4
        )

        self.assertEqual(result.status, 4)
        self.assertEqual(len(result.requests), 1)
        self.assertEqual(
            result.requests[0].argv,
            ("npx", "--yes", "@tryc.ai/x@9.9.9", "--pinned", "go", "one"),
        )
        self.assertNotIn("npm_config_package", result.requests[0].environ)

    def test_a_pin_that_still_does_not_match_is_reported_not_retried(self):
        result = self.run_x("--pinned", "go")

        self.assertEqual(result.status, 2)
        self.assertEqual(result.requests, [])
        self.assertIn("pins version 9.9.9 but version " + __version__, result.stderr)

    def test_version_reports_the_running_engine_whatever_the_pin(self):
        result = self.run_x("--version")

        self.assertEqual(result.stdout, __version__ + "\n")
        self.assertEqual(result.requests, [])


class MakefileCommandTest(RepoTestCase):
    CONFIG = (
        ROOT
        + '[commands.go]\ndescription = "Go."\nrun = ["t"]\n'
        + '[make]\ndefault = "go"\n'
    )

    def test_write_then_check(self):
        self.write("x.toml", self.CONFIG)

        self.assertEqual(self.run_x("makefile", "--check").status, 1)
        self.assertEqual(self.run_x("makefile").status, 0)
        self.assertEqual(self.run_x("makefile", "--check").status, 0)
        text = (self.root / "Makefile").read_text(encoding="utf-8")
        self.assertTrue(text.startswith(BANNER + "\n"))
        self.assertIn("\n.PHONY: go\ngo:\n\t@$(X) go $(ARGS)\n", text)

    def test_check_fails_after_the_config_changes(self):
        self.write("x.toml", self.CONFIG)
        self.run_x("makefile")
        self.write(
            "x.toml", self.CONFIG + '[commands.more]\ndescription = "M."\nrun = ["m"]\n'
        )

        result = self.run_x("makefile", "--check")

        self.assertEqual(result.status, 1)
        self.assertIn("is stale", result.stderr)

    def test_hand_written_makefile_is_not_overwritten_without_force(self):
        self.write("x.toml", self.CONFIG)
        self.write("Makefile", "all:\n\techo mine\n")

        refused = self.run_x("makefile")

        self.assertEqual(refused.status, 1)
        self.assertIn("was not generated by x", refused.stderr)
        self.assertEqual(
            (self.root / "Makefile").read_text(encoding="utf-8"), "all:\n\techo mine\n"
        )
        self.assertEqual(self.run_x("makefile", "--force").status, 0)
        self.assertEqual(self.run_x("makefile", "--check").status, 0)

    def test_it_is_written_at_the_root_from_any_folder(self):
        self.write("x.toml", self.CONFIG)
        (self.root / "deep").mkdir()

        self.assertEqual(self.run_x("makefile", cwd="deep").status, 0)
        self.assertTrue((self.root / "Makefile").is_file())
        self.assertFalse((self.root / "deep" / "Makefile").exists())

    def test_a_name_make_cannot_use_is_reported_in_one_line(self):
        self.write(
            "x.toml",
            ROOT + '[commands."a:b"]\ndescription = "Odd."\nrun = ["t"]\n[make]\n',
        )

        for argv in (("makefile",), ("makefile", "--check")):
            with self.subTest(argv):
                result = self.run_x(*argv)

                self.assertEqual(result.status, 2)
                self.assertEqual(
                    result.stderr,
                    "x: a command named 'a:b' cannot be a Make target; "
                    "rename it or set make = false\n",
                )
        self.assertIn("error\tMakefile\ta command named 'a:b'", self.run_x("check").stdout)

    def test_a_makefile_path_that_is_a_folder_is_refused_and_leaves_nothing(self):
        self.write(
            "x.toml",
            ROOT + '[commands.go]\ndescription = "Go."\nrun = ["t"]\n[make]\n',
        )
        (self.root / "Makefile").mkdir()

        refused = self.run_x("makefile")
        forced = self.run_x("makefile", "--force")

        self.assertEqual(refused.status, 1)
        self.assertIn("was not generated by x", refused.stderr)
        self.assertEqual(forced.status, 1)
        self.assertTrue(forced.stderr.startswith("x: cannot write "))
        self.assertEqual(
            sorted(entry.name for entry in self.root.iterdir()),
            [".git", "Makefile", "x.toml"],
        )

    def test_a_makefile_path_in_a_missing_folder_is_reported_in_one_line(self):
        self.write(
            "x.toml",
            ROOT
            + '[commands.go]\ndescription = "Go."\nrun = ["t"]\n'
            + '[make]\npath = "build/Makefile"\n',
        )

        result = self.run_x("makefile")

        self.assertEqual(result.status, 1)
        self.assertTrue(result.stderr.startswith("x: cannot write "))
        self.assertEqual(result.stderr.count("\n"), 1)

    def test_it_needs_a_make_table(self):
        self.write(
            "x.toml", ROOT + '[commands.go]\ndescription = "Go."\nrun = ["t"]\n'
        )

        result = self.run_x("makefile")

        self.assertEqual(result.status, 2)
        self.assertIn("needs a [make] table", result.stderr)


class CheckAndInitTest(RepoTestCase):
    def test_check_reports_each_file_and_the_makefile(self):
        self.write(
            "x.toml",
            ROOT + '[commands.go]\ndescription = "Go."\nbash = "tools/go.sh"\n[make]\n',
        )
        self.write("tools/go.sh", "#!/bin/bash\n")
        self.run_x("makefile")

        result = self.run_x("check")

        self.assertEqual(result.status, 0)
        self.assertEqual(
            result.stdout,
            "ok\tx.toml\t1 command\nok\tMakefile\tmatches x.toml\n",
        )

    def test_check_tells_a_hand_written_makefile_from_a_stale_one(self):
        self.write(
            "x.toml", ROOT + '[commands.go]\ndescription = "Go."\nrun = ["t"]\n[make]\n'
        )
        self.write("Makefile", "all:\n\techo mine\n")

        result = self.run_x("check")

        self.assertEqual(result.status, 1)
        self.assertIn(
            "error\tMakefile\twas not generated by x; `makefile --force` would replace it\n",
            result.stdout,
        )

    def test_check_fails_on_a_stale_makefile_and_warns_on_a_missing_script(self):
        self.write(
            "x.toml",
            ROOT + '[commands.go]\ndescription = "Go."\nbash = "tools/go.sh"\n[make]\n',
        )

        result = self.run_x("check")

        self.assertEqual(result.status, 1)
        self.assertIn("warn\tx.toml\tgo: no file at tools/go.sh\n", result.stdout)
        self.assertIn("error\tMakefile\tis stale", result.stdout)

    def test_check_notes_a_folder_command_that_replaces_a_root_one(self):
        self.write("x.toml", ROOT + '[commands.go]\ndescription = "Go."\nrun = ["t"]\n')
        self.write("sub/x.toml", '[commands.go]\ndescription = "Local go."\nrun = ["u"]\n')

        result = self.run_x("check", cwd="sub")

        self.assertEqual(result.status, 0)
        self.assertIn("note\tsub/x.toml\tgo replaces go from x.toml\n", result.stdout)

    def test_check_notes_a_replacement_or_takeover_made_through_an_alias(self):
        self.write(
            "x.toml",
            ROOT
            + '[commands.deploy]\ndescription = "D."\nrun = ["d"]\n'
            + '[commands.test]\ndescription = "T."\nrun = ["t"]\naliases = ["t"]\n',
        )
        self.write(
            "sub/x.toml",
            '[commands.ship]\ndescription = "S."\nrun = ["s"]\naliases = ["deploy", "t"]\n',
        )

        result = self.run_x("check", cwd="sub")

        self.assertIn("note\tsub/x.toml\tship replaces deploy from x.toml\n", result.stdout)
        self.assertIn(
            "note\tsub/x.toml\tship takes the alias t from test in x.toml\n",
            result.stdout,
        )

    def test_check_from_the_root_does_not_see_a_folders_replacements(self):
        self.write("x.toml", ROOT + '[commands.go]\ndescription = "Go."\nrun = ["t"]\n')
        self.write("sub/x.toml", '[commands.go]\ndescription = "Local go."\nrun = ["u"]\n')

        self.assertNotIn("note\t", self.run_x("check").stdout)

    def test_check_without_a_config_fails(self):
        result = self.run_x("check")

        self.assertEqual(result.status, 1)
        self.assertEqual(
            result.stderr,
            "x: no x.toml applies in " + str(self.root) + "; run `init` to create one\n",
        )

    @unittest.skipUnless(shutil.which("git"), "git is not installed")
    def test_check_runs_the_registry_of_a_tracked_folder_file_it_is_not_in(self):
        shutil.rmtree(self.root / ".git")
        self.write("x.toml", ROOT + '[commands.go]\ndescription = "Go."\nrun = ["t"]\n')
        # Parses, but cannot be run: no handler and no [defaults].
        self.write("web/x.toml", '[commands.serve]\ndescription = "Serve."\n')
        self.write("api/x.toml", '[commands.up]\ndescription = "Up."\nrun = ["u"]\n')
        for argv in (["init", "-q"], ["add", "x.toml", "web/x.toml", "api/x.toml"]):
            subprocess.run(["git", *argv], cwd=self.root, check=True, capture_output=True)

        result = self.run_x("check")

        self.assertEqual(result.status, 1)
        self.assertIn("ok\tapi/x.toml\t1 command\n", result.stdout)
        self.assertIn("error\tweb/x.toml\t", result.stdout)
        self.assertIn("needs one of run, bash, npx, bazel, builtin", result.stdout)

    def test_init_writes_a_root_file_that_loads(self):
        result = self.run_x("init")

        self.assertEqual(result.status, 0)
        self.assertEqual(result.stdout, "wrote x.toml\n")
        self.assertEqual(self.run_x("help").stdout, "hello\tSay hello.\n")
        argv = self.run_x("hello", "there").requests[0].argv
        # The starter runs the interpreter x itself runs on, so it
        # needs no other program to exist on any platform.
        self.assertEqual(argv[:2], (sys.executable, "-c"))
        self.assertEqual(argv[3:], ("there",))
        printed = subprocess.run(
            list(argv), stdout=subprocess.PIPE, text=True, check=True
        ).stdout
        self.assertEqual(printed, "hello from x there\n")

    def test_init_below_a_root_writes_a_folder_file(self):
        self.run_x("init")
        (self.root / "sub").mkdir()

        self.assertEqual(self.run_x("init", cwd="sub").status, 0)

        text = (self.root / "sub" / "x.toml").read_text(encoding="utf-8")
        self.assertNotIn("[x]", text)
        request = self.run_x("hello", cwd="sub").requests[0]
        self.assertEqual(request.cwd, self.root / "sub")
        printed = subprocess.run(
            list(request.argv), stdout=subprocess.PIPE, text=True, check=True
        ).stdout
        self.assertEqual(printed, "hello from this folder\n")

    @unittest.skipIf(os.name == "nt", "POSIX permissions")
    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root ignores them")
    def test_init_in_a_folder_it_cannot_write_says_so_in_one_line(self):
        locked = self.root / "locked"
        locked.mkdir()
        locked.chmod(stat.S_IRUSR | stat.S_IXUSR)
        self.addCleanup(locked.chmod, stat.S_IRWXU)

        result = self.run_x("init", cwd="locked")

        self.assertEqual(result.status, 1)
        self.assertTrue(result.stderr.startswith("x: cannot write "))
        self.assertEqual(result.stderr.count("\n"), 1)

    def test_init_never_overwrites(self):
        self.write("x.toml", ROOT)

        result = self.run_x("init")

        self.assertEqual(result.status, 1)
        self.assertIn("already exists", result.stderr)
        self.assertEqual((self.root / "x.toml").read_text(encoding="utf-8"), ROOT)


class ResumeTest(RepoTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.home = self.root.parent / "home"
        self.checkout = self.root.parent / "checkout"
        self.checkout.mkdir()
        directory = self.home / ".claude" / "projects" / "-checkout"
        directory.mkdir(parents=True)
        (directory / "session-one.jsonl").write_text(
            json.dumps({"type": "user", "cwd": str(self.checkout)}) + "\n",
            encoding="utf-8",
        )
        self.environ = {"HOME": str(self.home)}

    def test_configured_launcher_resumes_in_the_sessions_directory(self):
        self.write(
            "x.toml",
            ROOT + '[resume.agents.claude]\nnpx = ["--yes", "example-agent"]\n',
        )

        result = self.run_x("resume", "session-one", "--verbose", environ=self.environ)

        self.assertEqual(result.status, 0)
        self.assertEqual(
            result.requests[0].argv,
            ("npx", "--yes", "example-agent", "--resume", "session-one", "--verbose"),
        )
        self.assertEqual(result.requests[0].cwd, self.checkout)

    def test_launcher_placeholders_name_the_agent_session_and_directory(self):
        self.write(
            "x.toml",
            ROOT
            + "[resume.agents.claude]\n"
            + 'run = ["launch", "{agent}", "--cwd", "{cwd}", "--id", "{session}", "--"]\n',
        )

        result = self.run_x("resume", "CLAUDE=session-one", environ=self.environ)

        self.assertEqual(
            result.requests[0].argv,
            (
                "launch",
                "claude",
                "--cwd",
                str(self.checkout),
                "--id",
                "session-one",
                "--",
                "--resume",
                "session-one",
            ),
        )

    CUSTOM = (
        ROOT
        + '[commands.helper]\ndescription = "Our agent."\nrun = ["our-agent"]\n'
        + "[resume.agents.helper]\n"
        + 'command = "helper"\n'
        + 'resume = ["--resume", "{session}"]\n'
        + 'last = ["--continue"]\n'
        + 'aliases = ["hp"]\n'
    )

    def test_custom_agent_is_resumed_with_the_arguments_its_table_gives(self):
        self.write("x.toml", self.CUSTOM)

        named = self.run_x("resume", "HELPER=abc", "extra", environ=self.environ)
        aliased = self.run_x("resume", "hp=abc", environ=self.environ)
        flagged = self.run_x("resume", "--agent", "helper", "abc", environ=self.environ)

        self.assertEqual(
            named.requests[0].argv, ("our-agent", "--resume", "abc", "extra")
        )
        self.assertEqual(aliased.requests[0].argv, ("our-agent", "--resume", "abc"))
        self.assertEqual(flagged.requests[0].argv, ("our-agent", "--resume", "abc"))

    def test_custom_agent_last_uses_its_last_arguments(self):
        self.write("x.toml", self.CUSTOM)

        result = self.run_x("resume", "HELPER=last", "extra", environ=self.environ)

        self.assertEqual(result.requests[0].argv, ("our-agent", "--continue", "extra"))

    def test_custom_agent_without_last_refuses_it(self):
        self.write("x.toml", self.CUSTOM.replace('last = ["--continue"]\n', ""))

        result = self.run_x("resume", "HELPER=last", environ=self.environ)

        self.assertEqual(result.status, 2)
        self.assertEqual(result.requests, [])
        self.assertIn("'last' is not configured for helper", result.stderr)

    def test_custom_agent_with_a_launcher_starts_at_the_root(self):
        self.write(
            "x.toml",
            ROOT
            + "[resume.agents.helper]\n"
            + 'run = ["helper-cli"]\nresume = ["--session={session}"]\n',
        )

        result = self.run_x("resume", "HELPER=abc", environ=self.environ)

        self.assertEqual(result.requests[0].argv, ("helper-cli", "--session=abc"))
        self.assertEqual(result.requests[0].cwd, self.root)

    def test_alias_for_a_builtin_agent_comes_from_its_table(self):
        self.write(
            "x.toml",
            ROOT + '[resume.agents.claude]\nrun = ["agent"]\naliases = ["cl"]\n',
        )

        result = self.run_x("resume", "CL=session-one", environ=self.environ)

        self.assertEqual(result.requests[0].argv, ("agent", "--resume", "session-one"))
        self.assertEqual(result.requests[0].cwd, self.checkout)

    def test_an_agent_that_is_not_declared_is_not_part_of_the_grammar(self):
        self.write("x.toml", ROOT)

        result = self.run_x("resume", "--agent", "helper", "abc", environ=self.environ)

        self.assertEqual(result.status, 2)
        self.assertIn("unknown agent 'helper'", result.stderr)

    def test_dispatched_agent_can_be_told_the_agent_and_its_directory(self):
        self.write(
            "x.toml",
            ROOT
            + '[commands.launch]\ndescription = "Start an agent."\nrun = ["launcher"]\n'
            + "[resume.agents.claude]\n"
            + 'command = "launch"\nargs = ["{agent}", "--cwd", "{cwd}", "--"]\n',
        )

        result = self.run_x("resume", "session-one", "-v", environ=self.environ)

        self.assertEqual(
            result.requests[0].argv,
            (
                "launcher",
                "claude",
                "--cwd",
                str(self.checkout),
                "--",
                "--resume",
                "session-one",
                "-v",
            ),
        )
        # A dispatched command runs where that command always runs;
        # the session's folder reaches it as an argument instead.
        self.assertEqual(result.requests[0].cwd, self.root)

    def test_agent_without_a_launcher_says_what_to_add(self):
        self.write("x.toml", ROOT)

        result = self.run_x("resume", "session-one", environ=self.environ)

        self.assertEqual(result.status, 2)
        self.assertEqual(result.requests, [])
        self.assertIn("add [resume.agents.claude] to the root x.toml", result.stderr)

    def test_grammar_errors_exit_two(self):
        self.write("x.toml", ROOT)

        result = self.run_x("resume", environ=self.environ)

        self.assertEqual(result.status, 2)
        self.assertTrue(result.stderr.startswith("resume: missing session id"))


if __name__ == "__main__":
    unittest.main()
