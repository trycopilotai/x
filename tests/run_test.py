"""Contract and real socket tests for the reserved policy transport.

Socketpair integration exercises the actual client framing and backpressure;
it does not qualify the broker, resource isolation, or native agent providers.
"""

from __future__ import annotations

import base64
import io
import json
import os
from pathlib import Path
import random
import signal
import socket
import stat
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from tests.support import RepoTestCase
from x.run import (
    MAX_FRAME_BYTES,
    MAX_REQUEST_BYTES,
    RunError,
    connect_broker,
    consume_event,
    encode_frame,
    make_request,
    parse_provision,
    parse_run,
    run_main,
    stream_request,
)


class ParsingTest(unittest.TestCase):
    def test_explicit_here_and_exact_payload(self):
        argv = ("bash", "-lc", "echo '雪'; gcc", "--remote=payload", "", "$HOME")
        options = parse_run(("--here",) + argv)
        self.assertEqual(options.target, {"kind": "here"})
        self.assertEqual(options.argv, argv)

    def test_enrolled_machine_identifier_characters(self):
        self.assertEqual(
            parse_run(("--remote=Worker_1.profile", "true")).target["machine_id"],
            "Worker_1.profile",
        )

    def test_separator_and_optional_flags(self):
        options = parse_run(
            (
                "--remote=dev-worker",
                "--provision-ok=1h,10usd",
                "--capture-output",
                "--explain",
                "--",
                "bash",
                "--here",
            )
        )
        self.assertEqual(options.target, {"kind": "remote", "machine_id": "dev-worker"})
        self.assertEqual(options.argv, ("bash", "--here"))
        self.assertTrue(options.capture_output)
        self.assertTrue(options.explain)
        self.assertEqual(
            options.provision_ok, {"seconds": 3600, "micro_usd": 10_000_000}
        )

    def test_payload_runner_flags_are_never_parsed(self):
        options = parse_run(
            ("--here", "bash", "--provision-ok=NaN", "--here", "--remote=other")
        )
        self.assertIsNone(options.provision_ok)
        self.assertEqual(
            options.argv[1:], ("--provision-ok=NaN", "--here", "--remote=other")
        )

    def test_no_default_or_conflicting_target(self):
        cases = [
            (),
            ("bash",),
            ("--here",),
            ("--", "bash"),
            ("--here", "--remote=dev", "bash"),
            ("--remote=dev", "--here", "bash"),
            ("--here", "--here", "bash"),
            ("--remote=dev", "--remote=dev", "bash"),
        ]
        for arguments in cases:
            with self.subTest(arguments=arguments), self.assertRaises(RunError):
                parse_run(arguments)

    def test_duplicate_runner_options_are_rejected(self):
        for token in ("--explain", "--capture-output", "--provision-ok=1h,10usd"):
            with self.subTest(token=token), self.assertRaises(RunError):
                parse_run(("--remote=dev", token, token, "bash"))

    def test_malformed_targets_and_unknown_options(self):
        for token in (
            "--remote=",
            "--remote=a/b",
            "--remote=../dev",
            "--remote=" + "a" * 129,
            "--here=true",
            "--remote",
            "--socket=other",
            "--admin",
        ):
            with self.subTest(token=token), self.assertRaises(RunError):
                parse_run((token, "bash"))

    def test_invalid_arguments_do_not_echo_secret(self):
        for arguments in (
            ("--token=SECRET_SENTINEL", "bash"),
            ("--here", "bash", "SECRET_SENTINEL\0"),
        ):
            try:
                parse_run(arguments)
            except RunError as error:
                self.assertNotIn("SECRET_SENTINEL", str(error))
            else:
                self.fail("invalid request was accepted")

    def test_provision_units_and_exact_fraction(self):
        for value, seconds, micros in (
            ("1s,0.000001usd", 1, 1),
            ("3m,1.230001usd", 180, 1_230_001),
            ("4h,20usd", 14_400, 20_000_000),
            ("31d,1usd", 2_678_400, 1_000_000),
        ):
            self.assertEqual(
                parse_provision(value), {"seconds": seconds, "micro_usd": micros}
            )

    def test_invalid_provision_authority(self):
        for value in (
            "0h,10usd",
            "-1h,10usd",
            "1.5h,10usd",
            "32d,10usd",
            "1h,0usd",
            "1h,-1usd",
            "1h,nanusd",
            "1h,infusd",
            "1h,1e3usd",
            "1h,1.0000001usd",
            "1h,10USD",
            "1h,10usd ",
            "99999999h,1usd",
            "1h,1000001usd",
            "1h,10usd,extra",
        ):
            with self.subTest(value=value), self.assertRaises(RunError):
                parse_provision(value)

    def test_local_provisioning_authority_is_refused(self):
        with self.assertRaises(RunError):
            parse_run(("--here", "--provision-ok=1h,10usd", "bash"))

    def test_random_payload_roundtrips_without_expansion(self):
        rng = random.Random(1301)
        alphabet = "abc 雪\t\n$();'\"\\-="
        for _ in range(300):
            argv = ["bash"]
            for _ in range(rng.randrange(1, 12)):
                argv.append(
                    "".join(rng.choice(alphabet) for _ in range(rng.randrange(0, 40)))
                )
            options = parse_run(["--here", *argv])
            document = make_request(options, Path("/workspace"), run_id="run-test")
            self.assertEqual(json.loads(encode_frame(document))["argv"], argv)
            self.assertEqual(
                set(document),
                {
                    "version",
                    "operation",
                    "target",
                    "argv",
                    "provision_ok",
                    "capture_output",
                    "run_id",
                    "workspace",
                    "tty",
                },
            )
            self.assertFalse(document["tty"])

    def test_request_size_is_bounded(self):
        with self.assertRaises(RunError):
            make_request(
                parse_run(("--here", "bash", "x" * MAX_REQUEST_BYTES)),
                Path("/workspace"),
            )


