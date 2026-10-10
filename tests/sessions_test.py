"""Tests for the resume grammar and on-disk session lookup.

Every session store here is a fixture written into a temporary home
directory in the layout the agent uses. No real agent is involved.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import unittest

from x import sessions
from x.sessions import ResumeError, parse_resume_argv, resume_plan


def _claude_session(home: Path, project: str, session_id: str, cwd: Path) -> Path:
    directory = home / ".claude" / "projects" / project
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / (session_id + ".jsonl")
    path.write_text(
        json.dumps({"type": "summary"})
        + "\n"
        + json.dumps({"type": "user", "cwd": str(cwd), "sessionId": session_id})
        + "\n",
        encoding="utf-8",
    )
    return path


def _codex_session(
    home: Path,
    session_id: str,
    cwd: Path,
    stamp: str = "2000-01-02T03-04-05",
) -> Path:
    directory = home / ".codex" / "sessions" / "2000" / "01" / "02"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / ("rollout-" + stamp + "-" + session_id + ".jsonl")
    path.write_text(
        json.dumps(
            {
                "type": "session_meta",
                "payload": {"session_id": session_id, "cwd": str(cwd)},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    return path


def _agy_session(home: Path, session_id: str) -> Path:
    directory = home / ".gemini" / "antigravity-cli" / "conversations"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / (session_id + ".db")
    path.write_bytes(b"")
    return path


def _kimi_session(home: Path, session_id: str, work_dir: Path | None = None) -> Path:
    # Kimi buckets a session directory under a working-directory key and
    # records the directory once, in a shared index.
    path = home / ".kimi-code" / "sessions" / "wd_fixture_000000000000" / session_id
    (path / "agents" / "main").mkdir(parents=True, exist_ok=True)
    if work_dir is not None:
        index = home / ".kimi-code" / "session_index.jsonl"
        record = {
            "sessionId": session_id,
            "sessionDir": str(path),
            "workDir": str(work_dir),
        }
        with index.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record) + "\n")
    return path


def _pi_session(
    home: Path,
    session_id: str,
    cwd: Path,
    stamp: str = "2000-01-02T03-04-05-000Z",
) -> Path:
    # Pi buckets sessions by working directory and names each file
    # <timestamp>_<id>.jsonl; the header record carries the cwd.
    directory = home / ".pi" / "agent" / "sessions" / "--checkout--"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / (stamp + "_" + session_id + ".jsonl")
    path.write_text(
        json.dumps(
            {
                "type": "session",
                "version": 3,
                "id": session_id,
                "cwd": str(cwd),
            }
        )
        + "\n",
        encoding="utf-8",
    )
    return path


def _grok_session(
    home: Path,
    session_id: str,
    cwd: Path,
    bucket: str = "%2Fcheckout",
) -> Path:
    # Grok buckets sessions by URL-encoded working directory and
    # stores summary.json with info.id / info.cwd.
    directory = home / ".grok" / "sessions" / bucket / session_id
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "summary.json"
    path.write_text(
        json.dumps({"info": {"id": session_id, "cwd": str(cwd)}}),
        encoding="utf-8",
    )
    return path


class HomeTestCase(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name)
        self.checkout = self.home / "checkout"
        self.checkout.mkdir()


class GrammarTest(unittest.TestCase):
    def test_agent_assignment_names_agent_and_session(self):
        request = parse_resume_argv(("CLAUDE=session-one",))

        self.assertEqual(request.agent, sessions.CLAUDE)
        self.assertEqual(request.session_id, "session-one")
        self.assertEqual(request.passthrough, ())

    def test_uuid_and_agent_keys_mirror_the_flags(self):
        keyed = parse_resume_argv(("AGENT=codex", "UUID=session-one"))
        flagged = parse_resume_argv(("--agent", "codex", "--uuid", "session-one"))

        self.assertEqual(keyed, flagged)
        self.assertEqual(keyed.agent, sessions.CODEX)

    def test_session_and_id_spellings_mean_uuid(self):
        expected = parse_resume_argv(("UUID=session-one",))

        for request in (
            ("SESSION=session-one",),
            ("ID=session-one",),
            ("--session", "session-one"),
            ("--id", "session-one"),
            ("--uuid=session-one",),
            ("--session=session-one",),
            ("--id=session-one",),
        ):
            with self.subTest(request):
                self.assertEqual(parse_resume_argv(request), expected)

    def test_bare_id_is_the_session_id(self):
        request = parse_resume_argv(("session-one", "--verbose"))

        self.assertIsNone(request.agent)
        self.assertEqual(request.session_id, "session-one")
        self.assertEqual(request.passthrough, ("--verbose",))

    def test_unrecognized_assignment_is_passed_through(self):
        request = parse_resume_argv(("UUID=session-one", "MODEL=large"))

        self.assertEqual(request.passthrough, ("MODEL=large",))

    def test_keys_are_matched_without_regard_to_case(self):
        for spelling in ("CLAUDE", "claude", "Claude", "cLaUdE"):
            with self.subTest(spelling):
                request = parse_resume_argv((spelling + "=session-one",))

                self.assertEqual(request.agent, sessions.CLAUDE)
                self.assertEqual(request.passthrough, ())

    def test_each_agent_answers_to_its_product_name(self):
        expected = {
            "claude-code": sessions.CLAUDE,
            "claude_code": sessions.CLAUDE,
            "antigravity": sessions.AGY,
            "kimi-code": sessions.KIMI,
            "grok-build": sessions.GROK,
            "pi": sessions.PI,
        }

        for name, agent in expected.items():
            with self.subTest(name):
                self.assertEqual(sessions.resolve_agent(name), agent)
                self.assertEqual(sessions.resolve_agent(name.upper()), agent)

    def test_conflicting_agents_are_rejected(self):
        with self.assertRaisesRegex(ResumeError, "conflicting agents"):
            parse_resume_argv(("CLAUDE=session-one", "CODEX=session-two"))

    def test_conflicting_session_ids_are_rejected(self):
        with self.assertRaisesRegex(ResumeError, "conflicting session ids"):
            parse_resume_argv(("UUID=session-one", "CLAUDE=session-two"))

    def test_unknown_agent_is_rejected_with_the_names_to_choose_from(self):
        with self.assertRaises(ResumeError) as caught:
            parse_resume_argv(("AGENT=nowhere", "UUID=session-one"))

        message = str(caught.exception)
        self.assertIn("unknown agent 'nowhere'", message)
        for agent in sessions.BUILTIN_AGENTS:
            self.assertIn(agent, message)

    def test_assignment_keys_are_what_the_parser_consumes(self):
        for key in sessions.assignment_keys():
            # `agent=` takes an agent name; every other key takes a
            # session id.
            value = "probe-id"
            if key == "agent":
                value = "claude"
            request = parse_resume_argv((key + "=" + value,))

            self.assertEqual(request.passthrough, (), msg=key + " reached passthrough")

        for name in sessions.agent_names():
            self.assertIn(name, sessions.assignment_keys())
        for key in ("uuid", "session", "id", "agent"):
            self.assertIn(key, sessions.assignment_keys())

        # A key outside the set is passed through untouched.
        for key in ("model", "effort", "profile"):
            request = parse_resume_argv(("UUID=probe-id", key + "=value"))

            self.assertEqual(request.passthrough, (key + "=value",))


class CustomAgentTest(HomeTestCase):
    def test_custom_agent_and_aliases_join_the_grammar(self):
        grammar = sessions.grammar(
            custom=("helper",), aliases={"hp": "helper", "cl": "claude"}
        )

        self.assertEqual(
            sessions.agent_names(grammar), sessions.BUILTIN_AGENTS + ("helper",)
        )
        self.assertEqual(parse_resume_argv(("HP=one",), grammar).agent, "helper")
        self.assertEqual(parse_resume_argv(("cl=one",), grammar).agent, sessions.CLAUDE)
        # Without the grammar the same token is just an argument.
        self.assertEqual(parse_resume_argv(("HP=one", "two")).passthrough, ("HP=one",))

    def test_custom_agent_is_planned_without_touching_the_disk(self):
        grammar = sessions.grammar(custom=("helper",))

        plan = resume_plan(("HELPER=session-one",), self.home, self.checkout, grammar)

        self.assertEqual(plan.agent, "helper")
        self.assertEqual(plan.session_id, "session-one")
        # Its arguments are the caller's to supply.
        self.assertEqual(plan.arguments, ())
        self.assertEqual(plan.cwd, self.checkout)

    def test_custom_agent_last_is_carried_through_in_any_case(self):
        grammar = sessions.grammar(custom=("helper",))

        for spelling in ("HELPER=last", "HELPER=LAST", "helper=Last"):
            with self.subTest(spelling):
                plan = resume_plan((spelling,), self.home, self.checkout, grammar)

                self.assertEqual(plan.session_id, sessions.LATEST)

    def test_bare_id_never_finds_a_custom_agent_and_the_error_says_so(self):
        grammar = sessions.grammar(custom=("helper",))

        with self.assertRaises(ResumeError) as caught:
            resume_plan(("session-missing",), self.home, self.checkout, grammar)

        message = str(caught.exception)
        self.assertIn("no session found for id session-missing", message)
        self.assertIn("HELPER=<id>", message)
        self.assertIn("Sessions of helper are never searched for", message)

    def test_names_cannot_collide_or_be_reserved(self):
        with self.assertRaisesRegex(ResumeError, "built in"):
            sessions.grammar(custom=("claude",))
        with self.assertRaisesRegex(ResumeError, "belongs to claude and codex"):
            sessions.grammar(aliases={"claude-code": "codex"})
        with self.assertRaisesRegex(ResumeError, "reserved key"):
            sessions.grammar(custom=("uuid",))
        with self.assertRaisesRegex(ResumeError, "names no agent"):
            sessions.grammar(aliases={"hp": "helper"})
        with self.assertRaisesRegex(ResumeError, "not usable as an agent name"):
            sessions.grammar(custom=("two words",))


class LookupTest(HomeTestCase):
    def test_bare_id_resolves_claude_and_its_directory(self):
        _claude_session(self.home, "-checkout", "session-one", self.checkout)

        plan = resume_plan(("session-one",), self.home, self.home)

        self.assertEqual(plan.agent, sessions.CLAUDE)
        self.assertEqual(plan.arguments, ("--resume", "session-one"))
        self.assertEqual(plan.cwd, self.checkout)

    def test_bare_id_resolves_codex_with_recorded_directory(self):
        _codex_session(self.home, "00000000-0000-4000-8000-000000000002", self.checkout)

        plan = resume_plan(
            ("00000000-0000-4000-8000-000000000002",), self.home, self.home
        )

        self.assertEqual(plan.agent, sessions.CODEX)
        self.assertEqual(
            plan.arguments,
            (
                "-C",
                str(self.checkout),
                "resume",
                "00000000-0000-4000-8000-000000000002",
            ),
        )

    def test_bare_id_resolves_antigravity_which_records_no_directory(self):
        _agy_session(self.home, "session-three")

        plan = resume_plan(("session-three",), self.home, self.home)

        self.assertEqual(plan.agent, sessions.AGY)
        self.assertEqual(plan.arguments, ("--conversation", "session-three"))
        self.assertEqual(plan.cwd, self.home)

    def test_bare_id_resolves_kimi_with_indexed_directory(self):
        _kimi_session(self.home, "session_fixture-four", self.checkout)

        plan = resume_plan(("session_fixture-four",), self.home, self.home)

        self.assertEqual(plan.agent, sessions.KIMI)
        self.assertEqual(plan.arguments, ("--session", "session_fixture-four"))
        # The directory comes from session_index.jsonl, not from a
        # scan of the transcript head.
        self.assertEqual(plan.cwd, self.checkout)

    def test_kimi_session_without_an_index_entry_falls_back_to_the_root(self):
        _kimi_session(self.home, "session_no-index")

        plan = resume_plan(("session_no-index",), self.home, self.home)

        self.assertEqual(plan.agent, sessions.KIMI)
        self.assertEqual(plan.cwd, self.home)

    def test_bare_id_resolves_pi_with_header_directory(self):
        _pi_session(self.home, "fixture-five", self.checkout)

        plan = resume_plan(("fixture-five",), self.home, self.home)

        self.assertEqual(plan.agent, sessions.PI)
        self.assertEqual(plan.arguments, ("--session", "fixture-five"))
        # The directory comes from the `type: "session"` header
        # record at the head of the transcript.
        self.assertEqual(plan.cwd, self.checkout)

    def test_bare_id_resolves_grok_with_summary_directory(self):
        _grok_session(self.home, "fixture-six", self.checkout)

        plan = resume_plan(("fixture-six",), self.home, self.home)

        self.assertEqual(plan.agent, sessions.GROK)
        self.assertEqual(plan.arguments, ("--resume", "fixture-six"))
        self.assertEqual(plan.cwd, self.checkout)

    def test_recorded_directory_that_no_longer_exists_falls_back_to_the_root(self):
        _claude_session(self.home, "-gone", "session-one", self.home / "gone")

        plan = resume_plan(("session-one",), self.home, self.checkout)

        self.assertEqual(plan.cwd, self.checkout)

    def test_named_agent_needs_no_file_on_disk(self):
        plan = resume_plan(("CLAUDE=session-elsewhere",), self.home, self.checkout)

        self.assertEqual(plan.agent, sessions.CLAUDE)
        self.assertEqual(plan.arguments, ("--resume", "session-elsewhere"))
        self.assertEqual(plan.cwd, self.checkout)

    def test_unknown_id_names_every_agent_key(self):
        with self.assertRaises(ResumeError) as caught:
            resume_plan(("session-missing",), self.home, self.home)

        message = str(caught.exception)
        self.assertIn("no session found for id session-missing", message)
        for agent in sessions.BUILTIN_AGENTS:
            self.assertIn(agent.upper() + "=<id>", message)

    def test_id_in_two_stores_is_ambiguous(self):
        _claude_session(self.home, "-checkout", "session-four", self.home)
        _codex_session(self.home, "session-four", self.home)

        with self.assertRaisesRegex(ResumeError, "matches claude, codex"):
            resume_plan(("session-four",), self.home, self.home)

    def test_named_agent_wins_over_ambiguity(self):
        _claude_session(self.home, "-checkout", "session-four", self.home)
        _codex_session(self.home, "session-four", self.home)

        plan = resume_plan(("CLAUDE=session-four",), self.home, self.home)

        self.assertEqual(plan.agent, sessions.CLAUDE)

    def test_last_prefers_a_session_from_this_repository(self):
        here = self.home / "here"
        elsewhere = self.home / "elsewhere"
        here.mkdir()
        elsewhere.mkdir()
        older = _claude_session(self.home, "-here", "session-here", here)
        newer = _claude_session(self.home, "-elsewhere", "session-away", elsewhere)
        os.utime(older, (1_600_000_000, 1_600_000_000))
        os.utime(newer, (1_700_000_000, 1_700_000_000))

        plan = resume_plan(("CLAUDE=last",), self.home, here)

        self.assertEqual(plan.session_id, "session-here")
        self.assertEqual(plan.cwd, here)

    def test_last_falls_back_to_the_newest_session(self):
        elsewhere = self.home / "elsewhere"
        elsewhere.mkdir()
        _claude_session(self.home, "-elsewhere", "session-away", elsewhere)

        plan = resume_plan(("CLAUDE=last",), self.home, self.home / "here")

        self.assertEqual(plan.session_id, "session-away")

    def test_last_with_no_sessions_says_so(self):
        with self.assertRaisesRegex(ResumeError, "no claude sessions found"):
            resume_plan(("CLAUDE=last",), self.home, self.home)

    def test_last_without_an_agent_is_rejected(self):
        with self.assertRaisesRegex(ResumeError, "needs an agent"):
            resume_plan(("UUID=last",), self.home, self.home)

    def test_missing_session_id_lists_the_ways_to_give_one(self):
        with self.assertRaises(ResumeError) as caught:
            resume_plan(("--verbose",), self.home, self.home)

        message = str(caught.exception)
        self.assertIn("missing session id", message)
        self.assertIn("UUID=<id>", message)
        self.assertIn("GROK=<id>", message)


if __name__ == "__main__":
    unittest.main()
