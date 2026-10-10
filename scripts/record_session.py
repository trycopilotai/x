#!/usr/bin/env python3
"""Record a terminal session with x and refresh the evidence manifest.

    python3 scripts/record_session.py

The commands are the ones listed in ``evidence/demo-manifest.json``.
They run, in order, in an empty throwaway directory, each through
``sh -c``. Two programs are put on ``PATH`` ahead of ``/usr/bin``
and ``/bin`` for the session:

- ``x``, which starts ``bin/x.js`` from this checkout with the
  ``node`` that was on ``PATH`` when the script started;
- ``python3``, which starts the interpreter running this script, so
  the launcher uses that interpreter.

Everything else the commands use, ``make`` and ``tail`` among them,
is the system's.

The transcript is what a shell would show: each command line
prefixed with ``$ ``, followed by that command's combined standard
output and standard error. Nothing is edited. The script writes the
``$ `` lines itself; every other line was printed by a command.

The manifest's hashes of the engine, the launcher, the skill, its
interface file and the transcript are then rewritten, with the date,
the interpreter and the Node version. Run ``scripts/generate_demo.py``
afterwards to rebuild the images.

If a command exits with a status other than zero, the script prints
the capture, writes neither file, and exits 1.

This records the command-line tool. It does not follow the skill and
starts no agent.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import platform
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "evidence" / "demo-manifest.json"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def program_paths() -> list[str]:
    """The files that make up the tool, as the npm package ships them."""
    paths = ["__main__.py", "bin/x.js"]
    for path in sorted((ROOT / "x").glob("*.py")):
        paths.append("x/" + path.name)
    return paths


def record(commands: list, workdir: Path, bindir: Path) -> tuple:
    environment = {"PATH": "%s:/usr/bin:/bin" % bindir}
    lines = []
    failed = False
    for command in commands:
        result = subprocess.run(
            ["sh", "-c", command],
            cwd=str(workdir),
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            universal_newlines=True,
        )
        lines.append("$ " + command)
        lines.extend(result.stdout.splitlines())
        if result.returncode != 0:
            lines.append("[exit status %d]" % result.returncode)
            failed = True
    return "\n".join(lines) + "\n", failed


def launcher(path: Path, argv: list) -> None:
    quoted = " ".join(shlex.quote(part) for part in argv)
    path.write_text('#!/bin/sh\nexec %s "$@"\n' % quoted, encoding="utf-8")
    path.chmod(0o755)


def capture(commands: list, node: str) -> tuple:
    """Run the session in a fresh throwaway directory."""
    with tempfile.TemporaryDirectory() as scratch:
        base = Path(scratch).resolve()
        workdir = base / "capture"
        bindir = base / "bin"
        workdir.mkdir()
        bindir.mkdir()
        launcher(bindir / "python3", [sys.executable])
        launcher(bindir / "x", [node, str(ROOT / "bin" / "x.js")])
        return record(commands, workdir, bindir)


def main() -> int:
    node = shutil.which("node")
    if node is None:
        print("node was not found on PATH; nothing written")
        return 1
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    commands = manifest["invocation"]["commands"]
    raw, failed = capture(commands, node)

    if failed:
        sys.stdout.write(raw)
        print("a command did not exit with status 0; nothing written")
        return 1
    transcript = ROOT / manifest["output"]["path"]
    transcript.parent.mkdir(parents=True, exist_ok=True)
    transcript.write_text(raw, encoding="utf-8")

    version = subprocess.run(
        [node, "--version"], stdout=subprocess.PIPE, universal_newlines=True, check=True
    ).stdout.strip()
    manifest["date"] = datetime.date.today().isoformat()
    manifest["invocation"]["interpreter"] = "Python " + platform.python_version()
    manifest["invocation"]["node"] = "Node " + version.lstrip("v")
    manifest["skill"]["sha256"] = sha256(ROOT / manifest["skill"]["path"])
    manifest["interface"]["sha256"] = sha256(ROOT / manifest["interface"]["path"])
    manifest["programs"] = [
        {"path": path, "sha256": sha256(ROOT / path)} for path in program_paths()
    ]
    manifest["output"]["sha256"] = sha256(transcript)
    MANIFEST.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print("wrote %s" % transcript.relative_to(ROOT))
    print("wrote %s" % MANIFEST.relative_to(ROOT))
    return 0


if __name__ == "__main__":
    sys.exit(main())
