"""Building the command registry from the files that apply here."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import sys

from x import commands as builtins
from x.config import (
    CommandConfig,
    ConfigError,
    DelegateConfig,
    HandlerConfig,
    RootConfig,
    Workspace,
    normalizer,
)
from x.dispatch import ControlPlane
from x.handlers import expand_one, flag_tokens, launch
from x.model import CommandHandler, CommandSpec, FlagSpec, Invocation


def merge_commands(workspace: Workspace) -> tuple[CommandConfig, ...]:
    """Return the commands visible from the starting folder.

    Root commands come first. A folder command replaces any farther
    command it collides with by name or alias, and takes a colliding
    alias away from a farther command that keeps its own name.
    """
    fold = normalizer(workspace.root.naming)
    merged: list[CommandConfig] = list(workspace.root.commands)
    for folder in workspace.folders:
        # Replacement is between files. Two commands in one file that
        # answer to the same name are a mistake in that file.
        owners: dict[str, str] = {}
        for command in folder.commands:
            for name in (command.name,) + command.aliases:
                folded = fold(name)
                owner = owners.get(folded)
                if owner is not None and owner != command.name:
                    raise ConfigError(
                        str(folder.path)
                        + ": the name '"
                        + folded
                        + "' is claimed by both "
                        + owner
                        + " and "
                        + command.name
                    )
                owners[folded] = command.name
        for command in folder.commands:
            claimed = {fold(name) for name in (command.name,) + command.aliases}
            kept: list[CommandConfig] = []
            for existing in merged:
                if existing.source == command.source:
                    kept.append(existing)
                    continue
                if fold(existing.name) in claimed:
                    continue
                aliases = tuple(
                    alias for alias in existing.aliases if fold(alias) not in claimed
                )
                if aliases != existing.aliases:
                    existing = replace(existing, aliases=aliases)
                kept.append(existing)
            kept.append(command)
            merged = kept
    return tuple(merged)


def command_cwd(command: CommandConfig, workspace: Workspace, caller: Path) -> Path:
    if command.cwd == "config":
        return command.directory
    if command.cwd == "root":
        return workspace.directory
    if command.cwd == "caller":
        return caller
    return command.directory / command.cwd


def _values(invocation: Invocation, workspace: Workspace) -> dict[str, str]:
    return {
        "name": invocation.name,
        "root": str(workspace.directory),
        "caller": str(invocation.context.cwd),
        # The interpreter running x, which is known to be new enough.
        "python": sys.executable,
    }


def _process_handler(
    handler: HandlerConfig,
    command: CommandConfig,
    workspace: Workspace,
) -> CommandHandler:
    root = workspace.root

    def run(invocation: Invocation) -> int:
        values = _values(invocation, workspace)
        extra_env = {key: expand_one(value, values) for key, value in command.env}
        return launch(
            invocation,
            handler,
            cwd=command_cwd(command, workspace, invocation.context.cwd),
            values=values,
            flags=flag_tokens(root.flags, invocation.flags),
            extra_env=extra_env,
            remove_env=command.unset_env,
        )

    return run


def _command_handler(
    handler: HandlerConfig,
    command: CommandConfig,
    workspace: Workspace,
    builtin_handlers: dict[str, CommandHandler],
) -> CommandHandler:
    if handler.kind == "builtin":
        selected = builtin_handlers.get(handler.target)
        if selected is None:
            raise ConfigError(
                str(command.source)
                + ": [commands."
                + command.name
                + "] builtin: no built-in named '"
                + handler.target
                + "'; choose from "
                + ", ".join(sorted(builtin_handlers))
            )
        return selected
    return _process_handler(handler, command, workspace)


def _spec(
    command: CommandConfig,
    workspace: Workspace,
    builtin_handlers: dict[str, CommandHandler],
) -> CommandSpec:
    defaults = workspace.root.defaults
    handler = command.handler
    platform_handlers = command.platform_handlers
    if handler is None:
        if defaults.handler is None:
            raise ConfigError(
                str(command.source)
                + ": [commands."
                + command.name
                + "]: needs one of run, bash, npx, bazel, builtin, or a "
                "[defaults] handler in the root x.toml"
            )
        handler = defaults.handler
        if not platform_handlers:
            platform_handlers = defaults.platform_handlers
    return CommandSpec(
        name=command.name,
        description=command.description,
        handler=_command_handler(handler, command, workspace, builtin_handlers),
        platform_handlers=tuple(
            (
                platform,
                _command_handler(selected, command, workspace, builtin_handlers),
            )
            for platform, selected in platform_handlers
        ),
        aliases=command.aliases,
        platforms=command.platforms,
        hidden=command.hidden,
    )


def _unknown_handler(
    delegate: DelegateConfig, workspace: Workspace
) -> CommandHandler | None:
    if delegate.handler is None:
        return None
    by_platform = dict(delegate.platform_handlers)

    def run(invocation: Invocation) -> int:
        handler = by_platform.get(invocation.context.platform, delegate.handler)
        return launch(
            invocation,
            handler,
            cwd=workspace.directory,
            values=_values(invocation, workspace),
        )

    return run


def build_control_plane(
    workspace: Workspace | None,
) -> tuple[ControlPlane, tuple[CommandSpec, ...]]:
    """Return the registry for `workspace` and the built-in specs.

    Without an `x.toml` only the built-ins exist. With one, a command
    the repository declares wins over a built-in of the same name; the
    built-ins it leaves alone stay reachable but are not listed.
    """
    if workspace is None:
        specs = builtins.specs(None, ())
        return ControlPlane(commands=specs), specs

    root: RootConfig = workspace.root
    merged = merge_commands(workspace)
    builtin_specs = builtins.specs(workspace, merged)
    builtin_handlers = {spec.name: spec.handler for spec in builtin_specs}
    fold = normalizer(root.naming)

    commands = [_spec(command, workspace, builtin_handlers) for command in merged]
    declared: set[str] = set()
    for command in merged:
        for name in (command.name,) + command.aliases:
            declared.add(fold(name))
    for spec in builtin_specs:
        if fold(spec.name) not in declared:
            commands.append(replace(spec, hidden=True))

    plane = ControlPlane(
        commands=tuple(commands),
        flags=tuple(
            FlagSpec(names=flag.names, key=flag.key, takes_value=flag.takes_value)
            for flag in root.flags
        ),
        normalizer=fold,
        default_command=root.default,
        unknown=_unknown_handler(root.unknown, workspace),
    )
    return plane, builtin_specs
