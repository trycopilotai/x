"""The built-in commands: help, version, check, init, makefile, resume."""

from __future__ import annotations

from pathlib import Path
import subprocess
import sys
from typing import Any, Sequence

from x import __version__, sessions
from x.config import (
    CONFIG_NAME,
    CommandConfig,
    HandlerConfig,
    ResumeAgentConfig,
    Workspace,
    load_workspace,
    normalizer,
    resume_grammar,
)
from x.handlers import expand, launch
from x.help import HelpFormat, render_help, render_json
from x.makefile import (
    check_makefile,
    is_generated,
    makefile_path,
    render_makefile,
    write_makefile,
)
from x.model import CommandHandler, CommandSpec, Invocation, SpecError


ROOT_TEMPLATE = """\
# x.toml: the commands this repository offers through x.
# `help` lists them; `help --all` also lists x's own commands.

[x]
version = "{version}"

# {{python}} is the Python that runs x, so this starter needs nothing
# else installed. Replace it with your own commands.

[commands.hello]
description = "Say hello."
run = ["{{python}}", "-c", "import sys; print('hello from x', *sys.argv[1:])"]
"""

FOLDER_TEMPLATE = """\
# x.toml: commands that exist only in this folder and below it.
# They run with this folder as their working directory.

[commands.hello]
description = "Say hello from this folder."
run = ["{python}", "-c", "import sys; print('hello from this folder', *sys.argv[1:])"]
"""


def _relative(path: Path, base: Path) -> str:
    try:
        return path.relative_to(base).as_posix()
    except ValueError:
        return str(path)


def _usage(invocation: Invocation, text: str) -> int:
    print("x: usage: " + text, file=invocation.context.stderr)
    return 2


def _count(number: int, noun: str) -> str:
    if number == 1:
        return "1 " + noun
    return str(number) + " " + noun + "s"


# --- version ---------------------------------------------------------


def _version(invocation: Invocation) -> int:
    invocation.context.stdout.write(__version__ + "\n")
    return 0


# --- help ------------------------------------------------------------


def _command_document(
    spec: CommandSpec,
    configured: dict[str, CommandConfig],
    workspace: Workspace | None,
) -> dict[str, Any]:
    # Every entry has every key, so a reader never has to guess
    # which ones a built-in carries.
    document: dict[str, Any] = {
        "name": spec.name,
        "description": spec.description,
        "aliases": list(spec.aliases),
        "platforms": list(spec.platforms),
        "hidden": spec.hidden,
        "builtin": spec.name not in configured,
        "kind": "builtin",
        "cwd": None,
        "source": None,
        "make": False,
    }
    command = configured.get(spec.name)
    if command is not None and workspace is not None:
        kind = "default"
        if command.handler is not None:
            kind = command.handler.kind
        document["kind"] = kind
        document["cwd"] = command.cwd
        document["source"] = _relative(command.source, workspace.directory)
        document["make"] = (
            workspace.root.make is not None
            and command.make.enabled
            and command.source == workspace.root.path
        )
    return document