class ReservedDispatchTest(RepoTestCase):
    def test_run_never_loads_configuration_or_repins(self):
        self.write("x.toml", "invalid = [SECRET_SENTINEL")
        with patch("x.main.run_main", return_value=37) as client, patch(
            "x.main.load_workspace", side_effect=AssertionError("workspace was read")
        ):
            result = self.run_x(
                "run",
                "--here",
                "bash",
                "-c",
                "echo ok",
                environ={
                    "X_ENGINE": "/hostile",
                    "PATH": "/hostile",
                    "PYTHONPATH": "/hostile",
                },
            )
        self.assertEqual(result.status, 37)
        self.assertEqual(result.requests, [])
        self.assertEqual(
            client.call_args.args, (["--here", "bash", "-c", "echo ok"], self.root)
        )

    def test_prefixed_run_cannot_reach_registry_or_repinned_engine(self):
        self.write("x.toml", "invalid = [SECRET_SENTINEL")
        prefixes = (
            ("--root", "missing"),
            ("--pinned",),
            ("--builtin",),
            ("--pinned", "--root", "missing"),
            ("--root", "missing", "--builtin"),
            ("--pinned", "--root", "missing", "--builtin"),
            ("--root", "missing", "--pinned"),
            ("--pinned", "--pinned"),
            ("--root", "missing", "--root", "missing"),
            ("--builtin", "--pinned", "--builtin"),
        )
        for prefix in prefixes:
            with self.subTest(prefix=prefix), patch(
                "x.main.load_workspace",
                side_effect=AssertionError("workspace was read"),
            ):
                result = self.run_x(*prefix, "run", "--here", "true")
            self.assertEqual(result.status, 125)
            self.assertEqual(result.requests, [])
            self.assertNotIn("SECRET_SENTINEL", result.stderr)

    def test_registry_run_cannot_override_reserved_command(self):
        self.write(
            "x.toml",
            '[x]\nversion = "0.0.0"\n[commands.run]\ndescription = "Hostile"\nrun = ["hostile"]\n',
        )
        with patch("x.main.run_main", return_value=0):
            result = self.run_x("run", "--here", "bash")
        self.assertEqual(result.status, 0)
        self.assertEqual(result.requests, [])

    def test_bad_request_does_not_connect_or_print_payload(self):
        output = io.StringIO()
        with patch("x.run.connect_broker", side_effect=AssertionError("connected")):
            status = run_main(
                ("--SECRET_SENTINEL",), self.root, stdin=io.StringIO(), stderr=output
            )
        self.assertEqual(status, 125)
        self.assertNotIn("SECRET_SENTINEL", output.getvalue())

    def test_help_does_not_connect(self):
        output = io.StringIO()
        with patch("x.run.connect_broker", side_effect=AssertionError("connected")):
            self.assertEqual(run_main(("--help",), self.root, stdout=output), 0)
        self.assertIn("--here", output.getvalue())

    def test_interactive_input_is_explicitly_refused(self):
        stream = io.StringIO()
        stream.isatty = lambda: True
        with patch("x.run.connect_broker", side_effect=AssertionError("connected")):
            self.assertEqual(
                run_main(
                    ("--here", "bash"), self.root, stdin=stream, stderr=io.StringIO()
                ),
                125,
            )


