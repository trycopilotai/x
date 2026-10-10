"""Coding-agent session lookup and the `resume` request grammar.

    resume <id>                  find which agent recorded the session
    resume UUID=<id>             the same
    resume CLAUDE=<id>           name the agent; any agent name works as the key
    resume CLAUDE=last           that agent's newest session
    resume --agent claude <id>   the agent named with a flag instead

A key is matched without regard to case. `SESSION=` and `ID=` mean
the same as `UUID=`. `--uuid`, `--session` and `--id` take the id as
the next argument or after `=`, and `AGENT=` means the same as
`--agent`.

Six agents keep their sessions in files under the home directory in a
layout this module reads, so a bare id names one of them by being
found there: Claude Code, Codex, Antigravity, Kimi Code, Pi and Grok
Build.

Any other agent is a custom agent: it is declared by the caller, has
to be named in the request, and nothing on disk is searched for it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
import re
from typing import Iterable, Iterator, Mapping


CLAUDE = "claude"
CODEX = "codex"
AGY = "agy"
KIMI = "kimi"
PI = "pi"
GROK = "grok"

LATEST = "last"

# The agents whose session stores are read, in the order they are
# listed to the user.
BUILTIN_AGENTS = (CLAUDE, CODEX, AGY, KIMI, PI, GROK)

# Each agent answers to its own name and to its product's name.
_BUILTIN_ALIASES: dict[str, str] = {
    "claude": CLAUDE,
    "claude-code": CLAUDE,
    "claude_code": CLAUDE,
    "codex": CODEX,
    "agy": AGY,
    "antigravity": AGY,
    "kimi": KIMI,
    "kimi-code": KIMI,
    "kimi_code": KIMI,
    "pi": PI,
    "grok": GROK,
    "grok-build": GROK,
    "grok_build": GROK,
}

_SESSION_KEYS = ("uuid", "session", "id")
_AGENT_KEY = "agent"

_ASSIGNMENT = re.compile(r"^(?P<key>[A-Za-z][A-Za-z0-9_-]*)=(?P<value>.*)$")
_SESSION_ID = re.compile(r"^[0-9A-Za-z][0-9A-Za-z._-]*$")
_AGENT_NAME = re.compile(r"^[a-z][a-z0-9_-]*$")

# Reading a whole transcript to learn its directory is wasteful; the
# record carrying `cwd` appears in the first handful of lines.
_CWD_SCAN_LINES = 200
# Newest-first scan bound when preferring a session from this
# repository over the newest one anywhere.
_LATEST_SCAN_LIMIT = 200


class ResumeError(ValueError):
    """A resume request cannot be satisfied."""


@dataclass(frozen=True)
class Grammar:
    """The agent names a request may use.

    `aliases` maps every accepted name, in lower case, to its agent.
    `custom` holds the agents that were declared by the caller and
    have no session store this module reads.
    """

    aliases: Mapping[str, str] = field(default_factory=lambda: dict(_BUILTIN_ALIASES))
    custom: tuple[str, ...] = ()

    def agents(self) -> tuple[str, ...]:
        return BUILTIN_AGENTS + self.custom


def is_builtin(name: str) -> bool:
    return name.strip().lower() in _BUILTIN_ALIASES


def builtin_agent(name: str) -> str:
    return _BUILTIN_ALIASES[name.strip().lower()]


def grammar(
    custom: Iterable[str] = (),
    aliases: Mapping[str, str] | None = None,
) -> Grammar:
    """Build a grammar with custom agents and extra names for agents.

    `aliases` maps an extra name to an agent, built-in or custom.
    Names are folded to lower case. A name may belong to one agent
    only, and may not be a key the grammar already gives a meaning.
    """
    accepted = dict(_BUILTIN_ALIASES)
    declared: list[str] = []
    reserved = set(_SESSION_KEYS) | {_AGENT_KEY}

    def claim(name: str, agent: str) -> None:
        folded = name.strip().lower()
        if not _AGENT_NAME.match(folded):
            raise ResumeError("not usable as an agent name: " + name)
        if folded in reserved:
            raise ResumeError("agent name is a reserved key: " + name)
        owner = accepted.get(folded)
        if owner is not None and owner != agent:
            raise ResumeError(
                "agent name '" + folded + "' belongs to " + owner + " and " + agent
            )
        accepted[folded] = agent

    for name in custom:
        folded = name.strip().lower()
        if folded in _BUILTIN_ALIASES:
            raise ResumeError("agent name is built in: " + name)
        claim(folded, folded)
        declared.append(folded)
    if aliases is not None:
        for name, agent in aliases.items():
            if agent not in BUILTIN_AGENTS and agent not in declared:
                raise ResumeError("alias '" + name + "' names no agent: " + agent)
            claim(name, agent)
    return Grammar(aliases=accepted, custom=tuple(declared))


DEFAULT_GRAMMAR = Grammar()


@dataclass(frozen=True)
class ResumeRequest:
    agent: str | None
    session_id: str
    passthrough: tuple[str, ...]


@dataclass(frozen=True)
class Session:
    agent: str
    session_id: str
    path: Path
    cwd: Path | None


@dataclass(frozen=True)
class ResumePlan:
    agent: str
    session_id: str
    # What the agent's own command line needs to resume the session.
    # Empty for a custom agent, whose arguments the caller supplies.
    arguments: tuple[str, ...]
    cwd: Path | None


def agent_names(selected: Grammar = DEFAULT_GRAMMAR) -> tuple[str, ...]:
    return selected.agents()


def assignment_keys(selected: Grammar = DEFAULT_GRAMMAR) -> tuple[str, ...]:
    """Every `KEY=` a resume request accepts, in lower case.

    A key is matched without regard to case, so this is a set of
    names and not of spellings, and no finite list of spellings
    covers it. A program that wraps `resume` should therefore pass
    the arguments on whole: one that forwards only some of these keys
    does not fail on the others, it silently drops them.
    """
    keys = set(selected.aliases)
    keys.update(_SESSION_KEYS)
    keys.add(_AGENT_KEY)
    return tuple(sorted(keys))


def resolve_agent(name: str, selected: Grammar = DEFAULT_GRAMMAR) -> str:
    resolved = selected.aliases.get(name.strip().lower())
    if resolved is None:
        raise ResumeError(
            "unknown agent '" + name + "'; use " + ", ".join(agent_names(selected))
        )
    return resolved


def home_directory(environ: Mapping[str, str]) -> Path:
    home = environ.get("HOME", "")
    if home:
        return Path(home)
    return Path.home()


def _assign_agent(current: str | None, requested: str, selected: Grammar) -> str:
    resolved = resolve_agent(requested, selected)
    if current is not None and current != resolved:
        raise ResumeError(
            "conflicting agents requested: " + current + " and " + resolved
        )
    return resolved


def _assign_session(current: str, requested: str) -> str:
    value = requested.strip()
    if not value:
        raise ResumeError("empty session id")
    if not _SESSION_ID.match(value):
        raise ResumeError("invalid session id: " + value)
    if current and current != value:
        raise ResumeError("conflicting session ids: " + current + " and " + value)
    return value


def parse_resume_argv(
    argv: Iterable[str], selected: Grammar = DEFAULT_GRAMMAR
) -> ResumeRequest:
    agent: str | None = None
    session_id = ""
    passthrough: list[str] = []
    arguments = list(argv)
    index = 0

    while index < len(arguments):
        token = arguments[index]
        assignment = _ASSIGNMENT.match(token)

        if token == "--agent":
            index += 1
            if index >= len(arguments):
                raise ResumeError("--agent requires a value")
            agent = _assign_agent(agent, arguments[index], selected)
        elif token.startswith("--agent="):
            agent = _assign_agent(agent, token[len("--agent=") :], selected)
        elif token in ("--uuid", "--session", "--id"):
            index += 1
            if index >= len(arguments):
                raise ResumeError(token + " requires a value")
            session_id = _assign_session(session_id, arguments[index])
        elif token.startswith(("--uuid=", "--session=", "--id=")):
            session_id = _assign_session(session_id, token.partition("=")[2])
        elif assignment is not None and not token.startswith("-"):
            key = assignment.group("key").lower()
            value = assignment.group("value")
            if key in selected.aliases:
                agent = _assign_agent(agent, key, selected)
                session_id = _assign_session(session_id, value)
            elif key in _SESSION_KEYS:
                session_id = _assign_session(session_id, value)
            elif key == _AGENT_KEY:
                agent = _assign_agent(agent, value, selected)
            else:
                passthrough.append(token)
        elif (
            not session_id
            and not token.startswith("-")
            and _SESSION_ID.match(token) is not None
        ):
            session_id = _assign_session(session_id, token)
        else:
            passthrough.append(token)
        index += 1

    return ResumeRequest(
        agent=agent,
        session_id=session_id,
        passthrough=tuple(passthrough),
    )


def _claude_root(home: Path) -> Path:
    return home / ".claude" / "projects"


def _codex_root(home: Path) -> Path:
    return home / ".codex" / "sessions"


def _agy_root(home: Path) -> Path:
    return home / ".gemini" / "antigravity-cli" / "conversations"


def _kimi_home(home: Path) -> Path:
    return home / ".kimi-code"


def _kimi_root(home: Path) -> Path:
    return _kimi_home(home) / "sessions"


def _pi_root(home: Path) -> Path:
    return home / ".pi" / "agent" / "sessions"


def _grok_home(home: Path) -> Path:
    return home / ".grok"


def _grok_root(home: Path) -> Path:
    return _grok_home(home) / "sessions"


def _read_json_line(line: str) -> dict | None:
    try:
        payload = json.loads(line)
    except ValueError:
        return None
    if isinstance(payload, dict):
        return payload
    return None


def _claude_cwd(path: Path) -> Path | None:
    # Claude Code writes one JSON record per line; the session's
    # directory appears on the first user or assistant record.
    try:
        with path.open("r", encoding="utf-8", errors="replace") as stream:
            for number, line in enumerate(stream):
                if number >= _CWD_SCAN_LINES:
                    break
                record = _read_json_line(line)
                if record is None:
                    continue
                directory = record.get("cwd")
                if isinstance(directory, str) and directory:
                    return Path(directory)
    except OSError:
        return None
    return None


def _codex_cwd(path: Path) -> Path | None:
    # Codex writes a session_meta record first; it carries the cwd.
    try:
        with path.open("r", encoding="utf-8", errors="replace") as stream:
            for number, line in enumerate(stream):
                if number >= _CWD_SCAN_LINES:
                    break
                record = _read_json_line(line)
                if record is None:
                    continue
                payload = record.get("payload")
                if not isinstance(payload, dict):
                    continue
                directory = payload.get("cwd")
                if isinstance(directory, str) and directory:
                    return Path(directory)
    except OSError:
        return None
    return None


def _claude_sessions(home: Path) -> Iterator[Session]:
    root = _claude_root(home)
    # Only direct children: subagent transcripts live one level
    # deeper, under <session>/subagents/, and are not resumable.
    for path in root.glob("*/*.jsonl"):
        yield Session(
            agent=CLAUDE,
            session_id=path.stem,
            path=path,
            cwd=None,
        )


def _codex_sessions(home: Path) -> Iterator[Session]:
    root = _codex_root(home)
    for path in root.rglob("rollout-*.jsonl"):
        identifier = _codex_session_id(path.stem)
        if not identifier:
            continue
        yield Session(
            agent=CODEX,
            session_id=identifier,
            path=path,
            cwd=None,
        )


def _agy_sessions(home: Path) -> Iterator[Session]:
    root = _agy_root(home)
    for path in root.glob("*.db"):
        yield Session(
            agent=AGY,
            session_id=path.stem,
            path=path,
            cwd=None,
        )


def _pi_session_id(stem: str) -> str:
    # <iso-timestamp-with-dashes>_<session id>; the timestamp carries
    # no underscore, so the id is everything after the first one.
    _, separator, identifier = stem.partition("_")
    if not separator:
        return ""
    return identifier


def _pi_sessions(home: Path) -> Iterator[Session]:
    root = _pi_root(home)
    # Pi buckets sessions by working directory:
    # sessions/--<slugged path>--/<timestamp>_<uuid>.jsonl.
    for path in root.glob("*/*.jsonl"):
        identifier = _pi_session_id(path.stem)
        if not identifier:
            continue
        yield Session(
            agent=PI,
            session_id=identifier,
            path=path,
            cwd=None,
        )


def _pi_cwd(path: Path) -> Path | None:
    # Pi writes a `type: "session"` header record first; it carries
    # the working directory as `cwd`.
    try:
        with path.open("r", encoding="utf-8", errors="replace") as stream:
            for number, line in enumerate(stream):
                if number >= _CWD_SCAN_LINES:
                    break
                record = _read_json_line(line)
                if record is None:
                    continue
                if record.get("type") != "session":
                    continue
                directory = record.get("cwd")
                if isinstance(directory, str) and directory:
                    return Path(directory)
    except OSError:
        return None
    return None


def _grok_sessions(home: Path) -> Iterator[Session]:
    root = _grok_root(home)
    # Grok buckets sessions by working directory:
    # sessions/<url-encoded-cwd>/<session-id>/summary.json.
    for path in root.glob("*/*/summary.json"):
        yield Session(
            agent=GROK,
            session_id=path.parent.name,
            path=path,
            cwd=None,
        )


def _grok_cwd(path: Path) -> Path | None:
    # summary.json carries info.cwd (and info.id).
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    info = payload.get("info")
    if not isinstance(info, dict):
        return None
    directory = info.get("cwd")
    if isinstance(directory, str) and directory:
        return Path(directory)
    return None


def _kimi_sessions(home: Path) -> Iterator[Session]:
    root = _kimi_root(home)
    # Kimi buckets sessions by working directory:
    # sessions/wd_<slug>_<hash>/session_<uuid>/. A session is a
    # directory, not a single transcript file.
    for path in root.glob("*/*"):
        if not path.is_dir():
            continue
        yield Session(
            agent=KIMI,
            session_id=path.name,
            path=path,
            cwd=None,
        )


def _kimi_cwd(path: Path) -> Path | None:
    # Kimi records every session's working directory in one index, so
    # this is a lookup rather than the head-of-transcript scan the
    # other agents need.
    # path is <home>/.kimi-code/sessions/<workDirKey>/<sessionId>.
    index = path.parent.parent.parent / "session_index.jsonl"
    try:
        with index.open("r", encoding="utf-8", errors="replace") as stream:
            for line in stream:
                record = _read_json_line(line)
                if record is None:
                    continue
                if record.get("sessionId") != path.name:
                    continue
                directory = record.get("workDir")
                if isinstance(directory, str) and directory:
                    return Path(directory)
    except OSError:
        return None
    return None


def _codex_session_id(stem: str) -> str:
    # rollout-<timestamp>-<session id>
    parts = stem.split("-")
    if len(parts) < 6:
        return ""
    return "-".join(parts[-5:])


def _with_cwd(session: Session) -> Session:
    if session.agent == CLAUDE:
        directory = _claude_cwd(session.path)
    elif session.agent == CODEX:
        directory = _codex_cwd(session.path)
    elif session.agent == KIMI:
        directory = _kimi_cwd(session.path)
    elif session.agent == PI:
        directory = _pi_cwd(session.path)
    elif session.agent == GROK:
        directory = _grok_cwd(session.path)
    else:
        # Antigravity stores conversations as SQLite databases and
        # records no working directory.
        directory = None
    return Session(
        agent=session.agent,
        session_id=session.session_id,
        path=session.path,
        cwd=directory,
    )


def _sessions_for(agent: str, home: Path) -> Iterator[Session]:
    if agent == CLAUDE:
        return _claude_sessions(home)
    if agent == CODEX:
        return _codex_sessions(home)
    if agent == AGY:
        return _agy_sessions(home)
    if agent == KIMI:
        return _kimi_sessions(home)
    if agent == PI:
        return _pi_sessions(home)
    if agent == GROK:
        return _grok_sessions(home)
    return iter(())


def _lookup(agent: str, session_id: str, home: Path) -> list[Session]:
    # The id is validated before it reaches a glob, so it carries no
    # pattern characters and matches literally.
    if agent == CLAUDE:
        found = _claude_root(home).glob("*/" + session_id + ".jsonl")
    elif agent == CODEX:
        found = _codex_root(home).rglob("rollout-*-" + session_id + ".jsonl")
    elif agent == AGY:
        found = _agy_root(home).glob(session_id + ".db")
    elif agent == KIMI:
        # The working-directory bucket is not known from the id alone.
        found = _kimi_root(home).glob("*/" + session_id)
    elif agent == PI:
        # Neither the working-directory bucket nor the timestamp
        # prefix is known from the id alone.
        found = _pi_root(home).glob("*/*_" + session_id + ".jsonl")
    elif agent == GROK:
        # The working-directory bucket is not known from the id alone.
        found = _grok_root(home).glob("*/" + session_id + "/summary.json")
    else:
        found = iter(())
    sessions: list[Session] = []
    for path in found:
        sessions.append(
            Session(agent=agent, session_id=session_id, path=path, cwd=None)
        )
    return sessions


def _keys(selected: Grammar) -> str:
    return ", ".join(agent.upper() + "=<id>" for agent in agent_names(selected))


def find_session(
    session_id: str, home: Path, selected: Grammar = DEFAULT_GRAMMAR
) -> Session:
    """Return the one session with this id, across the built-in agents."""

    matches: list[Session] = []
    for agent in BUILTIN_AGENTS:
        matches.extend(_lookup(agent, session_id, home))
    if not matches:
        message = (
            "no session found for id "
            + session_id
            + "; name the agent with one of "
            + _keys(selected)
        )
        if selected.custom:
            # Saying only "none found" would hide the one case a
            # search can never settle.
            message += (
                ". Sessions of "
                + ", ".join(selected.custom)
                + " are never searched for, so naming the agent is the "
                "only way to reach one"
            )
        raise ResumeError(message)
    agents = sorted({session.agent for session in matches})
    if len(agents) > 1:
        raise ResumeError(
            "session id " + session_id + " matches " + ", ".join(agents)
        )
    return _with_cwd(matches[0])


def latest_session(agent: str, home: Path, root: Path) -> Session:
    """Return that agent's newest session, preferring this repository."""

    candidates = sorted(
        _sessions_for(agent, home),
        key=lambda session: session.path.stat().st_mtime,
        reverse=True,
    )
    if not candidates:
        raise ResumeError("no " + agent + " sessions found")
    for session in candidates[:_LATEST_SCAN_LIMIT]:
        resolved = _with_cwd(session)
        if resolved.cwd is not None and resolved.cwd == root:
            return resolved
    return _with_cwd(candidates[0])


