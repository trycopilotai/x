"""Immutable command and invocation models."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, IO, Mapping, Protocol, Sequence


class SpecError(ValueError):
    """A command registry is internally inconsistent."""


@dataclass(frozen=True)
class ProcessRequest:
    argv: tuple[str, ...]
    cwd: Path
    environ: Mapping[str, str]
    stdin: IO[Any] | None = None
    stdout: IO[Any] | None = None
    stderr: IO[Any] | None = None


class ProcessRunner(Protocol):
    def __call__(self, request: ProcessRequest) -> int:
        """Run one child process and return its exit status."""


@dataclass(frozen=True)
class RepositoryContext:
    root: Path
    cwd: Path
    environ: Mapping[str, str]
    platform: str
    stdin: IO[Any]
    stdout: IO[Any]
    stderr: IO[Any]
    process_runner: ProcessRunner | None = None


@dataclass(frozen=True)
class Invocation:
    argv: tuple[str, ...]
    context: RepositoryContext
    flags: Mapping[str, str]
    control_plane: Any = field(repr=False)
    # The canonical name of the command being run. For a request that
    # matched no command it is the token as it was typed.
    name: str = ""


CommandHandler = Callable[[Invocation], int]
Normalizer = Callable[[str], str]


@dataclass(frozen=True)
class CommandSpec:
    name: str
    description: str
    handler: CommandHandler
    platform_handlers: tuple[tuple[str, CommandHandler], ...] = ()
    aliases: tuple[str, ...] = ()
    platforms: tuple[str, ...] = ()
    hidden: bool = False


@dataclass(frozen=True)
class FlagSpec:
    names: tuple[str, ...]
    key: str
    takes_value: bool = False
    value: str = "1"


PLATFORMS = ("linux", "macos", "windows")


def identity(value: str) -> str:
    return value


def command_names(commands: Sequence[CommandSpec]) -> tuple[str, ...]:
    return tuple(command.name for command in commands)