class EventTest(unittest.TestCase):
    def setUp(self):
        self.request = make_request(
            parse_run(("--here", "cat")), Path("/workspace"), run_id="owned-run"
        )
        self.stdout = io.BytesIO()
        self.stderr = io.BytesIO()

    def event(self, value):
        return consume_event(value, self.request, self.stdout, self.stderr)

    def test_durable_acceptance_is_bound_to_run_identity(self):
        self.assertIsNone(self.event({"event": "accepted", "run_id": "owned-run"}))
        with self.assertRaises(RunError):
            self.event({"event": "accepted", "run_id": "foreign-run"})

    def test_output_bytes_and_status_are_preserved(self):
        payload = bytes(range(256))
        self.assertIsNone(
            self.event({"event": "stdout", "data": base64.b64encode(payload).decode()})
        )
        self.assertEqual(self.stdout.getvalue(), payload)
        self.assertEqual(
            self.event({"event": "exit", "code": 37, "run_id": "owned-run"}), 37
        )
        self.assertEqual(
            self.event({"event": "exit", "code": -15, "run_id": "owned-run"}), 143
        )

    def test_exit_cleanup_metadata_is_bounded_and_not_rendered(self):
        event = {
            "event": "exit",
            "code": 0,
            "run_id": "owned-run",
            "cleanup_verified": True,
            "disposition": "exited",
            "metrics": {"elapsed_ms": 20, "keyed_digest": "a" * 64},
        }
        self.assertEqual(self.event(event), 0)
        self.assertEqual(self.stdout.getvalue(), b"")
        for key, value in (
            ("cleanup_verified", False),
            ("cleanup_verified", "true"),
            ("disposition", "SECRET_SENTINEL\n"),
            ("metrics", {"elapsed_ms": float("inf")}),
            ("metrics", {"raw_output": "SECRET_SENTINEL"}),
        ):
            modified = dict(event)
            modified[key] = value
            with self.subTest(key=key), self.assertRaises(RunError):
                self.event(modified)

    def test_metrics_completeness_is_explicit_and_strictly_boolean(self):
        event = {"event": "exit", "code": 0, "run_id": "owned-run"}
        for complete in (True, False):
            event["metrics"] = {"metrics_complete": complete, "cpu_usec": 7}
            self.assertEqual(self.event(event), 0)
        for invalid in (0, 1, "true", None):
            event["metrics"] = {"metrics_complete": invalid}
            with self.subTest(value=invalid), self.assertRaises(RunError):
                self.event(event)
        event["metrics"] = {"cpu_usec": True}
        with self.assertRaises(RunError):
            self.event(event)

    def test_wrong_identity_and_malformed_events_fail_closed(self):
        invalid = [
            None,
            [],
            {},
            {"event": "unknown"},
            {"event": "stdout", "data": "!bad!"},
            {"event": "stdout", "data": "", "extra": True},
            {"event": "exit", "code": True, "run_id": "owned-run"},
            {"event": "exit", "code": 256, "run_id": "owned-run"},
            {"event": "exit", "code": 0, "run_id": "foreign-run"},
            {"event": "decision", "decision": "allow"},
        ]
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(RunError):
                self.event(value)

    def test_capacity_denial_is_clear_and_preserves_denial_sequence(self):
        decision = {
            "event": "decision",
            "decision": "deny",
            "rule_id": "capacity-unavailable",
            "policy_revision": "revision",
            "target": self.request["target"],
            "limits": {},
        }
        self.assertIsNone(self.event(decision))
        self.assertIn(b"capacity", self.stderr.getvalue())
        self.assertEqual(
            self.event({"event": "exit", "code": 126, "run_id": "owned-run"}), 126
        )

    def test_capacity_refusal_has_clear_message_without_raw_server_text(self):
        with self.assertRaises(RunError) as error:
            self.event(
                {
                    "event": "error",
                    "code": "capacity-unavailable",
                    "message": "SECRET_SENTINEL",
                }
            )
        self.assertIn("capacity", str(error.exception))
        self.assertNotIn("SECRET_SENTINEL", str(error.exception))

    def test_raw_broker_errors_are_not_echoed(self):
        with self.assertRaises(RunError) as error:
            self.event(
                {
                    "event": "error",
                    "code": "SECRET_SENTINEL",
                    "message": "SECRET_SENTINEL",
                }
            )
        self.assertNotIn("SECRET_SENTINEL", str(error.exception))

    def test_artifact_location_identity_size_and_digest_are_bound(self):
        identifier = "c85ea761-f573-4cee-a33e-a5b46404fffb"
        self.request["run_id"] = identifier
        artifact = {
            "event": "artifact",
            "run_id": identifier,
            "bytes": 1024,
            "sha256": "a" * 64,
            "path": "/var/lib/machines-run/results/"
            + str(os.getuid())
            + "/"
            + identifier
            + ".tar",
        }
        output = io.StringIO()
        self.assertIsNone(consume_event(artifact, self.request, io.StringIO(), output))
        self.assertIn("result artifact", output.getvalue())
        for key, value in (
            ("run_id", "foreign"),
            ("bytes", True),
            ("bytes", 64 * 1024 * 1024 + 1),
            ("sha256", "BAD"),
            ("path", "/tmp/SECRET_SENTINEL"),
        ):
            modified = dict(artifact)
            modified[key] = value
            with self.subTest(key=key), self.assertRaises(RunError) as error:
                consume_event(modified, self.request, io.StringIO(), io.StringIO())
            self.assertNotIn("SECRET_SENTINEL", str(error.exception))

    def test_run_decision_is_validated_without_changing_output(self):
        decision = {
            "event": "decision",
            "decision": "deny",
            "rule_id": "deny-policy",
            "policy_revision": "abc",
            "target": {"kind": "here"},
            "limits": {},
        }
        self.assertIsNone(self.event(decision))
        self.assertEqual(self.stdout.getvalue(), b"")
        self.assertEqual(self.stderr.getvalue(), b"")

    def test_decision_cannot_retarget_remote_work_locally(self):
        self.request["target"] = {"kind": "remote", "machine_id": "dev-worker"}
        decision = {
            "event": "decision",
            "decision": "allow",
            "rule_id": "allow-local",
            "policy_revision": "abc",
            "target": {"kind": "here"},
            "limits": {},
        }
        with self.assertRaises(RunError):
            self.event(decision)

    def test_explanation_requires_valid_decision(self):
        self.request["operation"] = "explain"
        output = io.StringIO()
        decision = {
            "event": "decision",
            "decision": "allow",
            "rule_id": "allow-local",
            "policy_revision": "abc",
            "target": {"kind": "here"},
            "limits": {"cpu": 1},
        }
        self.assertIsNone(consume_event(decision, self.request, output, io.StringIO()))
        self.assertEqual(json.loads(output.getvalue()), decision)


