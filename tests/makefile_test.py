"""Tests for Makefile rendering, freshness checks and safe writes."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from x.config import load_workspace
from x.makefile import (
    BANNER,
    check_makefile,
    is_generated,
    render_makefile,
    write_makefile,
)
from x.model import SpecError


# A repository whose commands are listed in the order they are
# declared and always forward ARGS, with a hand-written header, a
# recipe that bypasses x so it works before anything is installed,
# and a value handed to the command through the environment.
DECLARED = r"""
[x]
version = "0.1.0"
naming = "kebab"

[commands.start]
description = "Start the app."
run = ["tool", "start"]

[commands.setup]
description = "Prepare a fresh clone."
bash = "tools/setup.sh"
make.recipe = ["@tools/setup.sh $(ARGS)"]

[commands.ask]
description = "Ask one question."
run = ["tool", "ask"]
make.prelude = ["ask: export ASK_PROMPT := $(value PROMPT)"]

[commands.serve]
description = "Serve the site."
run = ["tool", "serve"]
make.recipe = ["@$(X) serve --port $(PORT) $(ARGS)"]

[commands.resume]
description = "Resume a session."
builtin = "resume"
make.recipe = [
  "@$(X) resume \\",
  "  $(if $(AGENT),--agent $(AGENT)) \\",
  "  $(if $(UUID),UUID=$(UUID)) $(ARGS)",
]

[commands.internal]
description = "Not a Make target."
run = ["tool", "internal"]
make = false

[make]
header = [
  "SHELL := /bin/bash",
  ".DEFAULT_GOAL := start",
  "X := tools/x.sh",
  "ARGS ?=",
  "PORT ?= 8080",
  "",
  "-include extra.mk",
  "",
  ".PHONY: default",
  "",
  "default: start",
]
"""

DECLARED_MAKEFILE = (
    BANNER
    + r"""

SHELL := /bin/bash
.DEFAULT_GOAL := start
X := tools/x.sh
ARGS ?=
PORT ?= 8080

-include extra.mk

.PHONY: default

default: start

.PHONY: start
start:
	@$(X) start $(ARGS)

.PHONY: setup
setup:
	@tools/setup.sh $(ARGS)

.PHONY: ask
ask: export ASK_PROMPT := $(value PROMPT)
ask:
	@$(X) ask $(ARGS)

.PHONY: serve
serve:
	@$(X) serve --port $(PORT) $(ARGS)

.PHONY: resume
resume:
	@$(X) resume \
	  $(if $(AGENT),--agent $(AGENT)) \
	  $(if $(UUID),UUID=$(UUID)) $(ARGS)
"""
)

# A repository whose targets are sorted, forward ARGS only where a
# command asks for it, give every alias its own target, and add a
# pattern rule after `help`.
SORTED = r"""
[x]
version = "0.1.0"
naming = "snake"

[defaults]
bash = "cli/main.sh"
args = ["{flags}", "{name}"]

[commands.up]
description = "Start a stack."
make.recipe = ["@$(X) up $(filter prod dev,$(MAKECMDGOALS))"]

[commands.help]
description = "Print help."
make.after = [".PHONY: help-%", "help-%:", "\t@$(X) help \"$*\""]

[commands.build]
description = "Build everything."
aliases = ["b"]
make.args = true

[commands.prod]
description = "Select the prod stack."
make.recipe = ['@if [ "$(firstword $(MAKECMDGOALS))" = "prod" ]; then $(X) prod; fi']

[make]
order = "alpha"
args = false
aliases = true
header = [
  "SHELL := /bin/bash",
  "X := ./cli/x",
  "ifeq ($(OS),Windows_NT)",
  "X := powershell.exe -File cli/x.ps1",
  "endif",
  ".DEFAULT_GOAL := help",
]
"""

SORTED_MAKEFILE = (
    BANNER
    + r"""

SHELL := /bin/bash
X := ./cli/x
ifeq ($(OS),Windows_NT)
X := powershell.exe -File cli/x.ps1
endif
.DEFAULT_GOAL := help

.PHONY: b
b:
	@$(X) b $(ARGS)

.PHONY: build
build:
	@$(X) build $(ARGS)

.PHONY: help
help:
	@$(X) help

.PHONY: help-%
help-%:
	@$(X) help "$*"

.PHONY: prod
prod:
	@if [ "$(firstword $(MAKECMDGOALS))" = "prod" ]; then $(X) prod; fi

.PHONY: up
up:
	@$(X) up $(filter prod dev,$(MAKECMDGOALS))
