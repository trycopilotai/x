"""Actual Node/Python/Unix-socket client integration on a disposable root worker.

This fixture authenticates a real root socket peer and preserves binary streams.
It does not execute workload commands or qualify kernel/provider containment.
"""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import unittest


ROOT = Path(__file__).resolve().parents[1]
SOCKET = Path("/run/machines-run/broker.sock")
NODE = shutil.which("node")


@unittest.skipUnless(
    sys.platform.startswith("linux")
    and getattr(os, "geteuid", lambda: -1)() == 0
    and NODE,
    "real root Linux broker fixture and Node are required",
)
class InstalledTransportTest(unittest.TestCase):
    def test_real_launcher_peer_identity_argv_and_binary_streams(self):
        if os.path.lexists(SOCKET):
            self.skipTest("an installed broker already owns the qualified socket")
        created_parent = False
        if not SOCKET.parent.exists():
            SOCKET.parent.mkdir(mode=0o750)
            created_parent = True
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        bound = False
        failures = []
        observed = []
        try:
            listener.bind(str(SOCKET))
            bound = True
            SOCKET.chmod(0o666)
            listener.listen(1)
            listener.settimeout(10)
            content = bytes(range(256)) * 300
            payload = [
                "bash",
                "-lc",
                "printf '雪'; echo '$HOME'",
                "--remote=payload",
                "",
            ]

            def broker():
                try:
                    channel, _address = listener.accept()
                    with channel, channel.makefile("rb") as stream:
                        request = json.loads(stream.readline(65538))
                        observed.append(request)
                        decision = {
                            "event": "decision",
                            "decision": "allow",
                            "rule_id": "fixture-allow",
                            "policy_revision": "fixture",
                            "target": request["target"],
                            "limits": {},
                        }
                        for frame in (
                            decision,
                            {"event": "accepted", "run_id": request["run_id"]},
                        ):
                            channel.sendall(json.dumps(frame).encode() + b"\n")
                        while True:
                            event = json.loads(stream.readline(65538))
                            if event["event"] == "stdin_eof":
                                break
                            self.assertEqual(event["event"], "stdin")
                            base64.b64decode(event["data"], validate=True)
                            channel.sendall(
                                json.dumps(
                                    {"event": "stdout", "data": event["data"]}
                                ).encode()
                                + b"\n"
                            )
                        channel.sendall(
                            json.dumps(
                                {
                                    "event": "exit",
                                    "code": 37,
                                    "run_id": request["run_id"],
                                    "cleanup_verified": True,
                                    "disposition": "exited",
                                    "metrics": {"elapsed_ms": 1},
                                }
                            ).encode()
                            + b"\n"
                        )
                except BaseException as error:
                    failures.append(error)

            thread = threading.Thread(target=broker, daemon=True)
            thread.start()
            with tempfile.TemporaryDirectory() as directory:
                cwd = Path(directory)
                (cwd / "x.toml").write_text("invalid = [SECRET_SENTINEL")
                hostile = cwd / "hostile-python"
                sentinel = cwd / "probe-was-run"
                hostile.write_text("#!/bin/sh\necho bad > " + str(sentinel) + "\n")
                hostile.chmod(0o755)
                environment = dict(os.environ)
                environment.update(
                    {
                        "X_PYTHON": str(hostile),
                        "X_ENGINE": str(hostile),
                        "PYTHONPATH": str(cwd),
                        "BASH_ENV": str(hostile),
                        "SECRET_SENTINEL": "must-not-be-forwarded",
                    }
                )
                environment.pop("NODE_OPTIONS", None)
                result = subprocess.run(
                    [NODE, str(ROOT / "bin/x.js"), "run", "--here", *payload],
                    input=content,
                    capture_output=True,
                    cwd=cwd,
                    env=environment,
                    timeout=15,
                )
                self.assertEqual(result.returncode, 37, result.stderr)
                self.assertEqual(result.stdout, content)
                self.assertEqual(result.stderr, b"")
                self.assertFalse(sentinel.exists())
            thread.join(5)
            self.assertFalse(thread.is_alive())
            if failures:
                raise failures[0]
            self.assertEqual(len(observed), 1)
            self.assertEqual(observed[0]["argv"], payload)
            self.assertEqual(observed[0]["target"], {"kind": "here"})
            self.assertNotIn("environ", observed[0])
            self.assertNotIn("SECRET_SENTINEL", json.dumps(observed[0]))
        finally:
            listener.close()
            if bound:
                SOCKET.unlink(missing_ok=True)
            if created_parent:
                SOCKET.parent.rmdir()


if __name__ == "__main__":
    unittest.main()
