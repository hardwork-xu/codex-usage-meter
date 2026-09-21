"""Synthetic socket-activation checks; never install or call launchd/Codex."""
from __future__ import annotations

import ctypes
import errno
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import plistlib
import socket
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import service_lifecycle as lifecycle


class ConfigurationTests(unittest.TestCase):
    def test_plist_uses_literal_argv_and_only_ipv4_loopback_socket_demand(self):
        with tempfile.TemporaryDirectory(prefix="meter fixture ") as temp:
            python = Path(temp) / "runtime & python"
            script = Path(temp) / "plugin 'quoted'" / "meter.py"
            folder = Path(temp) / "meter data"
            result = lifecycle.build_launch_agent(python, script, folder)
            self.assertEqual(result["ProgramArguments"], [str(python), "-X", "utf8", str(script),
                "--data-dir", str(folder), "serve", "--port", "50984", "--launchd-socket",
                "MeterHTTP", "--idle-timeout", "600"])
            self.assertEqual(result["Sockets"], {"MeterHTTP": {
                "SockFamily": "IPv4", "SockNodeName": "127.0.0.1", "SockServiceName": 50984,
                "SockType": "stream", "SockProtocol": "TCP", "SockPassive": True}})
            self.assertEqual(result["WorkingDirectory"], str(folder))
            self.assertEqual(result["Umask"], 0o077)
            self.assertEqual(result["ThrottleInterval"], 2)
            self.assertEqual(plistlib.loads(plistlib.dumps(result)), result)
            for key in ("RunAtLoad", "KeepAlive", "WatchPaths", "StartInterval",
                        "StartCalendarInterval", "EnvironmentVariables", "StandardOutPath",
                        "StandardErrorPath", "UserName", "GroupName"):
                self.assertNotIn(key, result)

    def test_plist_rejects_invalid_parameters_before_any_install(self):
        with tempfile.TemporaryDirectory() as temp:
            args = (Path(temp) / "python", Path(temp) / "meter.py", Path(temp))
            for port in (True, 0, 65536, "50984", 1.5):
                with self.subTest(port=port), self.assertRaises(ValueError):
                    lifecycle.build_launch_agent(*args, port=port)
            for idle in (True, 0, 86401, "600", 1.5):
                with self.subTest(idle=idle), self.assertRaises(ValueError):
                    lifecycle.build_launch_agent(*args, idle_seconds=idle)
            for bad in ("relative.py", str(Path(temp) / "bad\nscript"), None):
                with self.subTest(path=bad), self.assertRaises(ValueError):
                    lifecycle.build_launch_agent(args[0], bad, args[2])

    def test_marker_contract_and_other_platform_noop(self):
        marker = {"mode": "launchd-socket", "label": "local.codex-usage-meter",
                  "port": 50984, "socketName": "MeterHTTP"}
        self.assertEqual(lifecycle.activation_marker(), marker)
        with mock.patch.object(lifecycle.sys, "platform", "win32"), \
                mock.patch.object(Path, "lstat", side_effect=AssertionError("no marker lookup")):
            self.assertIsNone(lifecycle.read_service_mode("unused"))

    def test_exact_bounded_marker_or_no_marker(self):
        with tempfile.TemporaryDirectory() as temp, mock.patch.object(lifecycle.sys, "platform", "darwin"):
            self.assertIsNone(lifecycle.read_service_mode(temp))
            marker = lifecycle.activation_marker(54321)
            path = Path(temp) / lifecycle.MODE_FILE
            path.write_text(json.dumps(marker), encoding="utf-8")
            self.assertEqual(lifecycle.read_service_mode(temp), marker)
            for invalid in ("{", "[]", "null", "x" * 4097,
                            json.dumps({**marker, "label": "other-service"}),
                            json.dumps({**marker, "socketName": "other-socket"}),
                            json.dumps({**marker, "mode": "manual"}),
                            json.dumps({**marker, "port": True}),
                            json.dumps({**marker, "unexpected": "field"})):
                with self.subTest(value=invalid[:100]):
                    path.write_text(invalid, encoding="utf-8")
                    with self.assertRaisesRegex(RuntimeError, "未启动其他进程"):
                        lifecycle.read_service_mode(temp)
            path.unlink()
            path.mkdir()
            with self.assertRaises(RuntimeError):
                lifecycle.read_service_mode(temp)

    def test_symlink_marker_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp, mock.patch.object(lifecycle.sys, "platform", "darwin"):
            target = Path(temp) / "target.json"
            target.write_text(json.dumps(lifecycle.activation_marker()), encoding="utf-8")
            path = Path(temp) / lifecycle.MODE_FILE
            try:
                path.symlink_to(target)
            except OSError as exc:
                if getattr(exc, "winerror", None) == 1314:
                    self.skipTest("Windows symlink privilege unavailable")
                raise
            with self.assertRaises(RuntimeError):
                lifecycle.read_service_mode(temp)


