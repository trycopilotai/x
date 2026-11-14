"""Reserved machine-policy command transport; never execute the workload here."""

from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import select
import signal
import socket
import stat
import struct
import sys
from typing import Any, IO, Sequence
import uuid


BROKER_SOCKET = Path("/run/machines-run/broker.sock")
MAX_REQUEST_BYTES = 64 * 1024
MAX_FRAME_BYTES = 1024 * 1024
CHUNK_BYTES = 16 * 1024
MAX_PENDING_BYTES = 128 * 1024
MACHINE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
PROVISION = re.compile(
    r"([1-9][0-9]{0,7})(s|m|h|d),([0-9]{1,12})(?:\.([0-9]{1,6}))?usd\Z"
)
USAGE = "x run (--here | --remote=<machine_id>) [--provision-ok=1h,10usd] [--explain] [--capture-output] [--] <command> [args...]"


class RunError(ValueError):
    """A bounded, non-secret transport or command error."""


@dataclass(frozen=True)
class RunOptions:
    target: dict[str, str]
    argv: tuple[str, ...]
    provision_ok: dict[str, int] | None
    explain: bool
    capture_output: bool


def parse_provision(value: str) -> dict[str, int]:
    match = PROVISION.fullmatch(value)
    if match is None:
        raise RunError("provision authority needs a positive duration and decimal USD")
    duration, unit, dollars, fraction = match.groups()
    multiplier = {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit]
    seconds = int(duration) * multiplier
    micros = int(dollars) * 1_000_000
    if fraction is not None:
        micros += int(fraction.ljust(6, "0"))
    if seconds > 2_678_400 or not 0 < micros <= 10**12:
        raise RunError("provision authority is outside the supported range")
    return {"seconds": seconds, "micro_usd": micros}


def parse_run(arguments: Sequence[str]) -> RunOptions:
    target = None
    provision = None
    explain = False
    capture = False
    seen = set()
    index = 0
    for index, token in enumerate(arguments):
        if not isinstance(token, str) or "\0" in token:
            raise RunError("command arguments must be text without NUL")
        if token == "--":
            index += 1
            break
        if not token.startswith("-"):
            break
        key = token.split("=", 1)[0]
        if key in seen:
            raise RunError("duplicate runner option")
        seen.add(key)
        if token == "--here":
            if target is not None:
                raise RunError("exactly one execution target is required")
            target = {"kind": "here"}
        elif token.startswith("--remote="):
            if target is not None:
                raise RunError("exactly one execution target is required")
            machine = token.removeprefix("--remote=")
            if MACHINE_ID.fullmatch(machine) is None:
                raise RunError("remote machine identity is invalid")
            target = {"kind": "remote", "machine_id": machine}
        elif token.startswith("--provision-ok="):
            provision = parse_provision(token.removeprefix("--provision-ok="))
        elif token == "--explain":
            explain = True
        elif token == "--capture-output":
            capture = True
        else:
            raise RunError("unknown runner option")
    else:
        index = len(arguments)
    argv = tuple(arguments[index:])
    if target is None:
        raise RunError("exactly one execution target is required")
    if not argv or not argv[0]:
        raise RunError("a command is required")
    for token in argv:
        if not isinstance(token, str) or "\0" in token:
            raise RunError("command arguments must be text without NUL")
    if target["kind"] == "here" and provision is not None:
        raise RunError("provision authority requires a remote target")
    return RunOptions(target, argv, provision, explain, capture)


def encode_frame(document: dict) -> bytes:
    try:
        payload = json.dumps(document, ensure_ascii=True, separators=(",", ":")).encode(
            "ascii"
        )
    except (TypeError, ValueError, UnicodeError):
        raise RunError("request cannot be encoded") from None
    if len(payload) > MAX_REQUEST_BYTES:
        raise RunError("request exceeds the transport limit")
    return payload + b"\n"


def make_request(options: RunOptions, cwd: Path, *, run_id: str | None = None) -> dict:
    if run_id is None:
        run_id = str(uuid.uuid4())
    document = {
        "version": 1,
        "operation": "run",
        "target": options.target,
        "argv": list(options.argv),
        "provision_ok": options.provision_ok,
        "capture_output": options.capture_output,
        "run_id": run_id,
        "workspace": str(cwd),
        "tty": False,
    }
    if options.explain:
        document["operation"] = "explain"
    encode_frame(document)
    return document


