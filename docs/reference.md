# `x.toml` reference

Every key x reads. A key that is not listed here is an
error: the file does not load and x exits with status 2,
naming the file, the table and the key.

A complete small file:

```toml
[x]
version = "0.1.2"

[commands.test]
description = "Run the tests."
run = ["pytest", "-q"]
```

## Which files apply

Inside a Git repository, x looks for `x.toml` from the
working directory upward, one folder at a time, and stops at
the repository's top folder, the one that holds `.git`. In a
folder that is in no Git repository, only that folder is
searched: a file in a folder above it never applies.

- The first file with an `[x]` table is the **root file**.
  The search stops there. Its folder is the root.
- Files found before it are **folder files**. They may hold
  `[commands.*]` tables and nothing else.
- No file at all means only the built-in commands exist.
- Folder files with no root file above them are an error.

`x --root <folder>` starts the search from `<folder>`
instead; see [Command line](#command-line).

## `[x]`

| Key          | Type             | Default                  | Meaning                                                      |
| ------------ | ---------------- | ------------------------ | ------------------------------------------------------------ |
| `version`    | string, required | none                     | The exact x version this repository uses, such as `"0.1.2"`. |
| `naming`     | string           | `"exact"`                | How command names are matched: `exact`, `kebab` or `snake`.  |
| `default`    | string           | `"help"`                 | The command run when none is given.                          |
| `caller_env` | array of strings | `["X_CALLER_DIRECTORY"]` | Environment variables set to the folder x was started from.  |

When the running x is not the pinned version, x starts the
pinned one with `npx --yes @tryc.ai/x@<version>` and the
same arguments, once. `--version` reports the running
version without reading any `x.toml`.

### Naming

- `exact`: a name matches only as written.
- `snake`: `fooBar`, `foo.bar`, `foo-bar`, `Foo_Bar` and
  `foo_bar` all match the command `foo_bar`.
- `kebab`: the same spellings all match `foo-bar`.

Two commands, or a command and an alias, that fold to the
same name are an error.

## `[flags.<key>]`

A global flag is accepted before the command name. `--` ends
the global flags.

| Key           | Type                       | Default | Meaning                                                             |
| ------------- | -------------------------- | ------- | ------------------------------------------------------------------- |
| `names`       | array of strings, required | none    | Spellings, each starting with `-`.                                  |
| `takes_value` | boolean                    | `false` | Whether a value follows, as `--name value` or `--name=value`.       |
| `forward`     | array of strings           | `[]`    | Tokens that replace `{flags}` in a handler when the flag was given. |

Inside `forward`, `{value}` is the flag's value. A flag with
no `forward` is accepted and then dropped.

```toml
[flags.quiet]
names = ["-q", "--quiet"]
forward = ["--quiet"]

[commands.build]
description = "Build everything."
run = ["tools/build.sh", "{flags}"]
```

`x -q build fast` runs `tools/build.sh --quiet fast`.

## `[commands.<name>]`

Quote a name TOML would not accept bare, such as
`[commands."v2.migrate"]`.

| Key           | Type             | Default    | Meaning                                                     |
| ------------- | ---------------- | ---------- | ----------------------------------------------------------- |
| `description` | string, required | none       | One line, shown by `help`.                                  |
| `aliases`     | array of strings | `[]`       | Other names for the command.                                |
| `platforms`   | array of strings | all        | Any of `linux`, `macos`, `windows`. Elsewhere it exits 127. |
| `hidden`      | boolean          | `false`    | Runs normally but is left out of `help`.                    |
| `cwd`         | string           | `"config"` | Working directory; see below.                               |
| `env`         | table            | `{}`       | Environment variables to set. Values may use placeholders.  |
| `unset_env`   | array of strings | `[]`       | Environment variables to remove.                            |
| `make`        | boolean or table | `true`     | Makefile behaviour; see below.                              |

### Handlers

Each command has exactly one handler. The arguments typed
after the command name are appended to it.

| Key       | Value                                    | Runs                                                                |
| --------- | ---------------------------------------- | ------------------------------------------------------------------- |
| `run`     | array of strings                         | That argv.                                                          |
| `bash`    | script path; `args`, an array of strings | `bash <script> <args>`. On Windows, through WSL.                    |
| `npx`     | array of strings                         | `npx` followed by that argv.                                        |
| `bazel`   | array of strings                         | `npx --yes @bazel/bazelisk` followed by that argv.                  |
| `builtin` | name of a built-in                       | x's own `help`, `version`, `check`, `init`, `makefile` or `resume`. |

A command with no handler uses `[defaults]`. With no
`[defaults]` either, the file does not load.

On macOS and Linux x never runs a handler through a shell:
each array element reaches the program as one argument. On
Windows a program that is a `.cmd` or `.bat` file, which
includes `npx`, is run by `cmd.exe`, and `cmd.exe` gives
some characters in arguments a meaning.

A `run` program written with a `/` must be an executable
file. A script needs a `#!` first line naming an interpreter
that exists. x reports a program that is there but cannot be
run in one line and exits 126; one that is not there, 127.

x does not choose a Bazelisk version: `bazel` runs whichever
one `npx --yes` resolves.

### Placeholders

Inside `run`, `npx`, `bazel` and `args`:

| Placeholder | Becomes                                                                        |
| ----------- | ------------------------------------------------------------------------------ |
| `{name}`    | The command's own name, whichever alias was typed.                             |
| `{root}`    | The root file's folder.                                                        |
| `{caller}`  | The folder x was started from.                                                 |
| `{python}`  | The Python interpreter running x.                                              |
| `{flags}`   | The `forward` tokens of each global flag given. Only as a whole array element. |

The `bash` script path and `env` values take the first four.
`{flags}` has no meaning there and is left as written.

### `cwd`

| Value           | Working directory                                                    |
| --------------- | -------------------------------------------------------------------- |
| `"config"`      | The folder of the `x.toml` that declares the command.                |
| `"root"`        | The root file's folder.                                              |
| `"caller"`      | The folder x was started from.                                       |
| a relative path | That path, relative to the declaring file's folder. It may use `..`. |

A relative `bash` script, or a `run` program written with a
`/`, is found relative to this working directory. A working
directory that does not exist is reported and x exits 127.

### Per-platform handlers

`[commands.<name>.windows]`, `.macos` and `.linux` hold one
handler each and replace the command's handler there. They
cannot hold `builtin`.

```toml
[commands.open]
description = "Open the report."
run = ["xdg-open", "report.html"]

[commands.open.macos]
run = ["open", "report.html"]
```

## `[defaults]`

One handler, used by every command that declares none.
`{name}` is what makes it useful:

```toml
[defaults]
bash = "tools/cli.sh"
args = ["{flags}", "{name}"]
```

`[defaults.windows]`, `.macos` and `.linux` override it per
platform. Neither `[defaults]` nor `[unknown]` can hold
`builtin`.

## `[unknown]`

One handler for a request that matches no command. It
receives the whole command line as typed, global flags
included, runs at the root, and its exit status is returned.
Without this table an unknown command prints
`x: unknown command: <name>` and exits 2. Per-platform
tables work as in `[defaults]`.

## `[make]`

Present: `x makefile` writes a Makefile from the root file's
commands. Absent: there is no generated Makefile.

| Key        | Type             | Default                  | Meaning                                                          |
| ---------- | ---------------- | ------------------------ | ---------------------------------------------------------------- |
| `path`     | string           | `"Makefile"`             | Where to write, inside the root's folder. Its folder must exist. |
| `launcher` | string           | `npx --yes @tryc.ai/x@…` | What `X` is set to in the generated header.                      |
| `default`  | string           | none                     | Sets `.DEFAULT_GOAL` in the generated header.                    |
| `header`   | array of strings | generated                | Lines that replace the generated header entirely.                |
| `order`    | string           | `"declared"`             | `declared`, or `alpha` to sort targets by name.                  |
| `args`     | boolean          | `true`                   | Whether recipes end with `$(ARGS)`.                              |
| `aliases`  | boolean          | `false`                  | Whether each alias gets a target of its own.                     |

The generated header is:

```make
X := npx --yes @tryc.ai/x@0.1.2
ARGS ?=
```

A `header` must define `X` itself, and cannot be combined
with `launcher` or `default`.

Each target is:

```make
.PHONY: build
build:
	@$(X) build $(ARGS)
```

A command becomes a target under its own name, so that name
may hold only letters, digits, `_`, `.`, `+` and `-`. Any
other name needs `make = false`.

### Per-command `make`

`make = false` leaves the command out of the Makefile, and
`make = true` is the default. Otherwise
`[commands.<name>.make]` accepts:

| Key       | Type             | Meaning                                                              |
| --------- | ---------------- | -------------------------------------------------------------------- |
| `args`    | boolean          | Overrides `[make] args` for this target.                             |
| `aliases` | boolean          | Overrides `[make] aliases` for this command's aliases.               |
| `recipe`  | array of strings | Recipe lines that replace the generated one. x adds the leading tab. |
| `prelude` | array of strings | Lines written between `.PHONY:` and the target line.                 |
| `after`   | array of strings | Lines written as their own block after the target, verbatim.         |

Only root-file commands become targets. Folder commands
never do. With alias targets on, `recipe`, `prelude` and
`after` apply to the command's own target only; each alias
target keeps the generated recipe, under the alias's name.

`x makefile --check` exits 1 when the file on disk differs
from what would be written. `x makefile` refuses to replace
a Makefile whose first line does not say x generated it,
unless given `--force`.

## `[resume.agents.<agent>]`

`x resume <id>` restarts a coding-agent session. x supplies
the lookup; this table supplies how to start the agent. With
no table for an agent, `resume` says which one to add.

### Agents x reads

x reads the session files of six agents under the home
directory, so it can tell which of them recorded an id:

| Agent    | Product     | Also named                   | Reads                                                        | Resumes with              |
| -------- | ----------- | ---------------------------- | ------------------------------------------------------------ | ------------------------- |
| `claude` | Claude Code | `claude-code`, `claude_code` | `~/.claude/projects/`                                        | `--resume <id>`           |
| `codex`  | Codex       |                              | `~/.codex/sessions/`                                         | `-C <folder> resume <id>` |
| `agy`    | Antigravity | `antigravity`                | `~/.gemini/antigravity-cli/conversations/`                   | `--conversation <id>`     |
| `kimi`   | Kimi Code   | `kimi-code`, `kimi_code`     | `~/.kimi-code/sessions/`, `~/.kimi-code/session_index.jsonl` | `--session <id>`          |
| `pi`     | Pi          |                              | `~/.pi/agent/sessions/`                                      | `--session <id>`          |
| `grok`   | Grok Build  | `grok-build`, `grok_build`   | `~/.grok/sessions/`                                          | `--resume <id>`           |

These layouts and arguments are what the code encodes. They
were not checked against the agents' current releases.

| Key                           | Type             | Meaning                                                  |
| ----------------------------- | ---------------- | -------------------------------------------------------- |
| `run`, `npx`, `bazel`, `bash` | as for a command | A launcher: how to start the agent.                      |
| `command`                     | string           | Instead of a launcher: a command here to dispatch to.    |
| `args`                        | array of strings | With `command`: arguments placed before the resume ones. |
| `aliases`                     | array of strings | More names for this agent in a request.                  |

x appends the agent's resume arguments, then any arguments
of the request it did not recognise.

```toml
[resume.agents.claude]
npx = ["--yes", "@anthropic-ai/claude-code"]
aliases = ["cl"]
```

A launcher starts in the folder the session recorded. When
the session recorded none, that folder no longer exists, or
the agent was named for an id x did not find on disk, it
starts at the root. Antigravity records no folder. A
dispatched `command` runs where that command always runs, so
pass it the folder through `args` if it needs one.

### Any other agent

A table under a name that is not in the list above declares
a custom agent. x reads nothing from disk for it: it must be
named in the request, and its table states the arguments.

| Key      | Type                       | Meaning                                                                             |
| -------- | -------------------------- | ----------------------------------------------------------------------------------- |
| `resume` | array of strings, required | The arguments that resume a session.                                                |
| `last`   | array of strings           | The arguments for the newest session. Without it, `last` is refused for this agent. |

```toml
[resume.agents.helper]
run = ["helper-cli"]
resume = ["--resume", "{session}"]
last = ["--continue"]
```

A custom agent's launcher starts at the root. `resume` and
`last` are refused in the table of an agent x reads.

### Placeholders

Launcher arrays, `args`, `resume` and `last` take `{root}`,
`{caller}` and `{python}`, and also:

| Placeholder | Becomes                                                 |
| ----------- | ------------------------------------------------------- |
| `{agent}`   | The agent's name.                                       |
| `{session}` | The session id; `last` for a custom agent asked for it. |
| `{cwd}`     | The folder the launcher starts in, as described above.  |

`{flags}` expands to nothing here.

### Requests

```text
x resume <id>                  find the agent from the id
x resume UUID=<id>             the same; also SESSION=, ID=
x resume CLAUDE=<id>           name the agent
x resume CLAUDE=last           that agent's newest session
x resume --agent claude <id>   the same with a flag; also --agent=claude, AGENT=claude
x resume --uuid <id>           also --session, --id, and each with =<id>
```

Keys are matched without regard to case, and any agent name
or alias works as a key. `last` prefers the newest session
recorded in the root's folder and otherwise takes the newest
one anywhere.

An argument that is none of these is passed on to the agent.
Until an id has been given, the first argument that starts
with a letter or digit and holds only letters, digits, `.`,
`_` and `-` is taken as the id. So give the id before any
option that takes a value: `x resume <id> --model large`.

## Folder files

```toml
# web/x.toml
[commands.dev]
description = "Serve the site."
run = ["npm", "run", "dev"]
```

- Only `[commands.*]` is allowed.
- A folder command replaces a command from a farther file
  that it collides with by name or alias, and takes a
  colliding alias away from one that keeps its own name. Two
  commands in the same file that answer to one name are an
  error.
- `help` lists what is visible from the working directory.
- `check`, run from inside the folder, reports each
  replacement and each alias taken as a `note`.
- On Windows a `bash` handler starts in the same folder,
  translated to the path WSL knows it by.

## Built-in commands

| Command    | Does                                                                                                                                                                                                                                                   |
| ---------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `help`     | Lists commands; `help <name>`; `--json` for tools; `--all` to include hidden ones and built-ins.                                                                                                                                                       |
| `version`  | Prints the running version.                                                                                                                                                                                                                            |
| `check`    | Loads the files that apply here, and every other `x.toml` Git tracks under the root as it would load from its own folder; warns about missing scripts; compares the Makefile. A folder file Git does not track is checked only from inside its folder. |
| `init`     | Writes a starter `x.toml` here: a root file, or a folder file below a root. Never overwrites.                                                                                                                                                          |
| `makefile` | Writes the Makefile, or compares it with `--check`.                                                                                                                                                                                                    |
| `resume`   | Resumes a coding-agent session by id.                                                                                                                                                                                                                  |

A repository command with the same name as a built-in wins.
`x --builtin <name>` always runs the built-in.

### `help --json`

One object. `commands` lists every command, the repository's
first and then the built-ins the repository did not replace,
each with the same keys:

| Key                                                     | Value                                                                                                     |
| ------------------------------------------------------- | --------------------------------------------------------------------------------------------------------- |
| `name`, `description`, `aliases`, `platforms`, `hidden` | As declared.                                                                                              |
| `builtin`                                               | `true` for one of x's own commands that no repository command replaced.                                   |
| `kind`                                                  | The handler: `run`, `bash`, `npx`, `bazel`, `builtin`, or `default` for a command that uses `[defaults]`. |
| `cwd`                                                   | The command's `cwd` setting as written, such as `"config"`; `null` for a built-in.                        |
| `source`                                                | The declaring `x.toml`, relative to the root; `null` for a built-in.                                      |
| `make`                                                  | Whether the command is a target in the generated Makefile.                                                |

When the repository has an `x.toml`, the built-ins it did
not replace are marked `hidden`. The object also holds
`schema` (1), `version` (the running x), `pinned`, `root`,
`configs` (the files that apply, root first), `default`,
`naming` and `flags`. Without an `x.toml`, `root` is `null`
and `pinned` is absent.

## Command line

```text
x [--root <folder>] [--builtin <name> | <global flags> <command>] [arguments]
```

| Option             | Meaning                                                              |
| ------------------ | -------------------------------------------------------------------- |
| `--root <folder>`  | Look for `x.toml` from `<folder>` instead of the working directory.  |
| `--builtin <name>` | Run the built-in, even if the repository has a command of that name. |
| `--version`        | Print the running version.                                           |
| `--help`, `-h`     | The same as the `help` command.                                      |

`--root` does not change where the caller is: `{caller}`,
`cwd = "caller"` and the `caller_env` variables still name
the folder x was started from. It lets a launcher script
that lives in one repository be run from another checkout
and still dispatch its own repository's commands.

## Exit status

The status of the command that ran, with these exceptions
that come from x itself:

| Status | Meaning                                                                                                                                                                    |
| ------ | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 1      | `check` found an error, `makefile --check` found a stale file, or `init` or `makefile` did not write.                                                                      |
| 2      | Unknown command, bad usage, an `x.toml` that does not load, `makefile` with no `[make]` table or a command name Make cannot use, or a `resume` request that cannot be met. |
| 126    | The program is there but could not be run.                                                                                                                                 |
| 127    | Program or working directory not found, unsupported platform, no Python 3.11 or newer, or `npx` could not be started for a pinned version.                                 |
| 141    | The reader of x's own output went away, as in `x help \| head -1` on long output.                                                                                          |

Messages from x start with `x: `, or `resume: ` from that
built-in. An `x.toml` that does not load is reported with
its path, and with the table and key when one key is at
fault. A fault found only when the files are put together,
such as two commands that answer to one name, is reported
with the root file's path.

## Signals

On macOS and Linux, x forwards `SIGTERM` and `SIGHUP` to the
command. It forwards `SIGINT` and `SIGQUIT` too, except
while its standard input is a terminal, because a terminal
delivers those to the command itself. Other signals are not
forwarded.

A command ended by a signal ends x by the same signal, with
exceptions on a Node version in which the launcher stays
running as the parent of the engine (Node 18 and 20, and any
other without `process.execve`). There, for a signal that
Node keeps for itself or ignores, x exits with 128 plus the
signal's number instead: `SIGUSR1`, `SIGPIPE` and `SIGXFSZ`
were seen to behave this way. A signal Node has no name for,
such as `SIGEMT` on macOS, is reported there as status 0.

None of this has been run on Windows. There, the engine
forwards no signals to the command.

## Environment

| Variable             | Direction | Meaning                                                          |
| -------------------- | --------- | ---------------------------------------------------------------- |
| `X_PYTHON`           | read      | The Python interpreter the launcher must use.                    |
| `X_CALLER_DIRECTORY` | set       | The folder x was started from, unless `caller_env` names others. |
| `HOME`               | read      | Where `resume` looks for session files.                          |

`npm_config_package` and `npm_config_call`, which `npx` can
set in the environment of what it starts, are removed so
that an `npx` inside a command runs what it was asked to.
