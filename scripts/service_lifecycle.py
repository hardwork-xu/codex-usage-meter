"""Optional macOS socket activation; no installation or launchctl side effects.

launchd owns a numeric loopback listener while the Python worker is absent. The
worker receives its descriptor through Apple's public launch_activate_socket
API. Ordinary Windows/manual service startup never calls that API.
"""
from __future__ import annotations

import contextlib
import ctypes
import errno
import json
import os
from pathlib import Path
import socket
import stat
import sys


LAUNCH_AGENT_LABEL = "local.codex-usage-meter"
SOCKET_NAME = "MeterHTTP"
DEFAULT_PORT = 50984
MODE_FILE = "service-mode.json"


def _port(value):
    if type(value) is not int or not 1 <= value <= 65535:
        raise ValueError("本地面板端口必须是 1 到 65535 的整数")
    return value


def _absolute_path(value):
    try:
        path = Path(value).expanduser()
    except (TypeError, ValueError):
        raise ValueError("按需服务需要有效的绝对路径") from None
    if not path.is_absolute() or any(ord(char) < 32 for char in str(path)):
        raise ValueError("按需服务需要不含控制字符的绝对路径")
    return str(path)


def activation_marker(port=DEFAULT_PORT):
    """Return the local opt-in marker; the installer writes it after bootstrap."""
    return {"mode": "launchd-socket", "label": LAUNCH_AGENT_LABEL,
            "port": _port(port), "socketName": SOCKET_NAME}


def build_launch_agent(python_executable, meter_script, data_dir, *, port=DEFAULT_PORT, idle_seconds=600):
    """Build a plist dictionary without writing files or changing OS settings.

    The caller supplies stable, already-installed paths and creates data_dir.
    Omitting RunAtLoad/KeepAlive means socket demand starts the worker; no login
    launch, timer, background polling process, shell, or privilege change is used.
    """
    port = _port(port)
    if type(idle_seconds) is not int or not 1 <= idle_seconds <= 86400:
        raise ValueError("按需服务的空闲退出时间必须是 1 到 86400 秒的整数")
    python_executable = _absolute_path(python_executable)
    meter_script = _absolute_path(meter_script)
    data_dir = _absolute_path(data_dir)
    return {
        "Label": LAUNCH_AGENT_LABEL,
        "ProgramArguments": [python_executable, "-X", "utf8", meter_script, "--data-dir", data_dir,
                             "serve", "--port", str(port), "--launchd-socket", SOCKET_NAME,
                             "--idle-timeout", str(idle_seconds)],
        "WorkingDirectory": data_dir,
        "Umask": 0o077,
        "ExitTimeOut": 20,
        "ThrottleInterval": 2,
        "Sockets": {SOCKET_NAME: {"SockFamily": "IPv4", "SockNodeName": "127.0.0.1",
                                  "SockServiceName": port, "SockType": "stream",
                                  "SockProtocol": "TCP", "SockPassive": True}},
    }


def read_service_mode(folder):
    """Read only the exact bounded opt-in marker; malformed markers fail closed.

    Other platforms and absent markers use the existing manual service path.
    A bad marker must not silently spawn a competing server on the saved port.
    """
    if sys.platform != "darwin":
        return None
    path = Path(folder) / MODE_FILE
    try:
        original = path.lstat()
    except FileNotFoundError:
        return None
    except OSError:
        raise RuntimeError("无法读取本地按需服务配置") from None
    if not stat.S_ISREG(original.st_mode) or original.st_size > 4096:
        raise RuntimeError("本地按需服务配置无效，未启动其他进程")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        descriptor = os.open(path, flags)
        with os.fdopen(descriptor, "rb") as stream:
            opened = os.fstat(stream.fileno())
            if (not stat.S_ISREG(opened.st_mode) or
                    (opened.st_dev, opened.st_ino) != (original.st_dev, original.st_ino)):
                raise ValueError("changed marker")
            raw = stream.read(4097)
            if len(raw) > 4096:
                raise ValueError("oversized marker")
            value = json.loads(raw)
        if (not isinstance(value, dict) or set(value) != {"mode", "label", "port", "socketName"} or
                value != activation_marker(value.get("port"))):
            raise ValueError("invalid marker")
        return value
    except (OSError, ValueError, TypeError, UnicodeError):
        raise RuntimeError("本地按需服务配置无效，未启动其他进程") from None