class SocketIntegrationTest(unittest.TestCase):
    def exchange(self, server, input_bytes=b"", *, explain=False):
        client, peer = socket.socketpair()
        client.setblocking(False)
        request = make_request(
            parse_run(("--here", "cat")), Path("/workspace"), run_id="socket-run"
        )
        if explain:
            request["operation"] = "explain"
        failures = []

        def serve():
            try:
                with peer:
                    server(peer, request)
            except BaseException as error:
                failures.append(error)

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        stdout = io.BytesIO()
        stderr = io.BytesIO()
        try:
            with client:
                status = stream_request(
                    client, request, io.BytesIO(input_bytes), stdout, stderr
                )
        finally:
            thread.join(5)
        self.assertFalse(thread.is_alive(), "client left a blocked server")
        if failures:
            raise failures[0]
        return status, stdout.getvalue(), stderr.getvalue()

    def accept(self, channel, request):
        for event in (
            {
                "event": "decision",
                "decision": "allow",
                "rule_id": "allow-local",
                "policy_revision": "fixture",
                "target": request["target"],
                "limits": {},
            },
            {"event": "accepted", "run_id": request["run_id"]},
        ):
            channel.sendall(json.dumps(event).encode() + b"\n")

    def test_missing_or_out_of_order_admission_never_succeeds(self):
        decision = {
            "event": "decision",
            "decision": "allow",
            "rule_id": "allow-local",
            "policy_revision": "fixture",
            "target": {"kind": "here"},
            "limits": {},
        }
        denied = dict(decision)
        denied["decision"] = "deny"
        accepted = {"event": "accepted", "run_id": "socket-run"}
        exited = {"event": "exit", "code": 0, "run_id": "socket-run"}
        output = {"event": "stdout", "data": "aGVsbG8="}
        cases = (
            [exited],
            [output, exited],
            [accepted, exited],
            [decision, exited],
            [denied, exited],
            [decision, accepted, accepted, exited],
            [decision, accepted, decision, exited],
        )
        for frames in cases:

            def server(channel, request):
                stream = channel.makefile("rb")
                stream.readline()
                for event in frames:
                    channel.sendall(json.dumps(event).encode() + b"\n")
                while channel.recv(65536):
                    pass

            with self.subTest(frames=frames), self.assertRaises(RunError):
                self.exchange(server)

    def test_real_fragmented_frames_and_bidirectional_backpressure(self):
        content = bytes(range(256)) * 1000

        def server(channel, request):
            stream = channel.makefile("rb")
            self.assertEqual(json.loads(stream.readline()), request)
            self.accept(channel, request)
            observed = bytearray()
            while True:
                event = json.loads(stream.readline())
                if event["event"] == "stdin_eof":
                    break
                chunk = base64.b64decode(event["data"])
                observed.extend(chunk)
                frame = (
                    json.dumps({"event": "stdout", "data": event["data"]}).encode()
                    + b"\n"
                )
                for index in range(0, len(frame), 97):
                    channel.sendall(frame[index : index + 97])
            self.assertEqual(observed, content)
            channel.sendall(b'{"event":"stderr","data":"d2FybmluZw=="}\n')
            channel.sendall(b'{"event":"exit","code":23,"run_id":"socket-run"}\n')

        status, stdout, stderr = self.exchange(server, content)
        self.assertEqual(status, 23)
        self.assertEqual(stdout, content)
        self.assertEqual(stderr, b"warning")

    def test_explanation_sends_no_stdin(self):
        def server(channel, request):
            stream = channel.makefile("rb")
            self.assertEqual(json.loads(stream.readline()), request)
            channel.settimeout(0.1)
            with self.assertRaises(TimeoutError):
                channel.recv(1)
            channel.sendall(
                json.dumps(
                    {
                        "event": "decision",
                        "decision": "deny",
                        "rule_id": "deny-policy",
                        "policy_revision": "fixture",
                        "target": request["target"],
                        "limits": {},
                    }
                ).encode()
                + b"\n"
            )
            channel.sendall(b'{"event":"exit","code":126,"run_id":"socket-run"}\n')

        self.assertEqual(
            self.exchange(server, b"SECRET_SENTINEL", explain=True)[0], 126
        )

    def test_disconnect_is_ambiguous_not_success(self):
        def server(channel, request):
            channel.recv(65536)

        with self.assertRaises(RunError):
            self.exchange(server)

    def test_oversized_event_is_refused(self):
        def server(channel, request):
            channel.recv(65536)
            try:
                channel.sendall(b"x" * (MAX_FRAME_BYTES + 1))
            except BrokenPipeError:
                pass

        with self.assertRaises(RunError):
            self.exchange(server)

    def test_signal_becomes_cancellation_frame(self):
        def server(channel, request):
            stream = channel.makefile("rb")
            self.assertEqual(json.loads(stream.readline()), request)
            self.accept(channel, request)
            os.kill(os.getpid(), signal.SIGINT)
            while True:
                event = json.loads(stream.readline())
                if event["event"] == "cancel":
                    self.assertEqual(event["signal"], "INT")
                    break
            channel.sendall(b'{"event":"exit","code":130,"run_id":"socket-run"}\n')

        self.assertEqual(self.exchange(server)[0], 130)


