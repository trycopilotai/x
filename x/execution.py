"""Argv-safe child process execution and handler factories."""

from __future__ import annotations

import errno
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
from typing import Mapping, Sequence

from x.model import Invocation, ProcessRequest
from x.wsl import WslOptions, wsl_bash_request


# npm exports the flags of the `npx` that started this process to
# every child. Left in place they hijack a nested `npx`, which then
# runs the outer package instead of the one it was asked for.
NPM_LEAKED_ENV = ("npm_config_package", "npm_config_call")


def _resolve_executable(argv: Sequence[str], environ: Mapping[str, str]) -> list[str]:
    """Return argv with a Windows command shim resolved to its file.

    `npx` on Windows is `npx.cmd`; process creation does not apply
    PATHEXT, so the bare name is not found without this.
    """
    resolved = list(argv)
    if os.name == "nt" and resolved:
        found = shutil.which(resolved[0], path=environ.get("PATH"))
        if found is not None:
            resolved[0] = found
    return resolved


def _forwarded_signals() -> tuple[int, ...]:
    names = ("SIGTERM", "SIGHUP", "SIGINT", "SIGQUIT")
    selected = []
    for name in names:
        number = getattr(signal, name, None)
        if number is not None:
            selected.append(number)
    return tuple(selected)


def _terminal_delivers(signal_number: int) -> bool:
    """True when the terminal already sent this signal to the child.

    Ctrl-C and Ctrl-\\ go to the whole foreground process group, so
    the child has its own copy. Sending a second one makes programs
    that treat a repeated interrupt as "force quit" skip their
    cleanup.
    """
    keyboard = (signal.SIGINT, getattr(signal, "SIGQUIT", None))
    if signal_number not in keyboard:
        return False
    try:
        return os.isatty(sys.stdin.fileno())
    except (AttributeError, OSError, ValueError):
        return False


def _inheritable(stream):
    """The stream if a child can be given it, else None to inherit ours.

    A program that embeds x may hand it streams that are not files,
    such as an in-memory buffer. A child process cannot write to
    those, so it gets this process's own stream instead.
    """
    if stream is None:
        return None
    try:
        stream.fileno()
    except (AttributeError, OSError, ValueError):
        return None
    return stream


def start_process(request: ProcessRequest) -> subprocess.Popen:
    """Start the child. Raises OSError when it cannot be started."""
    return subprocess.Popen(
        _resolve_executable(request.argv, request.environ),
        cwd=request.cwd,
        env=dict(request.environ),
        stdin=_inheritable(request.stdin),
        stdout=_inheritable(request.stdout),
        stderr=_inheritable(request.stderr),
    )


def run_process(request: ProcessRequest) -> int:
    return wait_for_process(start_process(request))


def wait_for_process(process: subprocess.Popen) -> int:
    """Wait for a started child, relaying signals, and return its status.

    A child ended by a signal ends this process by the same signal.
    """

    def forward(signal_number, _frame) -> None:
        if _terminal_delivers(signal_number):
            return
        try:
            process.send_signal(signal_number)
        except (OSError, ValueError):
            pass

    previous: dict[int, object] = {}
    if os.name != "nt":
        for signal_number in _forwarded_signals():
            try:
                previous[signal_number] = signal.signal(signal_number, forward)
            except (OSError, ValueError):
                # Not the main thread, or a signal this host refuses.
                pass
    try:
        while True:
            try:
                returncode = process.wait()
                break
            except KeyboardInterrupt:
                # Windows, or a host where the handler above could not
                # be installed: the child received the interrupt too,
                # so keep waiting for it to finish on its own terms.
                continue
    finally:
        for signal_number, handler in previous.items():
            signal.signal(signal_number, handler)

    if returncode < 0 and os.name != "nt":
        signal_number = -returncode
        try:
            signal.signal(signal_number, signal.SIG_DFL)
        except (OSError, ValueError):
            # SIGKILL and SIGSTOP have no handler to reset.
            pass
        os.kill(os.getpid(), signal_number)
    return returncode


def build_request(
    invocation: Invocation,
    argv: Sequence[str],
    *,
    cwd: Path | None = None,
    remove_env: Sequence[str] = (),
    extra_env: Mapping[str, str] | None = None,
) -> ProcessRequest:
    context = invocation.context
    environment = dict(context.environ)
    for key in remove_env:
        environment.pop(key, None)
    if extra_env is not None:
        environment.update(extra_env)

    process_cwd = context.root
    if cwd is not None:
        process_cwd = cwd
    return ProcessRequest(
        argv=tuple(argv),
        cwd=process_cwd,
        environ=environment,
        stdin=context.stdin,
        stdout=context.stdout,
        stderr=context.stderr,
    )


