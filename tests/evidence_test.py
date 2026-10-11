"""The evidence, tied to the bytes it describes.

A transcript that no longer matches the tool, a manifest with a
stale hash, or an image that shows something the transcript does not
would each be a false statement in the repository. These tests are
where that is caught.
"""

from __future__ import annotations

import hashlib
import html
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import struct
import sys
import unittest


ROOT = Path(__file__).resolve().parent.parent
MANIFEST = ROOT / "evidence" / "demo-manifest.json"


def _load(relative: str, name: str):
    previous = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        spec = importlib.util.spec_from_file_location(name, ROOT / relative)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = previous
    return module


def _sha256(relative: str) -> str:
    return hashlib.sha256((ROOT / relative).read_bytes()).hexdigest()


def _manifest() -> dict:
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


class ManifestTest(unittest.TestCase):
    def test_every_recorded_hash_is_the_hash_of_the_file(self):
        manifest = _manifest()
        bound = [manifest["skill"], manifest["interface"], manifest["output"]]
        bound.extend(manifest["programs"])

        for entry in bound:
            with self.subTest(entry["path"]):
                self.assertEqual(
                    entry["sha256"],
                    _sha256(entry["path"]),
                    entry["path"] + " changed after the evidence was recorded; run "
                    "`make record` and then `make demo`, and commit the result",
                )

    def test_programs_are_exactly_the_files_of_the_tool(self):
        recorder = _load("scripts/record_session.py", "record_session")
        listed = [entry["path"] for entry in _manifest()["programs"]]
        package = json.loads((ROOT / "package.json").read_text(encoding="utf-8"))

        self.assertEqual(listed, recorder.program_paths())
        # The same set the npm package ships, less its documents.
        self.assertIn("bin/x.js", package["files"])
        self.assertIn("__main__.py", package["files"])
        self.assertIn("x/*.py", package["files"])
        self.assertEqual(
            sorted(path for path in listed if path.startswith("x/")),
            sorted("x/" + path.name for path in (ROOT / "x").glob("*.py")),
        )

    def test_it_says_no_agent_ran_and_nothing_was_edited(self):
        manifest = _manifest()

        self.assertIs(manifest["agent"]["invoked_the_skill"], False)
        self.assertIs(manifest["output"]["edited"], False)
        self.assertEqual(manifest["output"]["transforms"], [])

    def test_transcript_commands_are_the_manifests_commands(self):
        manifest = _manifest()
        transcript = (ROOT / manifest["output"]["path"]).read_text(encoding="utf-8")
        commands = [
            line[2:] for line in transcript.splitlines() if line.startswith("$ ")
        ]

        self.assertEqual(commands, manifest["invocation"]["commands"])


@unittest.skipIf(os.name == "nt", "the session uses sh, make, printf and tail")
@unittest.skipUnless(shutil.which("node"), "node is not installed")
@unittest.skipUnless(shutil.which("make"), "make is not installed")
class ReplayTest(unittest.TestCase):
    def test_running_the_session_again_prints_the_same_transcript(self):
        recorder = _load("scripts/record_session.py", "record_session")
        manifest = _manifest()
        transcript = (ROOT / manifest["output"]["path"]).read_text(encoding="utf-8")

        raw, failed = recorder.capture(
            manifest["invocation"]["commands"], shutil.which("node")
        )

        self.assertFalse(failed, raw)
        self.assertEqual(raw, transcript)


class ImageTest(unittest.TestCase):
    def test_demo_and_poster_are_a_fresh_render_of_the_transcript(self):
        generator = _load("scripts/generate_demo.py", "generate_demo")
        transcript = generator.TRANSCRIPT.read_text(encoding="utf-8")

        self.assertEqual(
            generator.DEMO.read_text(encoding="utf-8"),
            generator.render(transcript, animated=True),
        )
        self.assertEqual(
            generator.POSTER.read_text(encoding="utf-8"),
            generator.render(transcript, animated=False),
        )

    def test_the_verifier_finds_nothing_wrong(self):
        verifier = _load("scripts/verify_demo.py", "verify_demo")
        generator = verifier.load_generator()
        transcript = generator.TRANSCRIPT.read_text(encoding="utf-8")

        self.assertEqual(verifier.problems_in(generator, transcript), [])

    def test_the_verifier_notices_a_line_the_transcript_lacks(self):
        verifier = _load("scripts/verify_demo.py", "verify_demo")
        generator = verifier.load_generator()
        transcript = generator.TRANSCRIPT.read_text(encoding="utf-8")

        problems = verifier.problems_in(generator, transcript + "one more line\n")

        self.assertIn("demo.svg differs from a fresh render", problems)
        self.assertIn("demo.svg does not show the transcript line for line", problems)

    def test_readme_describes_the_demo_in_the_images_own_words(self):
        text = (ROOT / "README.md").read_text(encoding="utf-8")
        demo = (ROOT / "assets" / "demo.svg").read_text(encoding="utf-8")
        alt = re.search(r'alt="([^"]+)"', text).group(1)
        description = re.search(r"<desc[^>]*>([^<]+)</desc>", demo).group(1)

        self.assertEqual(alt, html.unescape(description))

    def test_readme_shows_the_demo_with_a_reduced_motion_poster(self):
        text = (ROOT / "README.md").read_text(encoding="utf-8")

        self.assertIn('src="assets/demo.svg"', text)
        self.assertIn('media="(prefers-reduced-motion: reduce)"', text)
        self.assertIn('srcset="assets/poster.svg"', text)
        self.assertIn("evidence/transcripts/session.txt", text)

    def test_social_preview_is_the_stamped_render_at_githubs_size(self):
        recorded = {}
        for line in (
            (ROOT / "assets" / "social-preview.sha256").read_text().splitlines()
        ):
            if line.strip():
                value, name = line.split()
                recorded[name] = value

        for name in ("social-preview.html", "social-preview.png"):
            with self.subTest(name):
                self.assertEqual(recorded[name], _sha256("assets/" + name))
        png = (ROOT / "assets" / "social-preview.png").read_bytes()
        self.assertEqual(png[:8], b"\x89PNG\r\n\x1a\n")
        self.assertEqual(struct.unpack(">II", png[16:24]), (1280, 640))


if __name__ == "__main__":
    unittest.main()
