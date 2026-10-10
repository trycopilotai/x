"""Native-Windows to WSL argv adaptation."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
import re
from typing import Sequence

from x.model import ProcessRequest, RepositoryContext


_WSL_UNC = re.compile(
    r"^\\\\wsl(?:\.localhost|\$)?\\(?P<distro>[^\\]+)\\(?P<tail>.*)$",
    re.IGNORECASE,
)
_DRIVE = re.compile(r"^(?P<drive>[A-Za-z]):[\\/](?P<tail>.*)$")


@dataclass(frozen=True)
class WslOptions:
    distro: str | None = None
    executable: str = "wsl.exe"
    bash: str = "bash"


def windows_to_wsl_path(value: str) -> tuple[str | None, str]:
    unc = _WSL_UNC.match(value)
    if unc is not None:
        tail = unc.group("tail").replace("\\", "/")
        return unc.group("distro"), "/" + tail.lstrip("/")

    drive = _DRIVE.match(value)
    if drive is not None:
        letter = drive.group("drive").lower()
        tail = drive.group("tail").replace("\\", "/")
        return None, "/mnt/" + letter + "/" + tail.lstrip("/")

    normalized = value.replace("\\", "/")
    if normalized.startswith("/"):
        return None, normalized
    raise ValueError("unsupported Windows path: " + value)


def wsl_bash_request(
    context: RepositoryContext,
    relative_script: str,
    argv: Sequence[str],
    options: WslOptions,
    directory: Path | None = None,
) -> ProcessRequest:
    """Build the `wsl.exe` command that runs a script with bash.

    The script starts in `directory`, or in the root when none is
    given, translated to the path WSL knows that folder by.
    """
    selected = context.root
    if directory is not None:
        selected = directory
    root_value = str(PureWindowsPath(str(selected)))
    detected_distro, wsl_root = windows_to_wsl_path(root_value)
    distro = options.distro
    if distro is None:
        distro = detected_distro

    command: list[str] = [options.executable]
    if distro is not None:
        command.extend(["-d", distro])
    command.extend(["--cd", wsl_root, "--exec", options.bash, relative_script])
    command.extend(argv)
    return ProcessRequest(
        argv=tuple(command),
        cwd=context.cwd,
        environ=dict(context.environ),
        stdin=context.stdin,
        stdout=context.stdout,
        stderr=context.stderr,
    )