def connect_broker() -> socket.socket:
    if not sys.platform.startswith("linux") or not hasattr(socket, "SO_PEERCRED"):
        raise RunError("machine-policy transport requires a qualified Linux broker")
    try:
        for path in (Path("/"), Path("/run"), BROKER_SOCKET.parent):
            info = path.lstat()
            if (
                not stat.S_ISDIR(info.st_mode)
                or info.st_uid != 0
                or info.st_mode & 0o022
            ):
                raise RunError("broker directory is not trusted")
        info = BROKER_SOCKET.lstat()
        if not stat.S_ISSOCK(info.st_mode) or info.st_uid != 0:
            raise RunError("broker socket is not trusted")
        channel = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            channel.settimeout(5)
            channel.connect(str(BROKER_SOCKET))
            credentials = channel.getsockopt(
                socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")
            )
            _pid, uid, _gid = struct.unpack("3i", credentials)
            if uid != 0:
                raise RunError("broker identity is not trusted")
            channel.setblocking(False)
            return channel
        except BaseException:
            channel.close()
            raise
    except OSError:
        raise RunError("machine-policy broker is unavailable") from None


def _descriptor(stream: IO[Any]) -> int | None:
    try:
        return stream.fileno()
    except (AttributeError, OSError, ValueError):
        return None


def _write_output(stream: IO[Any], payload: bytes) -> None:
    binary = getattr(stream, "buffer", None)
    if binary is not None:
        binary.write(payload)
        binary.flush()
        return
    try:
        stream.write(payload)
    except TypeError:
        stream.write(payload.decode("utf-8", "replace"))
    stream.flush()