def _launchd_descriptors(name):
    # Public SDK declaration: int launch_activate_socket(const char *, int **,
    # size_t *). The array must be freed; ownership of each descriptor is ours.
    try:
        library = ctypes.CDLL("/usr/lib/libSystem.B.dylib")
        activate = library.launch_activate_socket
        release_array = library.free
    except (OSError, AttributeError):
        raise RuntimeError("当前 macOS 未提供按需监听接口") from None
    activate.argtypes = [ctypes.c_char_p, ctypes.POINTER(ctypes.POINTER(ctypes.c_int)),
                         ctypes.POINTER(ctypes.c_size_t)]
    activate.restype = ctypes.c_int
    release_array.argtypes = [ctypes.c_void_p]
    release_array.restype = None
    descriptors = ctypes.POINTER(ctypes.c_int)()
    count = ctypes.c_size_t()
    result = activate(name.encode("ascii"), ctypes.byref(descriptors), ctypes.byref(count))
    try:
        if result:
            raise RuntimeError("无法接收系统按需监听端口，请检查该服务是否通过 launchd 启动（错误 " + str(result) + "）")
        if not descriptors or count.value == 0:
            raise RuntimeError("系统未返回本地面板监听端口")
        return [descriptors[index] for index in range(count.value)]
    finally:
        if descriptors:
            release_array(descriptors)


def _validate_listener(listener, expected_port):
    expected_port = _port(expected_port)
    if listener.family != socket.AF_INET or listener.getsockopt(socket.SOL_SOCKET, socket.SO_TYPE) != socket.SOCK_STREAM:
        raise RuntimeError("按需面板只接受 IPv4 TCP 监听端口")
    address = listener.getsockname()
    if address != ("127.0.0.1", expected_port):
        raise RuntimeError("按需监听地址与保存的本地面板地址不一致")
    try:
        listener.getpeername()
    except OSError as exc:
        if exc.errno not in (errno.ENOTCONN, 10057):
            raise RuntimeError("无法核实本地面板监听端口") from None
    else:
        raise RuntimeError("按需面板需要监听端口，不能接管已连接的请求")
    # No SO_ACCEPTCONN query: it is not consistently supported by macOS sockets.
    # launchd's SockPassive=true creates/listens to this socket before activation.
    return address


def activate_launchd_socket(name, expected_port):
    """Receive and own exactly one validated listener, or close all received FDs."""
    if sys.platform != "darwin":
        raise RuntimeError("当前系统不使用 macOS 按需监听；请使用普通服务启动方式")
    if name != SOCKET_NAME:
        raise ValueError("按需监听名称无效")
    _port(expected_port)
    descriptors = _launchd_descriptors(name)
    if len(descriptors) != 1:
        for descriptor in set(descriptors):
            with contextlib.suppress(OSError):
                socket.close(descriptor)
        raise RuntimeError("系统返回了多个监听端口，未接管其他地址")
    descriptor = descriptors[0]
    try:
        listener = socket.socket(fileno=descriptor)
    except OSError:
        with contextlib.suppress(OSError):
            socket.close(descriptor)
        raise RuntimeError("系统返回的本地监听端口无效") from None
    try:
        _validate_listener(listener, expected_port)
        listener.set_inheritable(False)
        listener.setblocking(True)
        return listener
    except BaseException:
        listener.close()
        raise


def adopt_http_socket(server_class, handler, listener, expected_port):
    """Transfer listener ownership to an HTTPServer without binding or listening.

    Ownership passes on entry, including failure. server_close() later closes
    only this worker's descriptor; launchd keeps its independent listening copy.
    Existing HTTP handlers retain their Host/Origin/CSRF checks.
    """
    server = None
    try:
        address = _validate_listener(listener, expected_port)
        server = server_class(address, handler, bind_and_activate=False)
        # TCPServer constructs an unused socket even when binding is disabled.
        server.socket.close()
        server.socket = listener
        listener.set_inheritable(False)
        listener.setblocking(True)
        server.server_address = address
        server.server_name, server.server_port = address
        return server
    except BaseException:
        if server is not None:
            server.server_close()
        listener.close()
        raise


__all__ = ["LAUNCH_AGENT_LABEL", "SOCKET_NAME", "DEFAULT_PORT", "MODE_FILE", "activation_marker",
           "build_launch_agent", "read_service_mode", "activate_launchd_socket", "adopt_http_socket"]