def _help(
    workspace: Workspace | None, merged: Sequence[CommandConfig]
) -> CommandHandler:
    configured = {command.name: command for command in merged}

    def handler(invocation: Invocation) -> int:
        as_json = False
        include_all = False
        names: list[str] = []
        for argument in invocation.argv:
            if argument == "--json":
                as_json = True
            elif argument == "--all":
                include_all = True
            elif argument.startswith("-") or names:
                return _usage(invocation, "help [--json] [--all] [command]")
            else:
                names.append(argument)

        plane = invocation.control_plane
        context = invocation.context
        if names:
            command = plane.resolve(names[0])
            if command is None:
                print("x: unknown command: " + names[0], file=context.stderr)
                return 2
            if as_json:
                context.stdout.write(
                    render_json(_command_document(command, configured, workspace))
                )
            else:
                context.stdout.write(command.name + "\t" + command.description + "\n")
            return 0

        if as_json:
            document: dict[str, Any] = {
                "schema": 1,
                "version": __version__,
                "root": None,
                "configs": [],
                "default": plane.default_command,
                "naming": "exact",
                "flags": [
                    {
                        "key": flag.key,
                        "names": list(flag.names),
                        "takes_value": flag.takes_value,
                    }
                    for flag in plane.flags
                ],
                "commands": [
                    _command_document(spec, configured, workspace)
                    for spec in plane.commands
                ],
            }
            if workspace is not None:
                document["root"] = str(workspace.directory)
                document["pinned"] = workspace.root.version
                document["naming"] = workspace.root.naming
                document["configs"] = [
                    _relative(path, workspace.directory) for path in workspace.paths
                ]
            context.stdout.write(render_json(document))
            return 0

        context.stdout.write(
            render_help(plane.commands, HelpFormat(include_hidden=include_all))
        )
        if workspace is None:
            print(
                "x: no x.toml here; these are the built-ins. Run `init` to add one.",
                file=context.stderr,
            )
        return 0

    return handler


# --- check -----------------------------------------------------------


