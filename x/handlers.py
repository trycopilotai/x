"""Turning a configured handler into one child process."""

from __future__ import annotations

from pathlib import Path
from typing import Mapping, Sequence

from x.config import ConfigError, FlagConfig, HandlerConfig
from x.execution import (
    bash_handler,
    bazel_handler,
    executable_handler,
    npx_handler,
)
from x.model import Invocation


def flag_tokens(
    flags: Sequence[FlagConfig], selected: Mapping[str, str]
) -> tuple[str, ...]:
    """Return the argv tokens that re-state the global flags in use."""
    tokens: list[str] = []
    for flag in flags:
        if flag.key not in selected:
            continue
        for token in flag.forward:
            tokens.append(token.replace("{value}", selected[flag.key]))
    return tuple(tokens)


def expand(
    tokens: Sequence[str],
    values: Mapping[str, str],
    flags: Sequence[str] = (),
) -> tuple[str, ...]:
    """Substitute `{key}` placeholders; `{flags}` expands in place."""
    expanded: list[str] = []
    for token in tokens:
        if token == "{flags}":
            expanded.extend(flags)
            continue
        for key, value in values.items():
            token = token.replace("{" + key + "}", value)
        expanded.append(token)
    return tuple(expanded)


def expand_one(text: str, values: Mapping[str, str]) -> str:
    """Substitute placeholders in one value. `{flags}` has no meaning here."""
    for key, value in values.items():
        text = text.replace("{" + key + "}", value)
    return text


def launch(
    invocation: Invocation,
    handler: HandlerConfig,
    *,
    cwd: Path,
    values: Mapping[str, str],
    flags: Sequence[str] = (),
    suffix: Sequence[str] = (),
    extra_env: Mapping[str, str] | None = None,
    remove_env: Sequence[str] = (),
) -> int:
    """Run `handler` with the invocation's arguments appended."""
    prefix = expand(handler.argv, values, flags) + tuple(suffix)
    if handler.kind == "run":
        factory = executable_handler(
            prefix, remove_env=remove_env, extra_env=extra_env, cwd=cwd
        )
    elif handler.kind == "npx":
        factory = npx_handler(
            prefix, remove_env=remove_env, extra_env=extra_env, cwd=cwd
        )
    elif handler.kind == "bazel":
        factory = bazel_handler(
            prefix, remove_env=remove_env, extra_env=extra_env, cwd=cwd
        )
    elif handler.kind == "bash":
        factory = bash_handler(
            expand_one(handler.target, values),
            prefix,
            remove_env=remove_env,
            extra_env=extra_env,
            cwd=cwd,
        )
    else:
        raise ConfigError("handler kind cannot start a process: " + handler.kind)
    return factory(invocation)