def execute(invocation: Invocation, request: ProcessRequest) -> int:
    """Start one child and return its status.

    A child that cannot be started is reported in one line: 127 when
    the program or its working directory does not exist, 126 when it
    exists but cannot be run.
    """
    runner = invocation.context.process_runner
    stderr = invocation.context.stderr
    executable = ""
    if request.argv:
        executable = request.argv[0]
    # Where a program written with a path would be, to tell a file
    # that is missing from one that is there and cannot be run.
    named = None
    if "/" in executable or os.sep in executable:
        named = Path(request.cwd) / executable
    started = None
    status = 0
    try:
        if runner is None:
            started = start_process(request)
        else:
            status = runner(request)
    except FileNotFoundError:
        if not Path(request.cwd).is_dir():
            print("x: working directory not found: " + str(request.cwd), file=stderr)
            return 127
        if named is not None and named.is_file():
            print(
                "x: cannot run "
                + executable
                + ": the interpreter on its #! line was not found",
                file=stderr,
            )
            return 126
        print("x: executable not found: " + executable, file=stderr)
        return 127
    except PermissionError:
        reason = "permission denied (is it executable? try chmod +x)"
        if named is not None and named.is_dir():
            reason = "it is a folder"
        print("x: cannot run " + executable + ": " + reason, file=stderr)
        return 126
    except OSError as error:
        reason = error.strerror or str(error)
        if error.errno == errno.ENOEXEC:
            reason = "not a program (a script needs a #! first line)"
        print("x: cannot run " + executable + ": " + reason, file=stderr)
        return 126
    except ValueError as error:
        # An argument or environment value no process can be given,
        # such as one holding a NUL character.
        print("x: cannot run " + executable + ": " + str(error), file=stderr)
        return 126
    # Waiting is outside the block above on purpose: nothing that
    # happens after the child has started is a failure to start it.
    if started is not None:
        return wait_for_process(started)
    return status


def executable_handler(
    prefix: Sequence[str],
    *,
    remove_env: Sequence[str] = (),
    extra_env: Mapping[str, str] | None = None,
    cwd: Path | None = None,
):
    fixed = tuple(prefix)

    def handler(invocation: Invocation) -> int:
        request = build_request(
            invocation,
            fixed + invocation.argv,
            cwd=cwd,
            remove_env=remove_env,
            extra_env=extra_env,
        )
        return execute(invocation, request)

    return handler


def npx_handler(
    prefix: Sequence[str],
    *,
    remove_env: Sequence[str] = (),
    extra_env: Mapping[str, str] | None = None,
    cwd: Path | None = None,
):
    return executable_handler(
        ("npx",) + tuple(prefix),
        remove_env=NPM_LEAKED_ENV + tuple(remove_env),
        extra_env=extra_env,
        cwd=cwd,
    )


def bazel_handler(
    prefix: Sequence[str],
    *,
    remove_env: Sequence[str] = (),
    extra_env: Mapping[str, str] | None = None,
    cwd: Path | None = None,
):
    return npx_handler(
        ("--yes", "@bazel/bazelisk") + tuple(prefix),
        remove_env=remove_env,
        extra_env=extra_env,
        cwd=cwd,
    )


def bash_handler(
    relative_script: str,
    prefix: Sequence[str] = (),
    *,
    options: WslOptions | None = None,
    remove_env: Sequence[str] = (),
    extra_env: Mapping[str, str] | None = None,
    cwd: Path | None = None,
):
    """Run a bash script, through WSL when the host is Windows.

    `relative_script` is resolved against the working directory of
    the child, which is the repository root unless `cwd` says
    otherwise.
    """
    selected_options = options
    if selected_options is None:
        selected_options = WslOptions()
    fixed = tuple(prefix)

    def handler(invocation: Invocation) -> int:
        context = invocation.context
        if context.platform == "windows":
            request = wsl_bash_request(
                context,
                relative_script,
                fixed + invocation.argv,
                selected_options,
                cwd,
            )
            environment = dict(request.environ)
            for key in remove_env:
                environment.pop(key, None)
            if extra_env is not None:
                environment.update(extra_env)
            request = ProcessRequest(
                argv=request.argv,
                cwd=request.cwd,
                environ=environment,
                stdin=request.stdin,
                stdout=request.stdout,
                stderr=request.stderr,
            )
        else:
            argv = ("bash", relative_script) + fixed + invocation.argv
            request = build_request(
                invocation,
                argv,
                cwd=cwd,
                remove_env=remove_env,
                extra_env=extra_env,
            )
        return execute(invocation, request)

    return handler