def _tracked_configs(root: Path) -> tuple[Path, ...]:
    """Every `x.toml` Git tracks under `root`; empty outside Git."""
    try:
        completed = subprocess.run(
            ["git", "ls-files", "-z", "--", CONFIG_NAME, "*/" + CONFIG_NAME],
            cwd=root,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    except OSError:
        return ()
    if completed.returncode != 0:
        return ()
    paths: list[Path] = []
    for entry in completed.stdout.decode("utf-8", "replace").split("\0"):
        if entry and Path(entry).name == CONFIG_NAME:
            paths.append(root / entry)
    return tuple(sorted(paths))


def _script_candidates(handler: HandlerConfig) -> tuple[str, ...]:
    if handler.kind == "bash":
        return (handler.target,)
    if handler.kind == "run" and handler.argv:
        first = handler.argv[0]
        if "/" in first and not Path(first).is_absolute():
            return (first,)
    return ()


def _missing_scripts(command: CommandConfig, workspace: Workspace) -> list[str]:
    if command.cwd == "caller":
        return []
    base = command.directory
    if command.cwd == "root":
        base = workspace.directory
    elif command.cwd != "config":
        base = command.directory / command.cwd
    handlers = [handler for _platform, handler in command.platform_handlers]
    if command.handler is not None:
        handlers.append(command.handler)
    missing: list[str] = []
    for handler in handlers:
        for script in _script_candidates(handler):
            if "{" in script:
                continue
            if not (base / script).exists():
                missing.append(script)
    return missing


def _check(
    workspace: Workspace | None, merged: Sequence[CommandConfig]
) -> CommandHandler:
    def handler(invocation: Invocation) -> int:
        context = invocation.context
        if invocation.argv:
            return _usage(invocation, "check")
        if workspace is None:
            print(
                "x: no x.toml applies in "
                + str(context.root)
                + "; run `init` to create one",
                file=context.stderr,
            )
            return 1

        base = workspace.directory
        errors = 0

        def report(level: str, path: Path, message: str) -> None:
            context.stdout.write(
                level + "\t" + _relative(path, base) + "\t" + message + "\n"
            )

        report(
            "ok", workspace.root.path, _count(len(workspace.root.commands), "command")
        )
        for folder in workspace.folders:
            report("ok", folder.path, _count(len(folder.commands), "command"))

        # Imported here because the registry imports this module.
        from x.registry import build_control_plane

        visible = set(workspace.paths)
        for path in _tracked_configs(base):
            if path in visible:
                continue
            if not path.is_file():
                # Tracked but deleted here: there is nothing to load.
                report("note", path, "is tracked by Git but not on disk")
                continue
            try:
                # Loading proves the file parses. Building the registry
                # from its folder proves its commands can be run there.
                other = load_workspace(path.parent)
                build_control_plane(other)
            except SpecError as error:
                errors += 1
                report("error", path, str(error))
                continue
            count = 0
            if other is not None:
                for layer in (other.root,) + other.folders:
                    if layer.path == path:
                        count = len(layer.commands)
            report("ok", path, _count(count, "command"))

        # The same rule the registry merges by: a folder command
        # replaces a farther command whose name it claims, and takes
        # away a farther command's alias it claims.
        fold = normalizer(workspace.root.naming)
        visible: list[CommandConfig] = list(workspace.root.commands)
        for folder in workspace.folders:
            for command in folder.commands:
                claimed = {fold(name) for name in (command.name,) + command.aliases}
                kept: list[CommandConfig] = []
                for existing in visible:
                    if existing.source == command.source:
                        kept.append(existing)
                        continue
                    if fold(existing.name) in claimed:
                        report(
                            "note",
                            command.source,
                            command.name
                            + " replaces "
                            + existing.name
                            + " from "
                            + _relative(existing.source, base),
                        )
                        continue
                    for alias in existing.aliases:
                        if fold(alias) in claimed:
                            report(
                                "note",
                                command.source,
                                command.name
                                + " takes the alias "
                                + alias
                                + " from "
                                + existing.name
                                + " in "
                                + _relative(existing.source, base),
                            )
                    kept.append(existing)
                kept.append(command)
                visible = kept

        for command in merged:
            for script in _missing_scripts(command, workspace):
                report("warn", command.source, command.name + ": no file at " + script)

        if workspace.root.make is not None:
            path = makefile_path(workspace.root)
            try:
                expected = render_makefile(workspace.root)
            except SpecError as error:
                errors += 1
                report("error", path, str(error))
            else:
                if check_makefile(path, expected):
                    report("ok", path, "matches x.toml")
                elif not is_generated(path):
                    errors += 1
                    report(
                        "error",
                        path,
                        "was not generated by x; `makefile --force` would replace it",
                    )
                else:
                    errors += 1
                    report("error", path, "is stale; run `makefile` to regenerate it")

        if errors:
            return 1
        return 0

    return handler


# --- init ------------------------------------------------------------


def _init(workspace: Workspace | None) -> CommandHandler:
    def handler(invocation: Invocation) -> int:
        context = invocation.context
        if invocation.argv:
            return _usage(invocation, "init")
        target = context.cwd / CONFIG_NAME
        if target.exists():
            print("x: " + str(target) + " already exists", file=context.stderr)
            return 1
        if workspace is None:
            contents = ROOT_TEMPLATE.format(version=__version__)
        else:
            contents = FOLDER_TEMPLATE
        try:
            target.write_bytes(contents.encode("utf-8"))
        except OSError as error:
            reason = error.strerror or str(error)
            print("x: cannot write " + str(target) + ": " + reason, file=context.stderr)
            return 1
        context.stdout.write("wrote " + CONFIG_NAME + "\n")
        return 0

    return handler


# --- makefile --------------------------------------------------------


def _makefile(workspace: Workspace | None) -> CommandHandler:
    def handler(invocation: Invocation) -> int:
        context = invocation.context
        check = False
        force = False
        for argument in invocation.argv:
            if argument == "--check":
                check = True
            elif argument == "--force":
                force = True
            else:
                return _usage(invocation, "makefile [--check] [--force]")
        if workspace is None or workspace.root.make is None:
            print(
                "x: makefile needs a [make] table in the root x.toml",
                file=context.stderr,
            )
            return 2

        path = makefile_path(workspace.root)
        try:
            contents = render_makefile(workspace.root)
        except SpecError as error:
            print("x: " + str(error), file=context.stderr)
            return 2
        if check:
            if check_makefile(path, contents):
                return 0
            print(
                "x: " + str(path) + " is stale; run `makefile` to regenerate it",
                file=context.stderr,
            )
            return 1
        if not force and not is_generated(path):
            print(
                "x: "
                + str(path)
                + " was not generated by x; pass --force to replace it",
                file=context.stderr,
            )
            return 1
        try:
            write_makefile(path, contents)
        except OSError as error:
            reason = error.strerror or str(error)
            print("x: cannot write " + str(path) + ": " + reason, file=context.stderr)
            return 1
        return 0

    return handler


# --- resume ----------------------------------------------------------


def _resume(workspace: Workspace | None) -> CommandHandler:
    def handler(invocation: Invocation) -> int:
        context = invocation.context
        configured: tuple[ResumeAgentConfig, ...] = ()
        if workspace is not None:
            configured = workspace.root.resume
        try:
            grammar = resume_grammar(configured)
            request = sessions.parse_resume_argv(invocation.argv, grammar)
            plan = sessions.resume_plan(
                invocation.argv,
                sessions.home_directory(context.environ),
                context.root,
                grammar,
            )
        except sessions.ResumeError as error:
            print("resume: " + str(error), file=context.stderr)
            return 2

        selected = None
        for agent in configured:
            if agent.agent == plan.agent:
                selected = agent
        if selected is None:
            print(
                "resume: no launcher for agent '"
                + plan.agent
                + "'; add [resume.agents."
                + plan.agent
                + "] to the root x.toml",
                file=context.stderr,
            )
            return 2

        directory = plan.cwd
        if directory is None:
            directory = context.root
        values = {
            "name": "resume",
            "root": str(context.root),
            "caller": str(context.cwd),
            "python": sys.executable,
            "agent": plan.agent,
            "session": plan.session_id,
            "cwd": str(directory),
        }

        # x knows how to resume the agents whose stores it reads. For
        # any other agent the arguments come from its table.
        resume_arguments = plan.arguments
        if selected.custom:
            if plan.session_id == sessions.LATEST:
                if selected.last is None:
                    print(
                        "resume: '"
                        + sessions.LATEST
                        + "' is not configured for "
                        + plan.agent
                        + "; add `last` to [resume.agents."
                        + plan.agent
                        + "] or pass a session id",
                        file=context.stderr,
                    )
                    return 2
                resume_arguments = expand(selected.last, values)
            else:
                resume_arguments = expand(selected.resume, values)

        if selected.command is not None:
            if invocation.control_plane.resolve(selected.command) is None:
                print(
                    "resume: this repository has no '" + selected.command + "' command",
                    file=context.stderr,
                )
                return 2
            # The global flags are passed along, because argv below
            # starts at the command name and re-parsing it finds none.
            return invocation.control_plane.dispatch(
                (selected.command,)
                + expand(selected.args, values)
                + resume_arguments
                + request.passthrough,
                context,
                invocation.flags,
            )

        return launch(
            Invocation(
                argv=request.passthrough,
                context=context,
                flags=invocation.flags,
                control_plane=invocation.control_plane,
                name="resume",
            ),
            selected.handler,
            cwd=directory,
            values=values,
            suffix=resume_arguments,
        )

    return handler


def specs(
    workspace: Workspace | None, merged: Sequence[CommandConfig]
) -> tuple[CommandSpec, ...]:
    return (
        CommandSpec(
            "help", "List commands, or describe one.", _help(workspace, merged)
        ),
        CommandSpec("version", "Print the running x version.", _version),
        CommandSpec(
            "check",
            "Validate x.toml and the generated Makefile.",
            _check(workspace, merged),
        ),
        CommandSpec("init", "Write a starter x.toml in this folder.", _init(workspace)),
        CommandSpec(
            "makefile",
            "Write the generated Makefile, or check it with --check.",
            _makefile(workspace),
        ),
        CommandSpec(
            "resume",
            "Resume a coding-agent session by its id.",
            _resume(workspace),
        ),
    )
