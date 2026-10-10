"""Shared fixtures for the test suite."""

from __future__ import annotations

import io
import os
from pathlib import Path
import tempfile
from types import MappingProxyType
import unittest

from x.main import main
from x.model import ProcessRequest, RepositoryContext


def context(
    root: Path,
    *,
    platform: str = "linux",
    environ: dict[str, str] | None = None,
    runner=None,
) -> RepositoryContext:
    selected_environ = environ
    if selected_environ is None:
        selected_environ = {}
    return RepositoryContext(
        root=root,
        cwd=root,
        environ=MappingProxyType(selected_environ),
        platform=platform,
        stdin=io.StringIO(),
        stdout=io.StringIO(),
        stderr=io.StringIO(),
        process_runner=runner,
    )


class Result:
    """The outcome of one in-process `x` run."""

    def __init__(
        self, status: int, stdout: str, stderr: str, requests: list[ProcessRequest]
    ) -> None:
        self.status = status
        self.stdout = stdout
        self.stderr = stderr
        self.requests = requests


class RepoTestCase(unittest.TestCase):
    """A test with a temporary repository it can write files into."""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve() / "repo"
        self.root.mkdir()
        # A `.git` entry bounds the upward search for x.toml, exactly
        # as a real checkout does.
        (self.root / ".git").mkdir()

    def write(self, relative: str, contents: str) -> Path:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(contents.encode("utf-8"))
        return path

    def run_real(self, *argv: str, cwd: str = ".") -> Result:
        """Run `x` in-process and let it start real child processes."""
        stdout = io.StringIO()
        stderr = io.StringIO()
        code = main(
            list(argv),
            cwd=self.root / cwd,
            environ=dict(os.environ),
            stdout=stdout,
            stderr=stderr,
        )
        return Result(code, stdout.getvalue(), stderr.getvalue(), [])

    def run_x(
        self,
        *argv: str,
        cwd: str = ".",
        environ: dict[str, str] | None = None,
        platform: str = "linux",
        status: int = 0,
    ) -> Result:
        """Run `x` in-process, recording child processes instead of starting them."""
        requests: list[ProcessRequest] = []

        def runner(request: ProcessRequest) -> int:
            requests.append(request)
            return status

        stdout = io.StringIO()
        stderr = io.StringIO()
        code = main(
            list(argv),
            cwd=self.root / cwd,
            environ=dict(environ or {}),
            platform=platform,
            process_runner=runner,
            stdout=stdout,
            stderr=stderr,
        )
        return Result(code, stdout.getvalue(), stderr.getvalue(), requests)
