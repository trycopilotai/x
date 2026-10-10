"""Host platform and invocation context construction."""

from __future__ import annotations

import os
from pathlib import Path
import sys
from typing import Mapping

from x.model import ProcessRunner, RepositoryContext


def host_platform() -> str:
    if os.name == "nt":
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    return "linux"


def repository_context(
    *,
    root: Path,
    cwd: Path | None = None,
    environ: Mapping[str, str] | None = None,
    platform: str | None = None,
    process_runner: ProcessRunner | None = None,
) -> RepositoryContext:
    current = Path.cwd()
    if cwd is not None:
        current = cwd

    environment: Mapping[str, str]
    if environ is None:
        environment = os.environ.copy()
    else:
        environment = dict(environ)

    selected_platform = platform
    if selected_platform is None:
        selected_platform = host_platform()

    return RepositoryContext(
        root=root.resolve(),
        cwd=current.resolve(),
        environ=environment,
        platform=selected_platform,
        stdin=sys.stdin,
        stdout=sys.stdout,
        stderr=sys.stderr,
        process_runner=process_runner,
    )