def consume_event(
    document: object, request: dict, stdout: IO[Any], stderr: IO[Any]
) -> int | None:
    if not isinstance(document, dict):
        raise RunError("invalid broker event")
    kind = document.get("event")
    if kind == "accepted":
        if (
            set(document) != {"event", "run_id"}
            or document.get("run_id") != request["run_id"]
        ):
            raise RunError("invalid broker acceptance identity")
        return None
    if kind == "artifact":
        if set(document) != {"event", "run_id", "sha256", "bytes", "path"}:
            raise RunError("invalid broker artifact")
        if document.get("run_id") != request["run_id"]:
            raise RunError("invalid broker artifact identity")
        try:
            canonical = str(uuid.UUID(request["run_id"]))
        except (ValueError, AttributeError):
            raise RunError("invalid broker artifact identity") from None
        if canonical != request["run_id"]:
            raise RunError("invalid broker artifact identity")
        expected_path = (
            "/var/lib/machines-run/results/"
            + str(os.getuid())
            + "/"
            + canonical
            + ".tar"
        )
        size = document.get("bytes")
        digest = document.get("sha256")
        if (
            document.get("path") != expected_path
            or type(size) is not int
            or not 0 <= size <= 64 * 1024 * 1024
        ):
            raise RunError("invalid broker artifact location or size")
        if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
            raise RunError("invalid broker artifact digest")
        _write_output(
            stderr,
            (
                "x run: result artifact "
                + json.dumps(document, ensure_ascii=True, sort_keys=True)
                + "\n"
            ).encode("utf-8"),
        )
        return None
    if kind in ("stdout", "stderr"):
        if set(document) != {"event", "data"} or not isinstance(document["data"], str):
            raise RunError("invalid broker output")
        try:
            payload = base64.b64decode(document["data"], validate=True)
        except (ValueError, binascii.Error):
            raise RunError("invalid broker output") from None
        stream = stdout
        if kind == "stderr":
            stream = stderr
        _write_output(stream, payload)
        return None
    if kind == "decision":
        if set(document) != {
            "event",
            "decision",
            "rule_id",
            "policy_revision",
            "target",
            "limits",
        }:
            raise RunError("invalid broker decision")
        if document.get("decision") not in ("allow", "deny"):
            raise RunError("invalid broker decision")
        for key in ("rule_id", "policy_revision"):
            if not isinstance(document.get(key), str):
                raise RunError("invalid broker decision")
        if document.get("target") != request["target"] or not isinstance(
            document.get("limits"), dict
        ):
            raise RunError("invalid broker decision target or limits")
        if (
            document["decision"] == "deny"
            and document["rule_id"] == "capacity-unavailable"
        ):
            _write_output(
                stderr, b"x run: local execution capacity is currently reserved\n"
            )
        if request["operation"] == "explain":
            _write_output(
                stdout, (json.dumps(document, sort_keys=True) + "\n").encode("utf-8")
            )
        return None
    if kind == "exit":
        required = {"event", "code", "run_id"}
        optional = {"cleanup_verified", "disposition", "metrics"}
        if not required.issubset(document) or set(document) - required - optional:
            raise RunError("invalid broker exit")
        if (
            "cleanup_verified" in document
            and type(document["cleanup_verified"]) is not bool
        ):
            raise RunError("invalid broker cleanup outcome")
        if "disposition" in document:
            disposition = document["disposition"]
            if (
                not isinstance(disposition, str)
                or re.fullmatch(r"[a-z][a-z0-9-]{0,63}", disposition) is None
            ):
                raise RunError("invalid broker disposition")
        if "metrics" in document:
            metrics = document["metrics"]
            if not isinstance(metrics, dict) or len(metrics) > 16:
                raise RunError("invalid broker metrics")
            for key, value in metrics.items():
                if re.fullmatch(r"[a-z][a-z0-9_]{0,63}", key) is None:
                    raise RunError("invalid broker metrics")
                if key == "metrics_complete":
                    if type(value) is not bool:
                        raise RunError("invalid broker metrics completeness")
                elif key.endswith(("digest", "sha256")):
                    if (
                        not isinstance(value, str)
                        or re.fullmatch(r"[0-9a-f]{64}", value) is None
                    ):
                        raise RunError("invalid broker metrics")
                elif type(value) is not int or not 0 <= value <= 2**63 - 1:
                    raise RunError("invalid broker metrics")
        if document.get("cleanup_verified") is False:
            raise RunError("broker outcome requires cleanup reconciliation")
        code = document.get("code")
        if (
            type(code) is not int
            or not -127 <= code <= 255
            or document.get("run_id") != request["run_id"]
        ):
            raise RunError("invalid broker exit identity or status")
        if code < 0:
            return 128 - code
        return code
    if kind == "error":
        if not isinstance(document.get("code"), str):
            raise RunError("invalid broker error")
        if document.get("code") == "capacity-unavailable":
            raise RunError("local execution capacity is currently reserved")
        # The broker's raw message might contain submitted secrets. Surface only
        # the protocol outcome, without echoing opaque server data.
        raise RunError("machine-policy broker refused the request")
    raise RunError("unknown broker event")


def advance_sequence(document: object, request: dict, state: dict) -> None:
    """Reject output or successful outcomes without ordered durable admission."""
    if not isinstance(document, dict):
        raise RunError("invalid broker event")
    kind = document.get("event")
    if kind == "decision":
        if state["decision"] is not None or state["accepted"]:
            raise RunError("broker decision is out of order")
        state["decision"] = document.get("decision")
    elif kind == "accepted":
        if (
            request["operation"] != "run"
            or state["decision"] != "allow"
            or state["accepted"]
        ):
            raise RunError("broker acceptance is out of order")
        state["accepted"] = True
    elif kind in ("stdout", "stderr", "artifact"):
        if not state["accepted"] or request["operation"] != "run":
            raise RunError("broker output preceded durable admission")
        if kind == "artifact":
            if state["artifact"]:
                raise RunError("duplicate broker result artifact")
            state["artifact"] = True
    elif kind == "exit":
        decision = state["decision"]
        code = document.get("code")
        if request["operation"] == "explain":
            expected = 126
            if decision == "allow":
                expected = 0
            if (
                decision not in ("allow", "deny")
                or code != expected
                or state["accepted"]
            ):
                raise RunError("explanation outcome differs from its decision")
        elif decision == "deny":
            if code != 126 or state["accepted"]:
                raise RunError("denied broker request reported an invalid outcome")
        elif decision != "allow" or not state["accepted"]:
            raise RunError("broker exit preceded durable admission")


