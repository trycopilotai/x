"""Build and publish the npm package under each of its names.

    python scripts/release.py pack
    python scripts/release.py publish

`pack` builds one tarball per name into `dist/` and proves they
carry the same files: the only difference allowed is the `name`
field of `package.json`. `publish` uploads them in order and can be
run again after a partial failure; a name that already holds this
version with the same tarball is skipped, and one that holds
different bytes stops the run.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile


ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist"
MANIFEST = DIST / "release-manifest.json"

# The first name is the canonical one: it is what package.json in
# the repository says and what the generated Makefile runs.
NAMES = ("@tryc.ai/x", "@trycopilotai/x", "@trycopilot.ai/x", "@trycai/x")


class ReleaseError(Exception):
    """The release cannot continue as asked."""


def _npm() -> str:
    found = shutil.which("npm")
    if found is None:
        raise ReleaseError("npm was not found on PATH")
    return found


def _run(
    argv: list[str], cwd: Path, *, check: bool = True
) -> subprocess.CompletedProcess:
    completed = subprocess.run(
        argv, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )
    if check and completed.returncode != 0:
        raise ReleaseError(
            " ".join(argv) + " failed:\n" + completed.stdout + completed.stderr
        )
    return completed


def _package() -> dict:
    return json.loads((ROOT / "package.json").read_text(encoding="utf-8"))


def _shipped_files() -> list[str]:
    """The files npm would pack, as npm itself reports them."""
    completed = _run([_npm(), "pack", "--dry-run", "--json"], ROOT)
    return sorted(entry["path"] for entry in json.loads(completed.stdout)[0]["files"])


def _integrity(data: bytes) -> str:
    return "sha512-" + base64.b64encode(hashlib.sha512(data).digest()).decode("ascii")


def _content_digest(tarball: Path) -> str:
    """A digest of the tarball's files that ignores the package name."""
    digest = hashlib.sha256()
    with tarfile.open(tarball, "r:gz") as archive:
        members = sorted(
            (member for member in archive.getmembers() if member.isfile()),
            key=lambda member: member.name,
        )
        for member in members:
            stream = archive.extractfile(member)
            data = b""
            if stream is not None:
                data = stream.read()
            if member.name == "package/package.json":
                document = json.loads(data.decode("utf-8"))
                document.pop("name", None)
                data = json.dumps(document, sort_keys=True).encode("utf-8")
            digest.update(member.name.encode("utf-8") + b"\0")
            digest.update(str(len(data)).encode("ascii") + b"\0")
            digest.update(data)
    return digest.hexdigest()


def _require_clean_tree() -> None:
    status = _run(["git", "status", "--porcelain"], ROOT).stdout
    if status.strip():
        raise ReleaseError("the working tree has uncommitted changes:\n" + status)


def _require_tag(version: str) -> None:
    completed = _run(
        ["git", "describe", "--tags", "--exact-match", "HEAD"], ROOT, check=False
    )
    tag = completed.stdout.strip()
    if completed.returncode != 0 or tag != "v" + version:
        raise ReleaseError(
            "HEAD must be tagged v" + version + " to publish; found '" + tag + "'"
        )