@unittest.skipUnless(hasattr(socket, "SO_PEERCRED"), "Linux peer credentials required")
class BrokerIdentityTest(unittest.TestCase):
    def metadata(self, path):
        mode = stat.S_IFDIR | 0o755
        if path.name == "broker.sock":
            mode = stat.S_IFSOCK | 0o660
        return SimpleNamespace(st_mode=mode, st_uid=0)

    def test_writable_or_symlink_parent_is_refused(self):
        for mode in (stat.S_IFDIR | 0o777, stat.S_IFLNK | 0o777):
            with patch("x.run.sys.platform", "linux"), patch(
                "x.run.Path.lstat", return_value=SimpleNamespace(st_mode=mode, st_uid=0)
            ), self.assertRaises(RunError):
                connect_broker()

    def test_world_connectable_root_socket_still_authenticates_root_peer(self):
        import struct

        fake = SimpleNamespace(
            settimeout=lambda _value: None,
            connect=lambda _path: None,
            close=lambda: None,
            setblocking=lambda _value: None,
            getsockopt=lambda *_args: struct.pack("3i", 100, 0, 0),
        )

        def metadata(path):
            info = self.metadata(path)
            if path.name == "broker.sock":
                info.st_mode = stat.S_IFSOCK | 0o666
            return info

        with patch("x.run.sys.platform", "linux"), patch(
            "x.run.Path.lstat", metadata
        ), patch("x.run.socket.socket", return_value=fake):
            self.assertIs(connect_broker(), fake)

    def test_root_peer_is_required_after_connection(self):
        import struct

        fake = SimpleNamespace(
            settimeout=lambda _value: None,
            connect=lambda _path: None,
            close=lambda: None,
            getsockopt=lambda *_args: struct.pack("3i", 100, 1000, 1000),
        )
        with patch("x.run.sys.platform", "linux"), patch(
            "x.run.Path.lstat", lambda path: self.metadata(path)
        ), patch("x.run.socket.socket", return_value=fake), self.assertRaises(RunError):
            connect_broker()


if __name__ == "__main__":
    unittest.main()
