# Contributing

x is small on purpose. A change should keep three
behaviours, each of which has tests:

**The child process is untouched.** On macOS and Linux,
arguments reach it as they were typed, with no shell in
between. Its streams are the caller's, its exit status is
returned as is, and a signal that ends it ends x the same
way, subject to the older-Node relay exceptions listed in
`docs/reference.md`.

**The Makefile is a pure function of `x.toml`.** The same
file renders the same bytes on every platform, and
`makefile --check` compares bytes.

**An `x.toml` that loads means what it says.** An unknown
key is an error, never ignored. A new key needs a row in
`docs/reference.md` and a test for both its effect and its
rejection when misspelled.

One more rule has no test: launch commands, flags and paths
that belong to one repository go in that repository's
`x.toml`, not in the engine.

Before opening a pull request:

```sh
make test
```

The suite uses the standard library only. Most tests run x
in-process and start nothing. Some start `node`, Python,
`sh`, `make -n`, `npm pack --dry-run` and `git`, and one
runs the recorded session again; those that need `node`,
`make`, `npm` or `git` are skipped when it is missing.

## When the tool or the skill changes

`evidence/demo-manifest.json` records the SHA-256 of every
file under `x/`, of `bin/x.js`, `__main__.py`,
`skills/x/SKILL.md` and `skills/x/agents/openai.yaml`, and
the suite compares them. After changing any of those files,
record the evidence again and commit the result with the
change:

```sh
make record
make demo
```

`make record` needs `sh`, `make` and `tail`, so it does not
run on Windows without WSL. If you cannot run it, say so in
the pull request and the maintainer will record it.

Add a focused regression test with every behaviour change.