def pack(*, allow_dirty: bool) -> dict:
    if not allow_dirty:
        _require_clean_tree()
    package = _package()
    if package["name"] != NAMES[0]:
        raise ReleaseError("package.json name must be " + NAMES[0])
    version = package["version"]
    files = _shipped_files()

    shutil.rmtree(DIST, ignore_errors=True)
    DIST.mkdir()
    entries = []
    for name in NAMES:
        with tempfile.TemporaryDirectory() as staging_name:
            staging = Path(staging_name)
            for relative in files:
                target = staging / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(ROOT / relative, target)
            renamed = dict(package)
            renamed["name"] = name
            (staging / "package.json").write_text(
                json.dumps(renamed, indent=2) + "\n", encoding="utf-8"
            )
            completed = _run(
                [_npm(), "pack", "--json", "--pack-destination", str(DIST)], staging
            )
            filename = json.loads(completed.stdout)[0]["filename"]
        tarball = DIST / Path(filename).name
        entries.append(
            {
                "name": name,
                "tarball": tarball.name,
                "integrity": _integrity(tarball.read_bytes()),
                "content_sha256": _content_digest(tarball),
            }
        )

    digests = {entry["content_sha256"] for entry in entries}
    if len(digests) != 1:
        raise ReleaseError("the tarballs differ beyond the package name")
    manifest = {
        "version": version,
        "commit": _run(["git", "rev-parse", "HEAD"], ROOT).stdout.strip(),
        "clean_tree": not allow_dirty,
        "files": files,
        "packages": entries,
    }
    MANIFEST.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return manifest


def _published_integrity(name: str, version: str) -> str | None:
    completed = _run(
        [_npm(), "view", name + "@" + version, "dist.integrity"], ROOT, check=False
    )
    value = completed.stdout.strip()
    if completed.returncode != 0 or not value:
        return None
    return value


def publish(*, allow_untagged: bool) -> None:
    if not MANIFEST.is_file():
        raise ReleaseError("run `pack` first; " + str(MANIFEST) + " is missing")
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    version = manifest["version"]
    if version != _package()["version"]:
        raise ReleaseError("dist/ was packed for a different version; run `pack`")
    if not allow_untagged:
        _require_clean_tree()
        _require_tag(version)
        head = _run(["git", "rev-parse", "HEAD"], ROOT).stdout.strip()
        if manifest.get("commit") != head or not manifest.get("clean_tree"):
            raise ReleaseError(
                "dist/ was not packed from a clean tree at the tagged commit; "
                "run `pack` again"
            )
    _run([_npm(), "whoami"], ROOT)

    pending = []
    for entry in manifest["packages"]:
        tarball = DIST / entry["tarball"]
        if _integrity(tarball.read_bytes()) != entry["integrity"]:
            raise ReleaseError(entry["tarball"] + " changed since it was packed")
        published = _published_integrity(entry["name"], version)
        if published is None:
            pending.append(entry)
        elif published == entry["integrity"]:
            print("already published: " + entry["name"] + "@" + version)
        else:
            raise ReleaseError(
                entry["name"] + "@" + version + " exists with different contents"
            )

    for entry in pending:
        print("publishing " + entry["name"] + "@" + version, flush=True)
        # Not captured: npm may need to ask for a one-time code.
        status = subprocess.call(
            [_npm(), "publish", str(DIST / entry["tarball"]), "--access", "public"],
            cwd=ROOT,
        )
        if status != 0:
            raise ReleaseError(
                "npm publish failed for " + entry["name"] + "; run `publish` again"
            )

    for entry in manifest["packages"]:
        published = _published_integrity(entry["name"], version)
        if published != entry["integrity"]:
            raise ReleaseError(
                entry["name"]
                + "@"
                + version
                + " is not yet served with the packed contents; npm can take a "
                "minute, so run `publish` again to re-check"
            )
        print("verified " + entry["name"] + "@" + version)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    pack_parser = commands.add_parser("pack", help="build the tarballs into dist/")
    pack_parser.add_argument("--allow-dirty", action="store_true")
    publish_parser = commands.add_parser("publish", help="publish what pack built")
    publish_parser.add_argument("--allow-untagged", action="store_true")
    arguments = parser.parse_args(argv)
    try:
        if arguments.command == "pack":
            manifest = pack(allow_dirty=arguments.allow_dirty)
            for entry in manifest["packages"]:
                print(entry["name"] + "\t" + entry["tarball"])
            print("identical contents: " + manifest["packages"][0]["content_sha256"])
        else:
            publish(allow_untagged=arguments.allow_untagged)
    except ReleaseError as error:
        print("release: " + str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
