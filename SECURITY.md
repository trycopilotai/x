# Security

## Reporting

Report a vulnerability privately through the repository's
Security tab ("Report a vulnerability"). If that is not
available to you, open an issue that says only that you have
a report to make, and a maintainer will arrange a private
channel. Please do not put a credential, a token, or a
private repository's contents in a public issue.

## What this tool does

x runs the programs a repository's `x.toml` names. That is
its purpose, and it is the same trust you give a Makefile or
an npm `scripts` block: **running a command from an `x.toml`
you have not read runs code its author chose.** Read the
entry first in a repository you have no reason to trust.

Three things follow from that and are easy to miss:

- `x` with no command runs the repository's `[x] default`
  command.
- A mistyped command name runs the repository's `[unknown]`
  handler, if it has one.
- `x help` runs the repository's own `help` command, if it
  defines one.

`x --builtin help` and `x --builtin help --json` read the
files and start none of the repository's programs. If the
root file pins a version other than the one running, x first
starts `npx` to fetch the pinned x, as described below.

Within that:

- **No shell, on macOS and Linux.** A handler is an argument
  list. x starts the program directly, so a space, quote,
  `$` or backtick in an argument reaches the program as text
  and is never evaluated.
- **On Windows, `cmd.exe` for some programs.** Windows runs
  a `.cmd` or `.bat` file through `cmd.exe`, which gives
  `&`, `|`, `^` and `%NAME%` a meaning inside arguments.
  `npx` and `npm` are such files, so this covers the `npx`
  and `bazel` handlers and the re-run of a pinned version. x
  does not escape arguments for `cmd.exe`.
- **`make` uses a shell.** A generated Makefile recipe is
  run by `make` through a shell, as every recipe is, and
  arguments passed as `ARGS=` are expanded by that shell.
- **No environment files.** x does not read `.env` or any
  other secrets file. A command sees the environment x was
  started with, plus the caller's folder under the names in
  `caller_env` (`X_CALLER_DIRECTORY` by default), plus what
  its `x.toml` entry sets or removes.
- **Which `x.toml` is read.** Inside a Git repository, the
  files from the working directory up to the repository's
  top folder, and never above it. In a folder that is in no
  Git repository, the file in that folder only; a file in a
  folder above it is not read. `check` also reads the
  `x.toml` files Git tracks below the root.
- **Programs x starts for itself.** Apart from the handlers:
  `git ls-files`, during `check`, to find those tracked
  files; and `npx --yes @tryc.ai/x@<version>` when
  `[x] version` names a different version than the one
  running. The version must be an exact version number; a
  range, a tag or another package name does not load. `npx`
  uses the network when that version is not in its cache.
- **Bazelisk is not pinned.** A `bazel` handler runs
  `npx --yes @bazel/bazelisk`, which fetches whichever
  Bazelisk version `npx` resolves.
- **What x writes.** `init` writes a new `x.toml` and never
  replaces one. `makefile` writes the generated Makefile,
  through a temporary file beside it, and refuses to replace
  a Makefile it did not generate unless given `--force`.
  Python may also write its bytecode cache, `__pycache__`,
  beside the engine's own files.
- **Session lookup is local and read-only.** `resume` reads
  the session files that six coding agents keep under your
  home directory, to find which agent owns an id and which
  folder it ran in. It prints none of their contents and
  sends nothing anywhere; it then starts the launcher your
  `x.toml` configures for that agent.

## The launcher

`bin/x.js` looks for a Python interpreter on `PATH`, or uses
the one `X_PYTHON` names, and runs the engine with `-E -s`
so that `PYTHONPATH` and the user site directory cannot
replace the engine's modules. It removes
`npm_config_package` and `npm_config_call` from the
environment so that an `npx` inside a command runs the
package that command asked for.

## Scripts in this repository

None of these scripts is in the npm package, which holds the
tool, `docs/reference.md`, the README and the license.

- `scripts/record_session.py` runs the eight commands listed
  in `evidence/demo-manifest.json`, each through `sh -c`, in
  a temporary folder it removes afterwards. Those commands
  run x, which starts `node` and Python, and `make` and
  `tail`. When every command exits 0 it rewrites the
  transcript and the manifest; otherwise it writes neither.
- `scripts/generate_demo.py` writes `assets/demo.svg` and
  `assets/poster.svg`, or neither with `--check`.
  `scripts/verify_demo.py` writes nothing.
- `assets/build.py` starts a local Chrome or Chromium,
  headless and with a temporary profile, to screenshot
  `assets/social-preview.html`, and writes the PNG and its
  stamp. With `--check` it starts nothing and writes
  nothing.
- `scripts/release.py pack` runs `git status`,
  `git rev-parse` and `npm pack`, and writes `dist/`.
  `scripts/release.py publish` also runs `git describe`,
  `npm whoami`, `npm view` and `npm publish`; the npm
  commands use the network and the npm login of whoever runs
  it. Unless given `--allow-untagged`, it refuses to publish
  when `dist/` was not packed from a clean tree at the
  commit the version's tag points to.
- The tests create temporary folders and remove them. Some
  start `node`, Python, `sh`, `make -n`,
  `npm pack --dry-run` and `git`, and one runs the recorded
  session again. No test opens a network connection itself;
  what `npm` does when it is started is npm's own behaviour.
  Running them can leave Python's `__pycache__` folders in
  the checkout.
- The CI workflow pins each action to a commit and uses no
  secret.

## Supply chain

The package has no dependencies, on npm or PyPI, and no
install scripts. `scripts/release.py` builds the four npm
names from one commit and refuses to continue unless their
files are identical apart from the package name.