def listening_socket():
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    return listener


class NativeActivationTests(unittest.TestCase):
    def test_public_native_abi_frees_descriptor_array_without_closing_sockets(self):
        descriptors = (ctypes.c_int * 1)(123)
        def native(name, array_out, count_out):
            self.assertEqual(name, b"MeterHTTP")
            ctypes.cast(array_out, ctypes.POINTER(ctypes.POINTER(ctypes.c_int)))[0] = \
                ctypes.cast(descriptors, ctypes.POINTER(ctypes.c_int))
            ctypes.cast(count_out, ctypes.POINTER(ctypes.c_size_t))[0] = 1
            return 0
        library = SimpleNamespace(launch_activate_socket=mock.Mock(side_effect=native), free=mock.Mock())
        with mock.patch.object(lifecycle.ctypes, "CDLL", return_value=library) as load, \
                mock.patch.object(lifecycle.socket, "close") as close:
            self.assertEqual(lifecycle._launchd_descriptors("MeterHTTP"), [123])
        load.assert_called_once_with("/usr/lib/libSystem.B.dylib")
        library.free.assert_called_once()
        close.assert_not_called()
        self.assertEqual(library.launch_activate_socket.restype, ctypes.c_int)
        self.assertEqual(library.launch_activate_socket.argtypes,
                         [ctypes.c_char_p, ctypes.POINTER(ctypes.POINTER(ctypes.c_int)),
                          ctypes.POINTER(ctypes.c_size_t)])

    def test_missing_api_and_native_activation_errors_do_not_fall_back_to_binding(self):
        for code in (errno.ENOENT, errno.ESRCH, errno.EALREADY):
            with self.subTest(code=code):
                library = SimpleNamespace(launch_activate_socket=mock.Mock(return_value=code), free=mock.Mock())
                with mock.patch.object(lifecycle.ctypes, "CDLL", return_value=library), \
                        self.assertRaisesRegex(RuntimeError, "错误 " + str(code)):
                    lifecycle._launchd_descriptors("MeterHTTP")
                library.free.assert_not_called()
        with mock.patch.object(lifecycle.ctypes, "CDLL", side_effect=OSError("unavailable")), \
                self.assertRaisesRegex(RuntimeError, "未提供"):
            lifecycle._launchd_descriptors("MeterHTTP")

    def test_non_macos_and_unknown_names_never_call_native_api(self):
        with mock.patch.object(lifecycle, "_launchd_descriptors") as native:
            with mock.patch.object(lifecycle.sys, "platform", "win32"), self.assertRaises(RuntimeError):
                lifecycle.activate_launchd_socket("MeterHTTP", 50984)
            with mock.patch.object(lifecycle.sys, "platform", "darwin"), self.assertRaises(ValueError):
                lifecycle.activate_launchd_socket("Unknown", 50984)
            native.assert_not_called()

    def test_accepts_one_owned_loopback_socket_with_no_inheritance(self):
        original = listening_socket()
        port = original.getsockname()[1]
        descriptor = original.detach()
        with mock.patch.object(lifecycle.sys, "platform", "darwin"), \
                mock.patch.object(lifecycle, "_launchd_descriptors", return_value=[descriptor]):
            listener = lifecycle.activate_launchd_socket("MeterHTTP", port)
        self.addCleanup(listener.close)
        self.assertEqual(listener.fileno(), descriptor)
        self.assertEqual(listener.getsockname(), ("127.0.0.1", port))
        self.assertFalse(listener.get_inheritable())
        self.assertTrue(listener.getblocking())

    def test_wrong_port_closes_owned_descriptor(self):
        original = listening_socket()
        wrong_port = 1 if original.getsockname()[1] != 1 else 2
        descriptor = original.detach()
        with mock.patch.object(lifecycle.sys, "platform", "darwin"), \
                mock.patch.object(lifecycle, "_launchd_descriptors", return_value=[descriptor]), \
                self.assertRaisesRegex(RuntimeError, "地址不一致"):
            lifecycle.activate_launchd_socket("MeterHTTP", wrong_port)
        with self.assertRaises(OSError):
            socket.socket(fileno=descriptor)

    def test_multiple_descriptors_are_all_closed(self):
        listeners = [listening_socket(), listening_socket()]
        descriptors = [item.detach() for item in listeners]
        with mock.patch.object(lifecycle.sys, "platform", "darwin"), \
                mock.patch.object(lifecycle, "_launchd_descriptors", return_value=descriptors), \
                self.assertRaisesRegex(RuntimeError, "多个监听"):
            lifecycle.activate_launchd_socket("MeterHTTP", 50984)
        for descriptor in descriptors:
            with self.assertRaises(OSError):
                socket.socket(fileno=descriptor)

    def test_wildcard_udp_and_connected_socket_are_rejected_without_public_bind(self):
        cases = (
            dict(family=socket.AF_INET, kind=socket.SOCK_STREAM, address=("0.0.0.0", 50984), peer=None),
            dict(family=socket.AF_INET, kind=socket.SOCK_DGRAM, address=("127.0.0.1", 50984), peer=None),
            dict(family=socket.AF_INET6, kind=socket.SOCK_STREAM, address=("::1", 50984), peer=None),
            dict(family=socket.AF_INET, kind=socket.SOCK_STREAM, address=("127.0.0.1", 50984),
                 peer=("127.0.0.1", 12345)),
        )
        for case in cases:
            with self.subTest(case=case):
                listener = mock.Mock(family=case["family"])
                listener.getsockopt.return_value = case["kind"]
                listener.getsockname.return_value = case["address"]
                listener.getpeername.return_value = case["peer"]
                if case["peer"] is None:
                    listener.getpeername.side_effect = OSError(errno.ENOTCONN, "not connected")
                with self.assertRaises(RuntimeError):
                    lifecycle.adopt_http_socket(mock.Mock(), mock.Mock(), listener, 50984)
                listener.close.assert_called_once()


class SyntheticHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        content = b'{"fixture":"socket activation"}'
        self.send_response(200)
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def log_message(self, *args):
        pass


class HTTPAdoptionTests(unittest.TestCase):
    def test_worker_restarts_at_same_listener_without_rebinding_or_dns(self):
        class NoBindServer(ThreadingHTTPServer):
            def server_bind(self):
                raise AssertionError("must use prebound descriptor")
            def server_activate(self):
                raise AssertionError("must use already-listening descriptor")

        keeper = listening_socket()
        self.addCleanup(keeper.close)
        port = keeper.getsockname()[1]
        for _ in range(2):
            worker = keeper.dup()
            with mock.patch.object(socket, "getfqdn", side_effect=AssertionError("no reverse DNS")):
                server = lifecycle.adopt_http_socket(NoBindServer, SyntheticHandler, worker, port)
            self.assertEqual(server.server_name, "127.0.0.1")
            self.assertEqual(server.server_port, port)
            thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
            thread.start()
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=2) as client:
                    client.sendall(b"GET / HTTP/1.0\r\nHost: 127.0.0.1\r\n\r\n")
                    chunks = []
                    while True:
                        part = client.recv(4096)
                        if not part:
                            break
                        chunks.append(part)
                result = b"".join(chunks)
                self.assertIn(b"200 OK", result)
                self.assertTrue(result.endswith(b'{"fixture":"socket activation"}'))
            finally:
                server.shutdown()
                thread.join(2)
                server.server_close()
            self.assertFalse(thread.is_alive())
            self.assertEqual(worker.fileno(), -1)
            self.assertEqual(keeper.getsockname(), ("127.0.0.1", port))

    def test_failed_server_construction_closes_transferred_listener(self):
        listener = listening_socket()
        port = listener.getsockname()[1]
        constructor = mock.Mock(side_effect=RuntimeError("synthetic failure"))
        with self.assertRaisesRegex(RuntimeError, "synthetic failure"):
            lifecycle.adopt_http_socket(constructor, SyntheticHandler, listener, port)
        self.assertEqual(listener.fileno(), -1)


if __name__ == "__main__":
    unittest.main()
