"""The release surface, asserted rather than assumed.

These are the checks a reviewer would otherwise make by eye, and
the ones a rename or a version bump quietly breaks.
"""

from __future__ import annotations

import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import unittest

import x
from x import commands, config
from x.main import main


ROOT = Path(__file__).resolve().parent.parent
NAME = "x"
SKILL_DIR = ROOT / "skills" / NAME
SKILL_FILE = SKILL_DIR / "SKILL.md"
NPM_NAMES = ("@tryc.ai/x", "@trycopilotai/x", "@trycopilot.ai/x", "@trycai/x")


def _read(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def _json(relative: str) -> dict:
    return json.loads(_read(relative))


def _git(*argv: str) -> str | None:
    """Output of a git command here, or None outside a usable checkout."""
    try:
        completed = subprocess.run(
            ["git", "-C", str(ROOT), *argv],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    except OSError:
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout.decode("utf-8", "replace")


def _frontmatter() -> tuple[str, str]:
    text = SKILL_FILE.read_text(encoding="utf-8")
    assert text.startswith("---\n")
    end = text.index("\n---\n", 3)
    return text[4:end], text[end + 5 :]


class SkillTest(unittest.TestCase):
    def test_frontmatter_carries_name_and_description_only(self):
        raw, _ = _frontmatter()

        self.assertEqual(
            re.findall(r"^([a-z_]+):", raw, flags=re.MULTILINE),
            ["name", "description"],
        )

    def test_name_matches_the_directory(self):
        raw, _ = _frontmatter()
        name = re.search(r"^name:\s*(\S+)", raw, flags=re.MULTILINE).group(1)

        self.assertEqual(name, NAME)
        self.assertEqual(name, SKILL_DIR.name)
        self.assertRegex(name, r"^[a-z0-9]+(-[a-z0-9]+)*$")
        self.assertLessEqual(len(name), 64)

    def test_description_says_when_to_use_it_and_fits_the_limit(self):
        raw, _ = _frontmatter()
        block = raw.split("description:", 1)[1].replace(">-", "", 1)
        description = " ".join(line.strip() for line in block.strip().splitlines())

        self.assertTrue(description)
        self.assertLessEqual(len(description), 1024)
        self.assertIn("Use when", description)

    def test_skill_stays_under_the_length_guideline(self):
        self.assertLess(len(SKILL_FILE.read_text(encoding="utf-8").splitlines()), 500)

    def test_skill_links_are_absolute_and_pinned(self):
        _, body = _frontmatter()

        # The skill is copied out of this repository when installed,
        # so a relative link would point at nothing.
        self.assertEqual(re.findall(r"\]\(([^)]+)\)", body), [])
        links = re.findall(r"<(https?://[^>]+)>", body)
        self.assertEqual(
            links,
            [
                "https://github.com/trycopilotai/x/blob/v"
                + x.__version__
                + "/docs/reference.md"
            ],
        )
        self.assertTrue((ROOT / "docs" / "reference.md").is_file())

    def test_symlink_points_into_the_regular_directory(self):
        self.assertFalse(SKILL_DIR.is_symlink())
        mode = _git("ls-files", "-s", "--", "skill")
        if mode:
            # Checked in the index so a checkout without symlink
            # support still verifies what the repository stores.
            self.assertTrue(mode.startswith("120000 "), mode)
        link = ROOT / "skill"
        if link.is_symlink():
            # Git writes the target with the host's separator.
            self.assertEqual(os.readlink(link).replace("\\", "/"), "skills/" + NAME)
            self.assertTrue((link / "SKILL.md").exists())

    def test_codex_agent_file_offers_the_dollar_invocation(self):
        self.assertIn("$" + NAME, _read("skills/x/agents/openai.yaml"))

    def test_skill_names_the_builtins_the_engine_has(self):
        names = [spec.name for spec in commands.specs(None, ())]
        _, body = _frontmatter()
        sentence = re.search(r"built-ins are (.+?)\.", body.replace("\n", " ")).group(1)

        self.assertEqual(re.findall(r"`([a-z]+)`", sentence), names)


class ManifestTest(unittest.TestCase):
    def test_both_product_manifests_agree(self):
        claude = _json(".claude-plugin/plugin.json")
        codex = _json(".codex-plugin/plugin.json")

        for manifest in (claude, codex):
            self.assertEqual(manifest["name"], NAME)
            self.assertEqual(manifest["skills"], "./skills/")
            self.assertEqual(manifest["license"], "MIT")
        self.assertEqual(claude["version"], codex["version"])
        self.assertEqual(claude["description"], codex["description"])
        self.assertIn("interface", codex)
        self.assertNotIn("interface", claude)
        self.assertIn("$" + NAME, codex["interface"]["defaultPrompt"][0])

    def test_version_is_identical_everywhere(self):
        version = x.__version__
        tag = "v" + version

        self.assertEqual(_json("package.json")["version"], version)
        self.assertEqual(_json(".claude-plugin/plugin.json")["version"], version)
        self.assertEqual(_json(".codex-plugin/plugin.json")["version"], version)
        self.assertEqual(config.load_workspace(ROOT).root.version, version)

        documents = ("README.md", "skills/x/SKILL.md", "docs/reference.md")
        pinned = 0
        for relative in documents:
            text = _read(relative)
            for found in re.findall(r"@tryc\.ai/x@([0-9][0-9A-Za-z.-]*)", text):
                self.assertEqual(found.rstrip("."), version, relative)
                pinned += 1
            for found in re.findall(r"\brelease=(v[0-9][0-9A-Za-z.-]*)", text):
                self.assertEqual(found, tag, relative)
                pinned += 1
            for found in re.findall(r"/blob/(v[0-9][0-9A-Za-z.-]*)/", text):
                self.assertEqual(found, tag, relative)
                pinned += 1
            for found in re.findall(r'^version = "([^"]+)"', text, flags=re.MULTILINE):
                self.assertEqual(found, version, relative)
                pinned += 1
        # Guards the patterns themselves: a rewording that stopped
        # them matching would otherwise pass by checking nothing.
        self.assertGreaterEqual(pinned, 9)

        described = _git("describe", "--tags", "--exact-match", "HEAD")
        if described:
            self.assertEqual(described.strip(), tag)

    def test_license_is_mit_and_names_the_organisation(self):
        text = _read("LICENSE")

        self.assertIn("MIT License", text)
        self.assertIn("Try Copilot AI", text)
        self.assertEqual(_json("package.json")["license"], "MIT")

    def test_text_people_read_says_license_after_mit(self):
        # The bare word names a university. The licence identifier
        # fields of package.json and the plugin manifests are read by
        # programs and must stay exactly "MIT"; everything written for
        # a person says which thing is meant.
        for relative in ("README.md", "assets/social-preview.html", "SECURITY.md"):
            text = _read(relative)
            for found in re.finditer(r"\bMIT\b(?! [Ll]icen[sc]e)", text):
                self.fail(relative + ": bare MIT at offset " + str(found.start()))
        self.assertIn("MIT license", _read("README.md"))
        self.assertIn("MIT license", _read("assets/social-preview.html"))


class ReadmeTest(unittest.TestCase):
    def test_documents_both_products_and_every_invocation(self):
        text = _read("README.md")

        self.assertIn("### Claude Code", text)
        self.assertIn("### Codex", text)
        for form in ("/x ", "/x:x ", "$x ", "`@x`"):
            self.assertIn(form, text)

    def test_install_blocks_are_one_script_with_both_refusals_before_the_write(self):
        text = _read("README.md")
        blocks = re.findall(r"```sh\n(\(\n  set -eu\n.+?\n\))\n```", text, flags=re.DOTALL)

        self.assertEqual(len(blocks), 2)
        for block in blocks:
            self.assertEqual(block.count("Refusing to replace non-symlink"), 1)
            self.assertEqual(block.count("Refusing to replace another link"), 1)
            # Both refusals come before the one line that writes.
            self.assertLess(block.rindex("Refusing"), block.index("ln -sfn"))
        self.assertIn("$HOME/.claude/skills/x", blocks[0])
        self.assertIn("$HOME/.agents/skills/x", blocks[1])
        # Apart from the two folders, the blocks are the same script.
        self.assertEqual(
            blocks[0].replace(".claude/plugins/", "P").replace(".claude/skills/", "S"),
            blocks[1].replace(".codex/plugins/", "P").replace(".agents/skills/", "S"),
        )

    def test_names_every_npm_package(self):
        text = _read("README.md")

        for name in NPM_NAMES:
            self.assertIn(name, text)

    def test_builtin_table_matches_the_engine(self):
        names = [spec.name for spec in commands.specs(None, ())]
        section = _read("README.md").split("## Built-in commands", 1)[1]
        section = section.split("\n## ", 1)[0]

        self.assertEqual(re.findall(r"^\| `([a-z]+)`", section, flags=re.MULTILINE), names)

    def test_example_makefile_is_what_the_example_config_renders(self):
        from x.makefile import render_makefile

        text = _read("README.md")
        source = re.search(r"```toml\n# x\.toml\n(.+?)```", text, flags=re.DOTALL).group(1)
        expected = re.search(
            r"```make\n(# Generated by x.+?)```", text, flags=re.DOTALL
        ).group(1)
        document = __import__("tomllib").loads(source)
        root = config.load_root(ROOT / "x.toml", document)

        self.assertEqual(render_makefile(root), expected)


class ReferenceTest(unittest.TestCase):
    def test_every_key_the_loader_reads_is_documented(self):
        text = _read("docs/reference.md")
        source = _read("x/config.py")
        # The loader reads each key through one of these calls, so the
        # list comes from the loader and not from this test.
        read = set(
            re.findall(
                r'\.(?:string|boolean|strings|take|child|has)\(\s*"([a-z_]+)"', source
            )
        )
        read.update(re.findall(r'_env_names\(\w+, "([a-z_]+)"\)', source))
        read.update(config.HANDLER_KINDS)
        tables = {
            "x": "[x]",
            "flags": "[flags.<key>]",
            "commands": "[commands.<name>]",
            "defaults": "[defaults]",
            "unknown": "[unknown]",
            "agents": "[resume.agents.<agent>]",
        }

        self.assertGreaterEqual(len(read), 30)
        for key in sorted(read):
            documented = tables.get(key, key)
            self.assertIn("`" + documented + "`", text, key)
        self.assertIn("`[make]`", text)

    def test_documented_choices_are_the_ones_the_loader_accepts(self):
        text = _read("docs/reference.md")

        for naming in config.NAMINGS:
            self.assertIn("`" + naming + "`", text)
        for keyword in config.CWD_KEYWORDS:
            self.assertIn('`"' + keyword + '"`', text)
        for kind in config.HANDLER_KINDS:
            self.assertIn("| `" + kind + "`", text)

    def test_builtin_table_matches_the_engine(self):
        names = [spec.name for spec in commands.specs(None, ())]
        section = _read("docs/reference.md").split("## Built-in commands", 1)[1]
        section = section.split("\n#", 1)[0]

        self.assertEqual(re.findall(r"^\| `([a-z]+)`", section, flags=re.MULTILINE), names)


class RepositoryTest(unittest.TestCase):
    def test_own_makefile_is_current(self):
        stderr = io.StringIO()

        status = main(
            ["--builtin", "makefile", "--check"],
            cwd=ROOT,
            environ={},
            stdout=io.StringIO(),
            stderr=stderr,
        )

        self.assertEqual(status, 0, stderr.getvalue())

    def test_release_script_publishes_the_documented_names(self):
        source = _read("scripts/release.py")
        listed = re.search(r"^NAMES = \((.+?)\)$", source, flags=re.MULTILINE).group(1)

        self.assertEqual(tuple(re.findall(r'"([^"]+)"', listed)), NPM_NAMES)
        self.assertEqual(_json("package.json")["name"], NPM_NAMES[0])


@unittest.skipUnless(shutil.which("npm"), "npm is not installed")
class NpmPackageTest(unittest.TestCase):
    def test_package_ships_the_tool_its_reference_and_its_documents(self):
        completed = subprocess.run(
            [shutil.which("npm"), "pack", "--dry-run", "--json"],
            cwd=ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=True,
        )
        shipped = sorted(entry["path"] for entry in json.loads(completed.stdout)[0]["files"])
        modules = sorted(
            "x/" + path.name for path in (ROOT / "x").iterdir() if path.suffix == ".py"
        )

        self.assertEqual(
            shipped,
            sorted(
                [
                    "LICENSE",
                    "README.md",
                    "__main__.py",
                    "bin/x.js",
                    "docs/reference.md",
                    "package.json",
                ]
                + modules
            ),
        )

    def test_package_has_no_dependencies_or_install_scripts(self):
        package = _json("package.json")

        for key in ("dependencies", "optionalDependencies", "peerDependencies", "scripts"):
            self.assertNotIn(key, package)
        self.assertEqual(package["bin"], {"x": "bin/x.js"})


if __name__ == "__main__":
    unittest.main()
