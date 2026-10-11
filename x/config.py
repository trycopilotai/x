"""Loading `x.toml`: one root file plus optional folder files.

The root file is the nearest `x.toml` at or above the starting
directory that has an `[x]` table. Files found on the way up to it
are folder files; they may only add or replace commands.

The search goes upward only inside a Git repository, and stops at
the repository's top folder. A starting directory that is in no Git
repository is searched alone: an `x.toml` in some folder above it
never applies.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import tomllib
from typing import Any, Mapping

from x import sessions
from x.model import PLATFORMS, Normalizer, SpecError, identity


CONFIG_NAME = "x.toml"

HANDLER_KINDS = ("run", "bash", "npx", "bazel", "builtin")
NAMINGS = ("exact", "kebab", "snake")
CWD_KEYWORDS = ("config", "root", "caller")

_VERSION = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z][0-9A-Za-z.-]*)?$")
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_CAMEL = re.compile(r"([a-z0-9])([A-Z])")
_SEPARATORS = re.compile(r"[._-]+")


class ConfigError(SpecError):
    """An `x.toml` file cannot be used as written."""


@dataclass(frozen=True)
class HandlerConfig:
    kind: str
    # run, npx, bazel: the argv prefix. bash: arguments after the
    # script. builtin: unused.
    argv: tuple[str, ...] = ()
    # bash: the script path. builtin: the built-in's name.
    target: str = ""


@dataclass(frozen=True)
class MakeRuleConfig:
    enabled: bool = True
    args: bool | None = None
    aliases: bool | None = None
    recipe: tuple[str, ...] = ()
    prelude: tuple[str, ...] = ()
    after: tuple[str, ...] = ()


@dataclass(frozen=True)
class CommandConfig:
    name: str
    description: str
    source: Path
    aliases: tuple[str, ...] = ()
    platforms: tuple[str, ...] = ()
    hidden: bool = False
    cwd: str = "config"
    env: tuple[tuple[str, str], ...] = ()
    unset_env: tuple[str, ...] = ()
    handler: HandlerConfig | None = None
    platform_handlers: tuple[tuple[str, HandlerConfig], ...] = ()
    make: MakeRuleConfig = field(default_factory=MakeRuleConfig)

    @property
    def directory(self) -> Path:
        return self.source.parent


@dataclass(frozen=True)
class FlagConfig:
    key: str
    names: tuple[str, ...]
    takes_value: bool = False
    forward: tuple[str, ...] = ()


@dataclass(frozen=True)
class DelegateConfig:
    handler: HandlerConfig | None = None
    platform_handlers: tuple[tuple[str, HandlerConfig], ...] = ()


@dataclass(frozen=True)
class MakefileConfig:
    path: str = "Makefile"
    header: tuple[str, ...] | None = None
    launcher: str | None = None
    default: str | None = None
    order: str = "declared"
    args: bool = True
    aliases: bool = False


@dataclass(frozen=True)
class ResumeAgentConfig:
    agent: str
    handler: HandlerConfig | None = None
    command: str | None = None
    # With `command`: arguments placed before the resume arguments.
    args: tuple[str, ...] = ()
    # Extra names the request grammar accepts for this agent.
    aliases: tuple[str, ...] = ()
    # A custom agent is one x reads no session store for. Its resume
    # arguments come from here instead: `resume` for a session id,
    # `last` for the newest session, or None when it has no such idea.
    custom: bool = False
    resume: tuple[str, ...] = ()
    last: tuple[str, ...] | None = None


@dataclass(frozen=True)
class RootConfig:
    path: Path
    version: str
    naming: str = "exact"
    default: str = "help"
    caller_env: tuple[str, ...] = ("X_CALLER_DIRECTORY",)
    flags: tuple[FlagConfig, ...] = ()
    defaults: DelegateConfig = field(default_factory=DelegateConfig)
    unknown: DelegateConfig = field(default_factory=DelegateConfig)
    commands: tuple[CommandConfig, ...] = ()
    make: MakefileConfig | None = None
    resume: tuple[ResumeAgentConfig, ...] = ()

    @property
    def directory(self) -> Path:
        return self.path.parent


@dataclass(frozen=True)
class FolderConfig:
    path: Path
    commands: tuple[CommandConfig, ...] = ()

    @property
    def directory(self) -> Path:
        return self.path.parent


@dataclass(frozen=True)
class Workspace:
    root: RootConfig
    # Folder files between the root and the starting directory,
    # farthest first, so a later entry is nearer and wins.
    folders: tuple[FolderConfig, ...] = ()

    @property
    def directory(self) -> Path:
        return self.root.directory

    @property
    def paths(self) -> tuple[Path, ...]:
        return (self.root.path,) + tuple(folder.path for folder in self.folders)


def normalizer(naming: str) -> Normalizer:
    """Return the name fold for a naming style.

    `exact` matches names as written. `snake` and `kebab` accept any
    of `fooBar`, `foo.bar`, `foo-bar` and `foo_bar` for one command.
    """
    if naming == "exact":
        return identity
    if naming not in ("snake", "kebab"):
        raise ConfigError("unknown naming style: " + naming)
    separator = "-"
    if naming == "snake":
        separator = "_"

    def fold(name: str) -> str:
        folded = _CAMEL.sub(r"\1" + separator + r"\2", name)
        folded = folded.lower()
        folded = _SEPARATORS.sub(separator, folded)
        return folded.strip(separator)

    return fold


def _is_rooted(value: str) -> bool:
    """True for a path that names a fixed place on any host.

    Judged the same way everywhere, so a file that loads on one
    platform loads on the others: a leading slash or backslash, or a
    Windows drive.
    """
    if value.startswith(("/", "\\")):
        return True
    return PureWindowsPath(value).is_absolute() or bool(PureWindowsPath(value).drive)


class _Reader:
    """Typed access to one TOML table that rejects unknown keys."""

    def __init__(self, path: Path, where: str, table: Any) -> None:
        self.path = path
        self.where = where
        if not isinstance(table, dict):
            raise self.error("must be a table")
        self.table: dict[str, Any] = table
        self.seen: set[str] = set()

    def error(self, message: str, key: str | None = None) -> ConfigError:
        location = self.where
        if key is not None:
            location += " " + key
        return ConfigError(str(self.path) + ": " + location + ": " + message)

    def has(self, key: str) -> bool:
        return key in self.table

    def take(self, key: str) -> Any:
        self.seen.add(key)
        return self.table.get(key)

    def string(self, key: str, default: str | None = None) -> str | None:
        value = self.take(key)
        if value is None:
            return default
        if not isinstance(value, str):
            raise self.error("must be a string", key)
        return value

    def boolean(self, key: str, default: bool | None = None) -> bool | None:
        value = self.take(key)
        if value is None:
            return default
        if not isinstance(value, bool):
            raise self.error("must be true or false", key)
        return value

    def strings(self, key: str) -> tuple[str, ...] | None:
        value = self.take(key)
        if value is None:
            return None
        if not isinstance(value, list) or not all(
            isinstance(item, str) for item in value
        ):
            raise self.error("must be an array of strings", key)
        return tuple(value)

    def nested(self, key: str) -> str:
        """Name the table `key` inside this one, as TOML writes it."""
        if self.where.startswith("[") and self.where.endswith("]"):
            return self.where[:-1] + "." + key + "]"
        return "[" + key + "]"

    def child(self, key: str) -> "_Reader | None":
        value = self.take(key)
        if value is None:
            return None
        return _Reader(self.path, self.nested(key), value)

    def finish(self) -> None:
        unknown = sorted(set(self.table) - self.seen)
        if unknown:
            raise self.error("unknown key '" + unknown[0] + "'")


def _handler(reader: _Reader, *, builtin: bool) -> HandlerConfig | None:
    present = [kind for kind in HANDLER_KINDS if reader.has(kind)]
    if not builtin and "builtin" in present:
        raise reader.error("is not allowed here", "builtin")
    if len(present) > 1:
        raise reader.error("declares more than one of " + ", ".join(present))
    if not present:
        if reader.has("args"):
            raise reader.error("is only valid beside bash", "args")
        return None

    kind = present[0]
    if kind in ("bash", "builtin"):
        target = reader.string(kind)
        if not target:
            raise reader.error("must not be empty", kind)
        argv: tuple[str, ...] = ()
        if kind == "bash":
            argv = reader.strings("args") or ()
        elif reader.has("args"):
            raise reader.error("is only valid beside bash", "args")
        return HandlerConfig(kind=kind, argv=argv, target=target)

    if reader.has("args"):
        raise reader.error("is only valid beside bash", "args")
    prefix = reader.strings(kind)
    if not prefix:
        raise reader.error("must be a non-empty array of strings", kind)
    return HandlerConfig(kind=kind, argv=prefix)


def _platform_handlers(
    reader: _Reader, *, builtin: bool
) -> tuple[tuple[str, HandlerConfig], ...]:
    selected: list[tuple[str, HandlerConfig]] = []
    for platform in PLATFORMS:
        child = reader.child(platform)
        if child is None:
            continue
        handler = _handler(child, builtin=builtin)
        if handler is None:
            raise child.error("needs one of " + ", ".join(HANDLER_KINDS[:4]))
        child.finish()
        selected.append((platform, handler))
    return tuple(selected)


def _delegate(reader: _Reader | None) -> DelegateConfig:
    if reader is None:
        return DelegateConfig()
    handler = _handler(reader, builtin=False)
    platform_handlers = _platform_handlers(reader, builtin=False)
    reader.finish()
    if handler is None:
        raise reader.error("needs one of " + ", ".join(HANDLER_KINDS[:4]))
    return DelegateConfig(handler=handler, platform_handlers=platform_handlers)


def _make_rule(reader: _Reader) -> MakeRuleConfig:
    value = reader.take("make")
    if value is None or value is True:
        return MakeRuleConfig()
    if value is False:
        return MakeRuleConfig(enabled=False)
    rule = _Reader(reader.path, reader.nested("make"), value)
    selected = MakeRuleConfig(
        args=rule.boolean("args"),
        aliases=rule.boolean("aliases"),
        recipe=rule.strings("recipe") or (),
        prelude=rule.strings("prelude") or (),
        after=rule.strings("after") or (),
    )
    rule.finish()
    return selected


def _env_names(reader: _Reader, key: str) -> tuple[str, ...] | None:
    names = reader.strings(key)
    if names is None:
        return None
    for name in names:
        if not _ENV_NAME.match(name):
            raise reader.error("is not an environment variable name: " + name, key)
    return names


def _command(path: Path, name: str, table: Any) -> CommandConfig:
    reader = _Reader(path, "[commands." + name + "]", table)
    if not name:
        raise reader.error("command name is empty")
    description = reader.string("description")
    if not description:
        raise reader.error("is required", "description")
    if "\n" in description:
        raise reader.error("must be one line", "description")

    platforms = reader.strings("platforms") or ()
    for platform in platforms:
        if platform not in PLATFORMS:
            raise reader.error("unknown platform '" + platform + "'", "platforms")

    cwd = reader.string("cwd", "config") or "config"
    if cwd not in CWD_KEYWORDS and _is_rooted(cwd):
        raise reader.error("must be config, root, caller, or a relative path", "cwd")

    environment: list[tuple[str, str]] = []
    env_reader = reader.child("env")
    if env_reader is not None:
        for key in env_reader.table:
            if not _ENV_NAME.match(key):
                raise env_reader.error("is not an environment variable name", key)
            value = env_reader.string(key)
            environment.append((key, value or ""))

    command = CommandConfig(
        name=name,
        description=description,
        source=path,
        aliases=reader.strings("aliases") or (),
        platforms=platforms,
        hidden=bool(reader.boolean("hidden", False)),
        cwd=cwd,
        env=tuple(environment),
        unset_env=_env_names(reader, "unset_env") or (),
        handler=_handler(reader, builtin=True),
        platform_handlers=_platform_handlers(reader, builtin=False),
        make=_make_rule(reader),
    )
    reader.finish()
    return command


def _commands(path: Path, table: Any) -> tuple[CommandConfig, ...]:
    if table is None:
        return ()
    if not isinstance(table, dict):
        raise ConfigError(str(path) + ": [commands]: must be a table")
    return tuple(_command(path, name, value) for name, value in table.items())


def _flags(path: Path, table: Any) -> tuple[FlagConfig, ...]:
    if table is None:
        return ()
    if not isinstance(table, dict):
        raise ConfigError(str(path) + ": [flags]: must be a table")
    selected: list[FlagConfig] = []
    for key, value in table.items():
        reader = _Reader(path, "[flags." + key + "]", value)
        names = reader.strings("names")
        if not names:
            raise reader.error("must be a non-empty array of strings", "names")
        for name in names:
            if not name.startswith("-"):
                raise reader.error("flag names start with '-': " + name, "names")
        selected.append(
            FlagConfig(
                key=key,
                names=names,
                takes_value=bool(reader.boolean("takes_value", False)),
                forward=reader.strings("forward") or (),
            )
        )
        reader.finish()
    return tuple(selected)


def _makefile(reader: _Reader | None) -> MakefileConfig | None:
    if reader is None:
        return None
    order = reader.string("order", "declared") or "declared"
    if order not in ("declared", "alpha"):
        raise reader.error("must be declared or alpha", "order")
    path = reader.string("path", "Makefile") or "Makefile"
    if _is_rooted(path) or ".." in PurePosixPath(path.replace("\\", "/")).parts:
        raise reader.error("must be a path inside the root x.toml's folder", "path")
    selected = MakefileConfig(
        path=path,
        header=reader.strings("header"),
        launcher=reader.string("launcher"),
        default=reader.string("default"),
        order=order,
        args=bool(reader.boolean("args", True)),
        aliases=bool(reader.boolean("aliases", False)),
    )
    reader.finish()
    if selected.header is not None and (
        selected.launcher is not None or selected.default is not None
    ):
        raise reader.error(
            "header replaces the generated header, so launcher and "
            "default have no effect beside it; put them in the header"
        )
    return selected


def resume_grammar(agents: tuple[ResumeAgentConfig, ...]) -> sessions.Grammar:
    """The request grammar these agent tables add up to."""
    aliases: dict[str, str] = {}
    for agent in agents:
        for alias in agent.aliases:
            folded = alias.strip().lower()
            owner = aliases.get(folded)
            if owner is not None and owner != agent.agent:
                raise sessions.ResumeError(
                    "agent name '"
                    + folded
                    + "' belongs to "
                    + owner
                    + " and "
                    + agent.agent
                )
            aliases[folded] = agent.agent
    return sessions.grammar(
        custom=[agent.agent for agent in agents if agent.custom],
        aliases=aliases,
    )


def _resume(path: Path, table: Any) -> tuple[ResumeAgentConfig, ...]:
    if table is None:
        return ()
    reader = _Reader(path, "[resume]", table)
    agents = reader.take("agents")
    reader.finish()
    if agents is None:
        return ()
    if not isinstance(agents, dict):
        raise reader.error("must be a table", "agents")
    selected: list[ResumeAgentConfig] = []
    seen: set[str] = set()
    for key, value in agents.items():
        child = _Reader(path, "[resume.agents." + key + "]", value)
        folded = key.strip().lower()
        custom = not sessions.is_builtin(folded)
        agent = folded
        if not custom:
            agent = sessions.builtin_agent(folded)
        if agent in seen:
            raise child.error("agent is configured twice: " + agent)
        seen.add(agent)

        command = child.string("command")
        arguments: tuple[str, ...] = ()
        handler = None
        if command is not None:
            present = [kind for kind in HANDLER_KINDS if child.has(kind)]
            if present:
                raise child.error("needs exactly one of run, npx, bazel, bash, command")
            arguments = child.strings("args") or ()
        else:
            handler = _handler(child, builtin=False)
        aliases = child.strings("aliases") or ()
        resume_arguments = child.strings("resume")
        last_arguments = child.strings("last")
        child.finish()

        if command is None and handler is None:
            raise child.error("needs exactly one of run, npx, bazel, bash, command")
        if custom and resume_arguments is None:
            raise child.error(
                "x reads no session store for '"
                + agent
                + "', so it needs `resume`: the arguments that resume a "
                'session, such as ["--resume", "{session}"]'
            )
        if not custom and (resume_arguments is not None or last_arguments is not None):
            raise child.error(
                "resume and last are for agents x reads no session store "
                "for; x supplies them for " + agent
            )
        selected.append(
            ResumeAgentConfig(
                agent=agent,
                handler=handler,
                command=command,
                args=arguments,
                aliases=aliases,
                custom=custom,
                resume=resume_arguments or (),
                last=last_arguments,
            )
        )

    configured = tuple(selected)
    try:
        resume_grammar(configured)
    except sessions.ResumeError as error:
        raise ConfigError(str(path) + ": [resume.agents]: " + str(error)) from None
    return configured


def _parse(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as stream:
            return tomllib.load(stream)
    except tomllib.TOMLDecodeError as error:
        raise ConfigError(str(path) + ": " + str(error)) from None
    except UnicodeDecodeError:
        raise ConfigError(str(path) + ": is not UTF-8 text") from None
    except OSError as error:
        raise ConfigError(str(path) + ": " + (error.strerror or str(error))) from None


def is_root(data: Mapping[str, Any]) -> bool:
    return "x" in data


def load_root(path: Path, data: dict[str, Any]) -> RootConfig:
    top = _Reader(path, "top level", data)
    header = top.child("x")
    if header is None:
        raise top.error("a root x.toml needs an [x] table")
    version = header.string("version")
    if not version:
        raise header.error("is required", "version")
    if not _VERSION.match(version):
        raise header.error("must be an exact version such as 0.1.0", "version")
    naming = header.string("naming", "exact") or "exact"
    if naming not in NAMINGS:
        raise header.error("must be one of " + ", ".join(NAMINGS), "naming")
    caller_env = _env_names(header, "caller_env")
    if caller_env is None:
        caller_env = ("X_CALLER_DIRECTORY",)
    default = header.string("default", "help") or "help"
    header.finish()

    root = RootConfig(
        path=path,
        version=version,
        naming=naming,
        default=default,
        caller_env=caller_env,
        flags=_flags(path, top.take("flags")),
        defaults=_delegate(top.child("defaults")),
        unknown=_delegate(top.child("unknown")),
        commands=_commands(path, top.take("commands")),
        make=_makefile(top.child("make")),
        resume=_resume(path, top.take("resume")),
    )
    top.finish()
    return root


def load_folder(path: Path, data: dict[str, Any]) -> FolderConfig:
    top = _Reader(path, "top level", data)
    folder = FolderConfig(path=path, commands=_commands(path, top.take("commands")))
    unknown = sorted(set(data) - {"commands"})
    if unknown:
        raise top.error(
            "a folder x.toml may only hold [commands.*]; '"
            + unknown[0]
            + "' belongs in the root file"
        )
    return folder


def repository_top(start: Path) -> Path | None:
    """The top folder of the Git repository holding `start`, if any."""
    current = start
    while True:
        if (current / ".git").exists():
            return current
        if current.parent == current:
            return None
        current = current.parent


def find_files(start: Path) -> tuple[tuple[Path, dict[str, Any]], ...]:
    """Return the `x.toml` files that apply to `start`, root first.

    Empty when no `x.toml` applies. Inside a Git repository the
    search runs from `start` up to the repository's top folder.
    Outside one it looks in `start` only.
    """
    current = start.resolve()
    top = repository_top(current)
    in_repository = top is not None
    if top is None:
        top = current
    found: list[tuple[Path, dict[str, Any]]] = []
    rooted = False
    while True:
        candidate = current / CONFIG_NAME
        if candidate.is_file():
            data = _parse(candidate)
            found.append((candidate, data))
            if is_root(data):
                rooted = True
                break
        if current == top:
            break
        current = current.parent

    if found and not rooted:
        message = (
            str(found[0][0]) + ": has no [x] table, and no root x.toml applies above it"
        )
        if in_repository:
            message += " in this repository; one file must be the root"
        else:
            message += (
                ": this folder is in no Git repository, so files in the "
                "folders above it are not read"
            )
        raise ConfigError(message)
    found.reverse()
    return tuple(found)


def load_workspace(start: Path) -> Workspace | None:
    files = find_files(start)
    if not files:
        return None
    root_path, root_data = files[0]
    root = load_root(root_path, root_data)
    folders = tuple(load_folder(path, data) for path, data in files[1:])
    return Workspace(root=root, folders=folders)
