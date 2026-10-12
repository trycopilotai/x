"""Tests for parsing and validating x.toml files."""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from x.config import ConfigError, load_workspace, normalizer, repository_top


ROOT = '[x]\nversion = "0.1.2"\n'


class ConfigTestCase(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.outside = Path(temporary.name).resolve()
        self.root = self.outside / "repo"
        self.root.mkdir()
        (self.root / ".git").mkdir()

    def write(self, relative: str, contents: str) -> Path:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(contents.encode("utf-8"))
        return path

    def load(self, contents: str, start: str = "."):
        self.write("x.toml", contents)
        return load_workspace(self.root / start)

    def assert_rejected(self, contents: str, pattern: str) -> None:
        with self.assertRaisesRegex(ConfigError, pattern):
            self.load(contents)


class RootFileTest(ConfigTestCase):
    def test_minimal_root_has_documented_defaults(self):
        workspace = self.load(ROOT)

        self.assertEqual(workspace.root.version, "0.1.2")
        self.assertEqual(workspace.root.naming, "exact")
        self.assertEqual(workspace.root.default, "help")
        self.assertEqual(workspace.root.caller_env, ("X_CALLER_DIRECTORY",))
        self.assertEqual(workspace.root.commands, ())
        self.assertIsNone(workspace.root.make)
        self.assertEqual(workspace.directory, self.root)

    def test_no_file_means_no_workspace(self):
        self.assertIsNone(load_workspace(self.root))

    def test_version_is_required_and_exact(self):
        self.assert_rejected("[x]\n", r"\[x\] version: is required")
        self.assert_rejected('[x]\nversion = "^0.1"\n', "must be an exact version")
        self.assert_rejected('[x]\nversion = "latest"\n', "must be an exact version")

    def test_unknown_keys_are_errors_at_every_level(self):
        self.assert_rejected(
            "surprise = 1\n" + ROOT, "top level: unknown key 'surprise'"
        )
        self.assert_rejected(
            '[x]\nversion = "0.1.2"\ncolour = "red"\n', r"\[x\]: unknown key 'colour'"
        )
        self.assert_rejected(
            ROOT + '[flags.quiet]\nnames = ["-q"]\nshort = "q"\n',
            r"\[flags.quiet\]: unknown key 'short'",
        )
        self.assert_rejected(
            ROOT + '[defaults]\nrun = ["a"]\ntimeout = 5\n',
            r"\[defaults\]: unknown key 'timeout'",
        )
        self.assert_rejected(
            ROOT + '[unknown]\nrun = ["a"]\nquiet = true\n',
            r"\[unknown\]: unknown key 'quiet'",
        )
        self.assert_rejected(
            ROOT + '[resume]\ndefault = "claude"\n',
            r"\[resume\]: unknown key 'default'",
        )
        self.assert_rejected(
            ROOT + '[resume.agents.claude]\nrun = ["a"]\nmodel = "m"\n',
            r"\[resume.agents.claude\]: unknown key 'model'",
        )
        self.assert_rejected(
            ROOT
            + '[commands.a]\ndescription = "A."\nrun = ["a"]\n'
            + '[commands.a.windows]\nrun = ["b"]\nshell = true\n',
            r"\[commands.a.windows\]: unknown key 'shell'",
        )
        self.assert_rejected(
            ROOT + '[commands.a]\ndescription = "A."\nrun = ["a"]\nretries = 3\n',
            r"\[commands.a\]: unknown key 'retries'",
        )
        self.assert_rejected(ROOT + "[make]\nindent = 2\n", "unknown key 'indent'")
        self.assert_rejected(
            ROOT + '[commands.a]\ndescription = "A."\nrun = ["a"]\n'
            "[commands.a.make]\nphony = false\n",
            "unknown key 'phony'",
        )

    def test_syntax_errors_name_the_file(self):
        with self.assertRaisesRegex(ConfigError, "x.toml"):
            self.load("[x\n")

    def test_a_file_that_is_not_utf8_is_a_config_error(self):
        (self.root / "x.toml").write_bytes(b'[x]\nversion = "0.1.2"\n# \xff\xfe\n')

        with self.assertRaisesRegex(ConfigError, "x.toml: is not UTF-8 text"):
            load_workspace(self.root)

    def test_naming_must_be_a_known_style(self):
        self.assert_rejected(
            '[x]\nversion = "0.1.2"\nnaming = "camel"\n', "must be one of exact"
        )

    def test_caller_env_must_be_variable_names(self):
        self.assert_rejected(
            '[x]\nversion = "0.1.2"\ncaller_env = ["NOT OK"]\n',
            "not an environment variable name",
        )


class CommandTest(ConfigTestCase):
    def test_every_handler_kind_is_read(self):
        workspace = self.load(
            ROOT
            + '[commands.a]\ndescription = "A."\nrun = ["tool", "one"]\n'
            + '[commands.b]\ndescription = "B."\nbash = "s.sh"\nargs = ["two"]\n'
            + '[commands.c]\ndescription = "C."\nnpx = ["--yes", "pkg"]\n'
            + '[commands.d]\ndescription = "D."\nbazel = ["build", "//..."]\n'
            + '[commands.e]\ndescription = "E."\nbuiltin = "help"\n'
        )

        handlers = {
            command.name: command.handler for command in workspace.root.commands
        }
        self.assertEqual(handlers["a"].kind, "run")
        self.assertEqual(handlers["a"].argv, ("tool", "one"))
        self.assertEqual(handlers["b"].kind, "bash")
        self.assertEqual(handlers["b"].target, "s.sh")
        self.assertEqual(handlers["b"].argv, ("two",))
        self.assertEqual(handlers["c"].argv, ("--yes", "pkg"))
        self.assertEqual(handlers["d"].argv, ("build", "//..."))
        self.assertEqual(handlers["e"].target, "help")

    def test_declaration_order_is_kept(self):
        workspace = self.load(
            ROOT
            + '[commands.zeta]\ndescription = "Z."\nrun = ["z"]\n'
            + '[commands.alpha]\ndescription = "A."\nrun = ["a"]\n'
        )

        self.assertEqual(
            [command.name for command in workspace.root.commands],
            ["zeta", "alpha"],
        )

    def test_quoted_names_allow_dots(self):
        workspace = self.load(
            ROOT + '[commands."v2.migrate"]\ndescription = "Migrate."\nrun = ["d"]\n'
        )

        self.assertEqual(workspace.root.commands[0].name, "v2.migrate")

    def test_description_is_required_and_single_line(self):
        self.assert_rejected(
            ROOT + '[commands.a]\nrun = ["a"]\n', "description: is required"
        )
        self.assert_rejected(
            ROOT + '[commands.a]\ndescription = "one\\ntwo"\nrun = ["a"]\n',
            "must be one line",
        )

    def test_one_handler_per_command(self):
        self.assert_rejected(
            ROOT + '[commands.a]\ndescription = "A."\nrun = ["a"]\nbash = "a.sh"\n',
            "more than one of run, bash",
        )

    def test_args_belongs_to_bash_only(self):
        self.assert_rejected(
            ROOT + '[commands.a]\ndescription = "A."\nrun = ["a"]\nargs = ["b"]\n',
            "args: is only valid beside bash",
        )

    def test_platform_tables_override_the_handler(self):
        workspace = self.load(
            ROOT
            + '[commands.a]\ndescription = "A."\nbash = "a.sh"\n'
            + '[commands.a.windows]\nbash = "a-windows.sh"\n'
        )

        command = workspace.root.commands[0]
        self.assertEqual(command.platform_handlers[0][0], "windows")
        self.assertEqual(command.platform_handlers[0][1].target, "a-windows.sh")

    def test_platform_table_needs_a_handler_and_cannot_be_a_builtin(self):
        self.assert_rejected(
            ROOT
            + '[commands.a]\ndescription = "A."\nrun = ["a"]\n[commands.a.linux]\n',
            "needs one of run, bash, npx, bazel",
        )
        self.assert_rejected(
            ROOT
            + '[commands.a]\ndescription = "A."\nrun = ["a"]\n'
            + '[commands.a.linux]\nbuiltin = "help"\n',
            "builtin: is not allowed here",
        )

    def test_unknown_platform_is_rejected(self):
        self.assert_rejected(
            ROOT
            + '[commands.a]\ndescription = "A."\nrun = ["a"]\nplatforms = ["bsd"]\n',
            "unknown platform 'bsd'",
        )

    def test_cwd_must_stay_relative_on_every_host(self):
        # Judged by the text, not by the host's own idea of an
        # absolute path, so one file loads the same everywhere.
        for value in (
            "/etc",
            "\\\\server\\share",
            "C:\\work",
            "C:/work",
            "C:work",
            "\\work",
        ):
            with self.subTest(value):
                self.assert_rejected(
                    ROOT
                    + '[commands.a]\ndescription = "A."\nrun = ["a"]\ncwd = '
                    + json.dumps(value)
                    + "\n",
                    "must be config, root, caller, or a relative path",
                )

    def test_relative_cwd_values_load(self):
        for value in ("pkg", "pkg/sub", "../sibling", "."):
            with self.subTest(value):
                workspace = self.load(
                    ROOT
                    + '[commands.a]\ndescription = "A."\nrun = ["a"]\ncwd = '
                    + json.dumps(value)
                    + "\n"
                )

                self.assertEqual(workspace.root.commands[0].cwd, value)

    def test_make_accepts_false_or_a_table(self):
        workspace = self.load(
            ROOT
            + '[commands.a]\ndescription = "A."\nrun = ["a"]\nmake = false\n'
            + '[commands.b]\ndescription = "B."\nrun = ["b"]\n'
            + '[commands.b.make]\nargs = false\nrecipe = ["@true"]\n'
        )

        first, second = workspace.root.commands
        self.assertFalse(first.make.enabled)
        self.assertTrue(second.make.enabled)
        self.assertIs(second.make.args, False)
        self.assertEqual(second.make.recipe, ("@true",))


class RootTablesTest(ConfigTestCase):
    def test_flags_defaults_and_unknown_are_read(self):
        workspace = self.load(
            ROOT
            + '[flags.silent]\nnames = ["-s", "--silent"]\nforward = ["--silent"]\n'
            + '[defaults]\nbash = "main.sh"\nargs = ["{flags}", "{name}"]\n'
            + '[defaults.windows]\nbash = "main-wsl.sh"\n'
            + '[unknown]\nbash = "main.sh"\n'
        )

        root = workspace.root
        self.assertEqual(root.flags[0].names, ("-s", "--silent"))
        self.assertEqual(root.flags[0].forward, ("--silent",))
        self.assertEqual(root.defaults.handler.argv, ("{flags}", "{name}"))
        self.assertEqual(root.defaults.platform_handlers[0][1].target, "main-wsl.sh")
        self.assertEqual(root.unknown.handler.target, "main.sh")

    def test_flag_names_start_with_a_dash(self):
        self.assert_rejected(
            ROOT + '[flags.silent]\nnames = ["silent"]\n', "flag names start with '-'"
        )

    def test_defaults_needs_a_handler(self):
        self.assert_rejected(
            ROOT + "[defaults]\n", "needs one of run, bash, npx, bazel"
        )

    def test_header_replaces_launcher_and_default(self):
        self.assert_rejected(
            ROOT + '[make]\nheader = ["X := tool"]\nlauncher = "tool"\n',
            "header replaces the generated header",
        )

    def test_make_path_must_stay_inside_the_root(self):
        for value in (
            "/tmp/Makefile",
            "C:\\Makefile",
            "../Makefile",
            "build/../../Makefile",
        ):
            with self.subTest(value):
                self.assert_rejected(
                    ROOT + "[make]\npath = " + json.dumps(value) + "\n",
                    "must be a path inside the root x.toml's folder",
                )

        workspace = self.load(ROOT + '[make]\npath = "build/Makefile"\n')
        self.assertEqual(workspace.root.make.path, "build/Makefile")

    def test_builtin_agents_are_found_by_product_name_and_need_one_launcher(self):
        workspace = self.load(
            ROOT
            + '[resume.agents.claude-code]\nnpx = ["--yes", "pkg"]\naliases = ["cl"]\n'
            + '[resume.agents.codex]\ncommand = "codex"\nargs = ["{cwd}"]\n'
        )

        agents = {agent.agent: agent for agent in workspace.root.resume}
        self.assertEqual(agents["claude"].handler.kind, "npx")
        self.assertEqual(agents["claude"].aliases, ("cl",))
        self.assertFalse(agents["claude"].custom)
        self.assertEqual(agents["codex"].command, "codex")
        self.assertEqual(agents["codex"].args, ("{cwd}",))
        self.assert_rejected(ROOT + "[resume.agents.claude]\n", "needs exactly one of")
        self.assert_rejected(
            ROOT + '[resume.agents.claude]\nrun = ["a"]\ncommand = "b"\n',
            "needs exactly one of",
        )
        self.assert_rejected(
            ROOT
            + '[resume.agents.claude]\nrun = ["a"]\n'
            + '[resume.agents.claude-code]\nrun = ["b"]\n',
            "configured twice",
        )
        self.assert_rejected(
            ROOT + '[resume.agents.claude]\nrun = ["a"]\nresume = ["--r"]\n',
            "x supplies them for claude",
        )

    def test_any_other_agent_is_custom_and_states_its_resume_arguments(self):
        workspace = self.load(
            ROOT
            + "[resume.agents.helper]\n"
            + 'run = ["helper-cli"]\nresume = ["--resume", "{session}"]\n'
            + 'last = ["--continue"]\naliases = ["hp"]\n'
        )

        agent = workspace.root.resume[0]
        self.assertTrue(agent.custom)
        self.assertEqual(agent.resume, ("--resume", "{session}"))
        self.assertEqual(agent.last, ("--continue",))
        self.assert_rejected(
            ROOT + '[resume.agents.helper]\nrun = ["a"]\n',
            "x reads no session store for 'helper', so it needs `resume`",
        )
        self.assert_rejected(
            ROOT + '[resume.agents."two words"]\nrun = ["a"]\nresume = ["r"]\n',
            "not usable as an agent name",
        )

    def test_agent_names_cannot_collide(self):
        self.assert_rejected(
            ROOT
            + '[resume.agents.claude]\nrun = ["a"]\naliases = ["shared"]\n'
            + '[resume.agents.codex]\nrun = ["b"]\naliases = ["shared"]\n',
            "agent name 'shared' belongs to claude and codex",
        )
        self.assert_rejected(
            ROOT + '[resume.agents.claude]\nrun = ["a"]\naliases = ["codex"]\n',
            "agent name 'codex' belongs to codex and claude",
        )
        self.assert_rejected(
            ROOT + '[resume.agents.claude]\nrun = ["a"]\naliases = ["uuid"]\n',
            "reserved key",
        )


class FolderFileTest(ConfigTestCase):
    def test_folder_files_are_collected_root_first(self):
        self.write("x.toml", ROOT)
        self.write("a/x.toml", '[commands.one]\ndescription = "1."\nrun = ["1"]\n')
        self.write("a/b/x.toml", '[commands.two]\ndescription = "2."\nrun = ["2"]\n')

        workspace = load_workspace(self.root / "a" / "b")

        self.assertEqual(
            [path.relative_to(self.root).as_posix() for path in workspace.paths],
            ["x.toml", "a/x.toml", "a/b/x.toml"],
        )
        self.assertEqual(workspace.folders[1].commands[0].directory, self.root / "a/b")

    def test_folder_files_hold_commands_only(self):
        self.write("x.toml", ROOT)
        self.write("a/x.toml", "[make]\n")

        with self.assertRaisesRegex(ConfigError, "may only hold"):
            load_workspace(self.root / "a")

    def test_folder_file_without_a_root_is_an_error(self):
        self.write("a/x.toml", '[commands.one]\ndescription = "1."\nrun = ["1"]\n')

        with self.assertRaisesRegex(
            ConfigError, "no root x.toml applies above it in this repository"
        ):
            load_workspace(self.root / "a")

    def test_folder_file_outside_a_repository_says_why_nothing_above_applies(self):
        self.require_no_repository_around_the_temp_folder()
        (self.outside / "x.toml").write_bytes(ROOT.encode("utf-8"))
        plain = self.outside / "plain"
        plain.mkdir()
        (plain / "x.toml").write_bytes(
            b'[commands.one]\ndescription = "1."\nrun = ["1"]\n'
        )

        with self.assertRaisesRegex(
            ConfigError,
            "this folder is in no Git repository, so files in the folders above",
        ):
            load_workspace(plain)

    def test_search_does_not_leave_the_repository(self):
        (self.outside / "x.toml").write_bytes(ROOT.encode("utf-8"))

        self.assertIsNone(load_workspace(self.root))

    def require_no_repository_around_the_temp_folder(self) -> None:
        if repository_top(self.outside) is not None:
            self.skipTest("the temporary folder is itself inside a Git repository")

    def test_a_folder_in_no_repository_is_searched_alone(self):
        self.require_no_repository_around_the_temp_folder()
        (self.outside / "x.toml").write_bytes(ROOT.encode("utf-8"))
        plain = self.outside / "plain" / "deeper"
        plain.mkdir(parents=True)

        # The file two folders up is never reached: nothing marks
        # these folders as belonging together.
        self.assertIsNone(load_workspace(plain))
        self.assertIsNone(load_workspace(plain.parent))
        self.assertEqual(load_workspace(self.outside).directory, self.outside)

    def test_a_folder_in_no_repository_uses_its_own_file(self):
        self.require_no_repository_around_the_temp_folder()
        plain = self.outside / "plain"
        plain.mkdir()
        (plain / "x.toml").write_bytes(b'[x]\nversion = "0.3.0"\n')
        (self.outside / "x.toml").write_bytes(ROOT.encode("utf-8"))

        self.assertEqual(load_workspace(plain).root.version, "0.3.0")

    def test_nearest_root_wins_over_one_farther_up(self):
        self.write("x.toml", ROOT)
        self.write("nested/x.toml", '[x]\nversion = "0.2.0"\n')

        workspace = load_workspace(self.root / "nested")

        self.assertEqual(workspace.root.version, "0.2.0")
        self.assertEqual(workspace.folders, ())


class NamingTest(unittest.TestCase):
    def test_exact_keeps_names_as_written(self):
        fold = normalizer("exact")

        self.assertEqual(fold("Build-All"), "Build-All")

    def test_snake_folds_case_and_separators(self):
        fold = normalizer("snake")

        for spelling in ("report_serve", "Report.Serve", "report-serve"):
            self.assertEqual(fold(spelling), "report_serve")
        self.assertEqual(fold("reportServe"), "report_serve")
        self.assertEqual(fold("v2_migrate"), "v2_migrate")

    def test_kebab_folds_to_dashes(self):
        fold = normalizer("kebab")

        for spelling in ("build-all", "build_all", "buildAll", "Build.All"):
            self.assertEqual(fold(spelling), "build-all")


if __name__ == "__main__":
    unittest.main()
