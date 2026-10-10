"""Help rendering: tab-separated rows, or JSON for tools and agents."""

from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any, Sequence

from x.model import CommandSpec


@dataclass(frozen=True)
class HelpFormat:
    usage: str | None = None
    heading: str | None = None
    row: str = "{name}\t{description}\n"
    include_aliases: bool = False
    include_hidden: bool = False


def render_help(
    commands: Sequence[CommandSpec],
    formatting: HelpFormat = HelpFormat(),
) -> str:
    parts: list[str] = []
    if formatting.usage is not None:
        parts.append(formatting.usage + "\n")
    if formatting.heading is not None:
        parts.append(formatting.heading + "\n")
    for command in commands:
        if command.hidden and not formatting.include_hidden:
            continue
        name = command.name
        if formatting.include_aliases and command.aliases:
            name += " (" + ", ".join(command.aliases) + ")"
        parts.append(
            formatting.row.format(
                name=name,
                description=command.description,
            )
        )
    return "".join(parts)


def render_json(document: dict[str, Any]) -> str:
    return json.dumps(document, indent=2, sort_keys=True) + "\n"