def stream_request(
    channel: socket.socket,
    request: dict,
    stdin: IO[Any],
    stdout: IO[Any],
    stderr: IO[Any],
) -> int:
    pending = bytearray(encode_frame(request))
    incoming = bytearray()
    input_fd = _descriptor(stdin)
    input_done = request["operation"] == "explain"
    cancellation = []
    state = {"decision": None, "accepted": False, "artifact": False}
    previous = {}

    def cancel(number, _frame):
        name = "TERM"
        if number == signal.SIGINT:
            name = "INT"
        if name not in cancellation:
            cancellation.append(name)

    for number in (signal.SIGINT, signal.SIGTERM):
        try:
            previous[number] = signal.signal(number, cancel)
        except ValueError:
            pass
    try:
        while True:
            if cancellation:
                pending.extend(
                    encode_frame({"event": "cancel", "signal": cancellation.pop(0)})
                )
                input_done = True
            read_set = [channel]
            if not input_done and len(pending) < MAX_PENDING_BYTES:
                if input_fd is None:
                    chunk = stdin.read(CHUNK_BYTES)
                    if isinstance(chunk, str):
                        chunk = chunk.encode("utf-8")
                    if chunk:
                        pending.extend(
                            encode_frame(
                                {
                                    "event": "stdin",
                                    "data": base64.b64encode(chunk).decode("ascii"),
                                }
                            )
                        )
                    else:
                        input_done = True
                        pending.extend(encode_frame({"event": "stdin_eof"}))
                else:
                    read_set.append(input_fd)
            write_set = []
            if pending:
                write_set.append(channel)
            readable, writable, _exceptional = select.select(
                read_set, write_set, [], 0.25
            )
            if channel in writable:
                try:
                    sent = channel.send(pending)
                    del pending[:sent]
                except BlockingIOError:
                    pass
            if input_fd is not None and input_fd in readable and not input_done:
                chunk = os.read(input_fd, CHUNK_BYTES)
                if chunk:
                    pending.extend(
                        encode_frame(
                            {
                                "event": "stdin",
                                "data": base64.b64encode(chunk).decode("ascii"),
                            }
                        )
                    )
                else:
                    input_done = True
                    pending.extend(encode_frame({"event": "stdin_eof"}))
            if channel not in readable:
                continue
            try:
                chunk = channel.recv(CHUNK_BYTES)
            except BlockingIOError:
                continue
            if not chunk:
                raise RunError("broker disconnected before a durable outcome")
            incoming.extend(chunk)
            while b"\n" in incoming:
                line, _separator, remainder = incoming.partition(b"\n")
                incoming = bytearray(remainder)
                if len(line) > MAX_FRAME_BYTES:
                    raise RunError("broker event exceeds the transport limit")
                try:
                    document = json.loads(line)
                except (ValueError, UnicodeError, RecursionError):
                    raise RunError("invalid broker event encoding") from None
                advance_sequence(document, request, state)
                status = consume_event(document, request, stdout, stderr)
                if status is not None:
                    return status
            if len(incoming) > MAX_FRAME_BYTES:
                raise RunError("broker event exceeds the transport limit")
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)


def run_main(
    arguments: Sequence[str],
    cwd: Path,
    *,
    stdin: IO[Any] | None = None,
    stdout: IO[Any] | None = None,
    stderr: IO[Any] | None = None,
) -> int:
    if stdin is None:
        stdin = sys.stdin
    if stdout is None:
        stdout = sys.stdout
    if stderr is None:
        stderr = sys.stderr
    if list(arguments) in (["--help"], ["-h"]):
        stdout.write(USAGE + "\n")
        return 0
    try:
        options = parse_run(arguments)
        if not options.explain and getattr(stdin, "isatty", lambda: False)():
            raise RunError("interactive terminals are not qualified for this transport")
        request = make_request(options, cwd)
        with connect_broker() as channel:
            return stream_request(channel, request, stdin, stdout, stderr)
    except RunError as error:
        print("x run: " + str(error), file=stderr)
        return 125
    except (OSError, ValueError):
        print(
            "x run: transport failed; execution outcome requires broker reconciliation",
            file=stderr,
        )
        return 125