def _arguments_for(agent: str, session_id: str, directory: Path | None) -> tuple:
    if agent == CLAUDE:
        return ("--resume", session_id)
    if agent == CODEX:
        # Codex resumes relative to a repository, named with -C.
        if directory is None:
            return ("resume", session_id)
        return ("-C", str(directory), "resume", session_id)
    if agent == AGY:
        return ("--conversation", session_id)
    if agent == KIMI:
        return ("--session", session_id)
    if agent == PI:
        return ("--session", session_id)
    if agent == GROK:
        return ("--resume", session_id)
    return ()


def resume_plan(
    argv: Iterable[str],
    home: Path,
    root: Path,
    selected: Grammar = DEFAULT_GRAMMAR,
) -> ResumePlan:
    """Turn a resume request into a plan.

    Raises ResumeError with a user-facing message when the request is
    ambiguous, malformed, or names a session that is not on disk.
    """

    request = parse_resume_argv(argv, selected)
    if not request.session_id:
        raise ResumeError(
            "missing session id; pass <id>, UUID=<id>, or one of " + _keys(selected)
        )

    is_latest = request.session_id.lower() == LATEST
    if request.agent is not None and request.agent in selected.custom:
        # Nothing on disk describes a custom agent's sessions, so the
        # request is carried through as written, `last` included.
        session_id = request.session_id
        if is_latest:
            session_id = LATEST
        return ResumePlan(
            agent=request.agent,
            session_id=session_id,
            arguments=(),
            cwd=root,
        )

    if is_latest:
        if request.agent is None:
            raise ResumeError(
                "'" + LATEST + "' needs an agent; pass CLAUDE=" + LATEST
            )
        session = latest_session(request.agent, home, root)
    elif request.agent is None:
        session = find_session(request.session_id, home, selected)
    else:
        # An explicitly named agent is trusted even when the session
        # is not on this machine's disk; only the directory is lost.
        session = Session(
            agent=request.agent,
            session_id=request.session_id,
            path=Path(),
            cwd=None,
        )
        found = _lookup(request.agent, request.session_id, home)
        if found:
            session = _with_cwd(found[0])

    directory = session.cwd
    if directory is None or not directory.is_dir():
        directory = root
    return ResumePlan(
        agent=session.agent,
        session_id=session.session_id,
        arguments=_arguments_for(session.agent, session.session_id, directory),
        cwd=directory,
    )
