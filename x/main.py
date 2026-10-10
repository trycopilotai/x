"""Command-line entry point."""

from __future__ import annotations

from dataclasses import replace
import os
from pathlib import Path
import sys
from types import MappingProxyType
from typing import IO, Any, Mapping, Sequence

from x import __version__
from x.config import ConfigError, Workspace, load_workspace
from x.context import repository_context
from x.execution import NPM_LEAKED_ENV, run_process
from x.makefile import PACKAGE
from x.model import Invocation, ProcessRequest, ProcessRunner, SpecError
from x.registry import build_control_plane


BUILTIN_FLAG = "--builtin"
ROOT_FLAG = "--root"
# Marks a process that was started to honour a version pin, so a pin
# that still does not match is reported instead of retried forever.
PINNED_FLAG = "--pinned"


def _fail(stderr: IO[Any], message: str, status: int = 2) -> int:
    print("x: " + message, file=stderr)
    return status


def _run_pinned(
    workspace: Workspace,
    arguments: Sequence[str],
    start: Path,
    environ: Mapping[str, str],
    process_runner: ProcessRunner | None,
    stderr: IO[Any],
) -> int:
    """Run the engine version that `x.toml` pins, with the same argv."""
    environment = dict(environ)
    for key in NPM_LEAKED_ENV:
        environment.pop(key, None)
    request = ProcessRequest(
        argv=("npx", "--yes", PACKAGE + "@" + workspace.root.version, PINNED_FLAG)
        + tuple(arguments),
        cwd=start,
        environ=environment,
    )
    runner = process_runner
    if runner is None:
        runner = run_process
    try:
        return runner(request)
    except OSError as error:
        reason = "npx was not found"
        if not isinstance(error, FileNotFoundError):
            reason = "npx could not be started (" + (error.strerror or str(error)) + ")"
        return _fail(
            stderr,
            "x.toml pins version "
            + workspace.root.version
            + " but this is "
            + __version__
            + ", and "
            + reason
            + " to fetch the pinned one",
            127,
        )


def main(
    argv: Sequence[str] | None = None,
    *,
    cwd: Path | None = None,
    environ: Mapping[str, str] | None = None,
    platform: str | None = None,
    process_runner: ProcessRunner | None = None,
    stdout: IO[Any] | None = None,
    stderr: IO[Any] | None = None,
) -> int:
    arguments = sys.argv[1:]
    if argv is not None:
        arguments = list(argv)
    environment = dict(os.environ)
    if environ is not None:
        environment = dict(environ)
    error_stream = sys.stderr
    if stderr is not None:
        error_stream = stderr
    try:
        start = Path.cwd()
    except OSError:
        # The folder x was started in has been deleted since.
        if cwd is None:
            return _fail(error_stream, "the working directory no longer exists", 127)
        start = cwd
    if cwd is not None:
        start = cwd
    start = start.resolve()

    pinned = False
    if arguments[:1] == [PINNED_FLAG]:
        pinned = True
        arguments = arguments[1:]
    original = list(arguments)

    # `--root <dir>` picks the x.toml to use without changing where
    # the caller is: a launcher that lives in one repository can be
    # run from anywhere and still dispatch that repository's commands.
    search = start
    if arguments[:1] == [ROOT_FLAG]:
        if len(arguments) < 2:
            return _fail(error_stream, "usage: " + ROOT_FLAG + " <folder> [command]")
        search = (start / arguments[1]).resolve()
        if not search.is_dir():
            return _fail(error_stream, ROOT_FLAG + ": not a folder: " + arguments[1])
        arguments = arguments[2:]

    builtin: str | None = None
    if arguments[:1] == ["--version"]:
        builtin = "version"
        arguments = arguments[1:]
    elif arguments[:1] in (["--help"], ["-h"]):
        arguments = ["help"] + arguments[1:]
    elif arguments[:1] == [BUILTIN_FLAG]:
        if len(arguments) < 2:
            return _fail(error_stream, "usage: " + BUILTIN_FLAG + " <name> [args]")
        builtin = arguments[1]
        arguments = arguments[2:]

    if builtin == "version":
        # Answered before any x.toml is read, so it works beside a
        # file that does not load and whatever version that file pins.
        output = sys.stdout
        if stdout is not None:
            output = stdout
        output.write(__version__ + "\n")
        return 0

    try:
        workspace = load_workspace(search)
        if workspace is not None and workspace.root.version != __version__:
            if pinned:
                return _fail(
                    error_stream,
                    str(workspace.root.path)
                    + " pins version "
                    + workspace.root.version
                    + " but version "
                    + __version__
                    + " is running",
                )
            return _run_pinned(
                workspace, original, start, environment, process_runner, error_stream
            )
        plane, builtin_specs = build_control_plane(workspace)
    except ConfigError as error:
        return _fail(error_stream, str(error))
    except SpecError as error:
        # Raised while the registry was put together, so it carries
        # no file of its own: every such fault is in the files read.
        message = str(error)
        if workspace is not None:
            message = str(workspace.root.path) + ": " + message
        return _fail(error_stream, message)

    root = search
    if workspace is not None:
        root = workspace.directory
        for name in workspace.root.caller_env:
            environment[name] = str(start)

    context = repository_context(
        root=root,
        cwd=start,
        environ=environment,
        platform=platform,
        process_runner=process_runner,
    )
    if stdout is not None:
        context = replace(context, stdout=stdout)
    if stderr is not None:
        context = replace(context, stderr=stderr)

    if builtin is not None:
        for spec in builtin_specs:
            if spec.name == builtin:
                return int(
                    spec.handler(
                        Invocation(
                            argv=tuple(arguments),
                            context=context,
                            flags=MappingProxyType({}),
                            control_plane=plane,
                            name=spec.name,
                        )
                    )
                )
        names = ", ".join(spec.name for spec in builtin_specs)
        return _fail(error_stream, "no built-in named '" + builtin + "'; use " + names)

    return plane.dispatch(arguments, context)


if __name__ == "__main__":
    raise SystemExit(main())
