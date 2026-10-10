"""Typed validation and command dispatch."""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping, Sequence

from x.model import (
    PLATFORMS,
    CommandHandler,
    CommandSpec,
    FlagSpec,
    Invocation,
    Normalizer,
    RepositoryContext,
    SpecError,
    identity,
)


@dataclass(frozen=True)
class ControlPlane:
    commands: tuple[CommandSpec, ...]
    flags: tuple[FlagSpec, ...] = ()
    normalizer: Normalizer = identity
    default_command: str = "help"
    unknown_command_message: str = "x: unknown command: {name}"
    # Runs a request that matched no command. It receives the whole
    # argv as it was typed, global flags included, so a repository can
    # hand anything it does not declare to an older dispatcher.
    unknown: CommandHandler | None = None

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        if not self.commands:
            raise SpecError("command registry is empty")

        identities: dict[str, str] = {}
        for command in self.commands:
            if not command.name:
                raise SpecError("command name is empty")
            if not command.description:
                raise SpecError("command description is empty: " + command.name)
            for platform in command.platforms:
                if platform not in PLATFORMS:
                    raise SpecError(
                        "unknown platform '" + platform + "': " + command.name
                    )
            platform_handlers: set[str] = set()
            for platform, _handler in command.platform_handlers:
                if platform not in PLATFORMS:
                    raise SpecError(
                        "unknown platform handler '" + platform + "': " + command.name
                    )
                if platform in platform_handlers:
                    raise SpecError(
                        "duplicate platform handler '" + platform + "': " + command.name
                    )
                platform_handlers.add(platform)
            names = (command.name,) + command.aliases
            for name in names:
                normalized = self.normalizer(name)
                if not normalized:
                    raise SpecError("normalized command name is empty: " + name)
                existing = identities.get(normalized)
                if existing is not None:
                    raise SpecError(
                        "the name '"
                        + normalized
                        + "' is claimed by both "
                        + existing
                        + " and "
                        + command.name
                    )
                identities[normalized] = command.name

        flag_names: set[str] = set()
        flag_keys: set[str] = set()
        for flag in self.flags:
            if not flag.names:
                raise SpecError("global flag has no names")
            if not flag.key:
                raise SpecError("global flag key is empty")
            if flag.key in flag_keys:
                raise SpecError("duplicate global flag key: " + flag.key)
            flag_keys.add(flag.key)
            for name in flag.names:
                if name in flag_names:
                    raise SpecError("duplicate global flag name: " + name)
                flag_names.add(name)
        if self.resolve(self.default_command) is None:
            raise SpecError("the default command does not exist: " + self.default_command)

    def resolve(self, requested: str) -> CommandSpec | None:
        normalized = self.normalizer(requested)
        for command in self.commands:
            for name in (command.name,) + command.aliases:
                if self.normalizer(name) == normalized:
                    return command
        return None

    def _parse_flags(
        self, argv: Sequence[str]
    ) -> tuple[Mapping[str, str], tuple[str, ...]]:
        values: dict[str, str] = {}
        index = 0
        while index < len(argv):
            token = argv[index]
            if token == "--":
                index += 1
                break

            matched: FlagSpec | None = None
            inline_value: str | None = None
            for flag in self.flags:
                if token in flag.names:
                    matched = flag
                    break
                for name in flag.names:
                    prefix = name + "="
                    if flag.takes_value and token.startswith(prefix):
                        matched = flag
                        inline_value = token[len(prefix) :]
                        break
                if matched is not None:
                    break

            if matched is None:
                break

            if matched.takes_value:
                if inline_value is not None:
                    values[matched.key] = inline_value
                else:
                    index += 1
                    if index >= len(argv):
                        raise SpecError("missing value for global flag " + token)
                    values[matched.key] = argv[index]
            else:
                values[matched.key] = matched.value
            index += 1

        return MappingProxyType(values), tuple(argv[index:])

    def dispatch(
        self,
        argv: Sequence[str],
        context: RepositoryContext,
        flags: Mapping[str, str] | None = None,
    ) -> int:
        """Run one command from `argv`.

        `flags` carries the global flags a command that is
        dispatching another one already parsed. Without it the
        second command loses them: `argv` here starts at the
        command name, so re-parsing finds none.
        """
        if flags is None:
            try:
                selected, remaining = self._parse_flags(argv)
            except SpecError as error:
                print("x: " + str(error), file=context.stderr)
                return 2
        else:
            selected = flags
            remaining = tuple(argv)

        if remaining:
            requested = remaining[0]
            command_argv = remaining[1:]
        else:
            requested = self.default_command
            command_argv = ()

        command = self.resolve(requested)
        if command is None:
            if self.unknown is not None:
                return int(
                    self.unknown(
                        Invocation(
                            argv=tuple(argv),
                            context=context,
                            flags=MappingProxyType({}),
                            control_plane=self,
                            name=requested,
                        )
                    )
                )
            message = self.unknown_command_message.format(name=requested)
            print(message, file=context.stderr)
            return 2
        if command.platforms and context.platform not in command.platforms:
            print(
                "x: " + command.name + ": unsupported platform: " + context.platform,
                file=context.stderr,
            )
            return 127

        invocation = Invocation(
            argv=tuple(command_argv),
            context=context,
            flags=selected,
            control_plane=self,
            name=command.name,
        )
        handler = command.handler
        for platform, platform_handler in command.platform_handlers:
            if platform == context.platform:
                handler = platform_handler
                break
        return int(handler(invocation))