"""
)


class MakefileTestCase(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        (self.root / ".git").mkdir()

    def root_config(self, contents: str):
        (self.root / "x.toml").write_bytes(contents.encode("utf-8"))
        return load_workspace(self.root).root


class RenderTest(MakefileTestCase):
    def test_declared_order_with_custom_header_and_recipes(self):
        self.assertEqual(render_makefile(self.root_config(DECLARED)), DECLARED_MAKEFILE)

    def test_sorted_targets_with_aliases_and_selective_args(self):
        self.assertEqual(render_makefile(self.root_config(SORTED)), SORTED_MAKEFILE)

    def test_default_header_pins_the_launcher_to_the_config_version(self):
        rendered = render_makefile(
            self.root_config(
                '[x]\nversion = "1.2.3"\n'
                '[commands.go]\ndescription = "Go."\nrun = ["go"]\n'
                '[make]\ndefault = "go"\n'
            )
        )

        self.assertEqual(
            rendered,
            BANNER
            + "\n\nX := npx --yes @tryc.ai/x@1.2.3\nARGS ?=\n.DEFAULT_GOAL := go\n\n"
            ".PHONY: go\ngo:\n\t@$(X) go $(ARGS)\n",
        )

    def test_launcher_key_replaces_only_the_launcher(self):
        rendered = render_makefile(
            self.root_config(
                '[x]\nversion = "1.2.3"\n'
                '[commands.go]\ndescription = "Go."\nrun = ["go"]\n'
                '[make]\nlauncher = "./x"\n'
            )
        )

        self.assertIn("\nX := ./x\nARGS ?=\n\n", rendered)

    def test_folder_commands_never_become_targets(self):
        root = self.root_config(
            '[x]\nversion = "0.1.0"\n'
            '[commands.go]\ndescription = "Go."\nrun = ["go"]\n[make]\n'
        )
        (self.root / "sub").mkdir()
        (self.root / "sub" / "x.toml").write_bytes(
            b'[commands.local]\ndescription = "Local."\nrun = ["l"]\n'
        )

        rendered = render_makefile(load_workspace(self.root / "sub").root)

        self.assertEqual(rendered, render_makefile(root))
        self.assertNotIn("local", rendered)

    def test_prelude_recipe_and_after_belong_to_the_commands_own_target(self):
        rendered = render_makefile(
            self.root_config(
                '[x]\nversion = "0.1.0"\n'
                '[commands.ask]\ndescription = "A."\nrun = ["a"]\naliases = ["q"]\n'
                'make.prelude = ["ask: export P := 1"]\n'
                'make.recipe = ["@custom"]\n'
                'make.after = ["# after ask"]\n'
                "[make]\naliases = true\n"
            )
        )

        self.assertIn(
            ".PHONY: ask\nask: export P := 1\nask:\n\t@custom\n\n# after ask\n\n"
            ".PHONY: q\nq:\n\t@$(X) q $(ARGS)\n",
            rendered,
        )

    def test_a_command_can_opt_its_aliases_in_or_out(self):
        config = (
            '[x]\nversion = "0.1.0"\n'
            '[commands.build]\ndescription = "B."\nrun = ["b"]\naliases = ["b"]\n'
            "make.aliases = {build}\n"
            '[commands.test]\ndescription = "T."\nrun = ["t"]\naliases = ["t"]\n'
            "[make]\naliases = {default}\n"
        )

        def targets(build: str, default: str) -> list[str]:
            rendered = render_makefile(
                self.root_config(config.format(build=build, default=default))
            )
            return [
                line[len(".PHONY: ") :]
                for line in rendered.split("\n")
                if line.startswith(".PHONY: ")
            ]

        self.assertEqual(targets("false", "true"), ["build", "test", "t"])
        self.assertEqual(targets("true", "false"), ["build", "b", "test"])

    def test_duplicate_targets_are_rejected(self):
        root = self.root_config(
            '[x]\nversion = "0.1.0"\n'
            '[commands.go]\ndescription = "Go."\nrun = ["go"]\naliases = ["g"]\n'
            '[commands.g]\ndescription = "G."\nrun = ["g"]\n'
            "[make]\naliases = true\n"
        )

        with self.assertRaisesRegex(SpecError, "duplicate Make target 'g'"):
            render_makefile(root)

    def test_names_make_cannot_use_are_rejected(self):
        root = self.root_config(
            '[x]\nversion = "0.1.0"\n'
            '[commands."a:b"]\ndescription = "Odd."\nrun = ["a"]\n[make]\n'
        )

        with self.assertRaisesRegex(SpecError, "cannot be a Make target"):
            render_makefile(root)

    def test_a_config_without_a_make_table_cannot_render(self):
        with self.assertRaisesRegex(SpecError, "no \\[make\\] table"):
            render_makefile(self.root_config('[x]\nversion = "0.1.0"\n'))

    @unittest.skipIf(os.name == "nt", "the fixtures set SHELL to /bin/bash")
    @unittest.skipUnless(shutil.which("make"), "make is not installed")
    def test_make_accepts_what_is_rendered(self):
        cases = (
            ("declared", DECLARED, "start", "tools/x.sh start"),
            ("sorted", SORTED, "help", "./cli/x help"),
        )
        for name, source, target, expected in cases:
            with self.subTest(name):
                path = self.root / "Makefile"
                write_makefile(path, render_makefile(self.root_config(source)))
                completed = subprocess.run(
                    ["make", "-n", "-f", str(path), target],
                    cwd=self.root,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    check=False,
                )

                self.assertEqual(completed.returncode, 0, completed.stderr)
                self.assertIn(expected, completed.stdout)


class FreshnessTest(MakefileTestCase):
    def test_check_compares_bytes(self):
        path = self.root / "Makefile"
        write_makefile(path, "expected\n")

        self.assertTrue(check_makefile(path, "expected\n"))
        self.assertFalse(check_makefile(path, "different\n"))
        self.assertFalse(check_makefile(self.root / "absent", "expected\n"))

    def test_carriage_returns_count_as_stale(self):
        path = self.root / "Makefile"
        path.write_bytes(b"expected\r\n")

        self.assertFalse(check_makefile(path, "expected\n"))

    def test_write_uses_line_feeds_and_leaves_no_temporary_file(self):
        path = self.root / "Makefile"
        write_makefile(path, "one\ntwo\n")

        self.assertEqual(path.read_bytes(), b"one\ntwo\n")
        self.assertEqual(
            sorted(entry.name for entry in self.root.iterdir()),
            [".git", "Makefile"],
        )

    def test_only_a_file_this_wrote_counts_as_generated(self):
        path = self.root / "Makefile"

        self.assertTrue(is_generated(path))
        path.write_bytes(b"all:\n\techo hand-written\n")
        self.assertFalse(is_generated(path))
        write_makefile(path, BANNER + "\n")
        self.assertTrue(is_generated(path))


if __name__ == "__main__":
    unittest.main()
