#!/usr/bin/env python3
"""Local-only Codex usage meter. Standard-library runtime; no model calls."""
from __future__ import annotations

import argparse
import contextlib
from copy import deepcopy
import decimal
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib import request, error, parse

from usage_log import read_usage_log
from pricing import estimate_turn
from conversations import fetch_conversation_titles, normalize_title, display_ids
from platform_support import default_data_dir, codex_command, file_lock, is_windows, subprocess_options

ROOT = Path(__file__).resolve().parent.parent
EXCHANGE_RATES = {"CNY": "6.70842351", "USD": "1", "HKD": "7.84339018"}
CURRENCIES = {"CNY": ("人民币", "¥"), "USD": ("美元", "$"), "HKD": ("港元", "HK$")}
FX_REFERENCE = {"sourceUrl": "https://www.ecb.europa.eu/stats/policy_and_exchange_rates/euro_reference_exchange_rates/html/index.en.html",
                "date": "2026-09-14", "label": "欧洲央行参考汇率"}
DEFAULT_SETTINGS = {"currencyName": "美元", "currencySymbol": "$", "ratePerMillion": None,
                    "pricingMode": "official", "usdPerCredit": "0.04", "currencyPerUsd": "1", "speedMode": "standard",
                    "currencyCode": "USD", "exchangeRates": EXCHANGE_RATES}
EVENTS = {"SessionStart", "UserPromptSubmit", "Stop", "Interrupt", "SubagentStop", "SubagentStart"}


def data_dir(value=None):
    base = value or os.environ.get("PLUGIN_DATA") or os.environ.get("METER_DATA_DIR")
    return Path(base).expanduser().resolve() if base else default_data_dir()


def read_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_name(path.name + "." + secrets.token_hex(6) + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            json.dump(value, out, ensure_ascii=False, indent=2)
            out.write("\n")
        # Windows may briefly deny replacement while a reader has the old file open.
        for attempt in range(11):
            try:
                os.replace(tmp, path)
                break
            except PermissionError:
                if not is_windows() or attempt == 10:
                    raise
                time.sleep(0.05)
    finally:
        with contextlib.suppress(OSError):
            tmp.unlink(missing_ok=True)


@contextlib.contextmanager
def locked(folder, name="registry"):
    with file_lock(folder / (name + ".lock")):
        yield


def register(folder, path, thread_id):
    """Only the exact caller-provided log. No discovery or recursive scanning."""
    path = Path(path).expanduser().absolute()
    result = read_usage_log(path, thread_id)
    with locked(folder):
        records = read_json(folder / "registry.json", {})
        if not isinstance(records, dict):
            raise ValueError("本地登记记录格式无效，请检查数据目录")
        if thread_id not in records and len(records) >= 100:
            raise ValueError("已达到 100 个任务的本地记录上限")
        previous = records.get(thread_id)
        previous = previous if isinstance(previous, dict) else {}
        records[thread_id] = {**previous, "path": str(path), "registeredAt": previous.get("registeredAt", time.time())}
        write_json(folder / "registry.json", records)
    return {"threadId": thread_id, "turnCount": len(result["turns"])}


def set_conversation_label(folder, value):
    if not isinstance(value, dict) or set(value) != {"threadId", "title"}:
        raise ValueError("对话备注字段无效")
    thread_id, raw_title = value["threadId"], value["title"]
    if not isinstance(thread_id, str) or not isinstance(raw_title, str) or len(raw_title) > 240:
        raise ValueError("对话编号或备注无效")
    title = normalize_title(raw_title)
    if raw_title.strip() and not title:
        raise ValueError("请输入有效的对话备注")
    with locked(folder):
        records = read_json(folder / "registry.json", {})
        if not isinstance(records, dict) or thread_id not in records or not isinstance(records[thread_id], dict):
            raise ValueError("该对话尚未登记")
        if title:
            records[thread_id]["alias"] = title
        else:
            records[thread_id].pop("alias", None)
        write_json(folder / "registry.json", records)
    return {"ok": True}


def conversation_catalog(records, turns, titles):
    """Attach names, collision-free short IDs and per-conversation turn numbers."""
    ids = [key for key, value in records.items() if isinstance(key, str) and isinstance(value, dict)]
    ids.sort(key=lambda key: (records[key].get("registeredAt")
                             if isinstance(records[key].get("registeredAt"), (int, float)) else 0, key))
    tags = display_ids(ids)
    catalog = []
    for index, thread_id in enumerate(ids, 1):
        record = records[thread_id]
        alias = normalize_title(record.get("alias"))
        official = normalize_title(titles.get(thread_id))
        title = alias or official or "对话 " + str(index)
        items = sorted((turn for turn in turns if turn.get("threadId") == thread_id),
                       key=lambda item: (item.get("startedAt") or 0, item.get("id") or ""))
        for number, turn in enumerate(items, 1):
            turn.update(conversationTitle=title, conversationDisplayId=tags[thread_id], turnNumber=number)
        catalog.append({"id": thread_id, "title": title, "displayId": tags[thread_id],
                        "titleSource": "custom" if alias else "official" if official else "fallback",
                        "activeTurnCount": sum(turn.get("status") == "running" for turn in items),
                        "turnCount": len(items),
                        "lastActivityAt": max((turn.get("endedAt") or turn.get("startedAt") or 0 for turn in items), default=0)})
    catalog.sort(key=lambda row: (bool(row["activeTurnCount"]), row["lastActivityAt"]), reverse=True)
    return catalog


def codex_binary():
    command = codex_command()
    if len(command) != 1:
        raise RuntimeError("此 Codex 安装需要通过完整命令启动")
    return command[0]


def fetch_quota():
    """Use documented quota RPC over bounded, cross-platform UTF-8 pipes."""
    from rpc_transport import JsonRpcProcess
    try:
        with JsonRpcProcess([*codex_command(), "app-server", "--stdio"], timeout=20) as rpc:
            rpc.send({"id": 1, "method": "initialize", "params": {
                "clientInfo": {"name": "codex_usage_meter", "version": "0.5.0"}}})
            initialized = False
            for response in rpc.responses():
                request_id = response.get("id")
                if type(request_id) is not int:
                    continue
                if not initialized and request_id == 1:
                    if "error" in response or not isinstance(response.get("result"), dict):
                        raise RuntimeError("Codex 初始化失败，请检查登录状态")
                    initialized = True
                    rpc.send({"method": "initialized"})
                    rpc.send({"id": 2, "method": "account/rateLimits/read"})
                elif initialized and request_id == 2:
                    if "error" in response:
                        raise RuntimeError("Codex 暂未提供额度，请检查登录状态后重试")
                    return normalize_quota(response.get("result", {}))
    except TimeoutError:
        raise RuntimeError("额度查询超时，请稍后重试") from None
    except (OSError, subprocess.SubprocessError):
        raise RuntimeError("Codex 用量连接暂不可用，请检查本地运行环境") from None
    raise RuntimeError("Codex 用量连接已关闭")


def normalize_quota(value):
    if not isinstance(value, dict):
        raise RuntimeError("Codex 未返回有效的额度数据")
    buckets = value.get("rateLimitsByLimitId")
    if not isinstance(buckets, dict) or not buckets:
        old = value.get("rateLimits")
        buckets = {old.get("limitId") or "codex": old} if isinstance(old, dict) else {}
    output = []
    for key, bucket in sorted(buckets.items(), key=lambda item: (item[0] != "codex", item[0])):
        if not isinstance(bucket, dict):
            continue
        windows = []
        for name in ("primary", "secondary"):
            window = bucket.get(name)
            if not isinstance(window, dict):
                continue
            used = window.get("usedPercent")
            if isinstance(used, bool) or not isinstance(used, (int, float)) or not 0 <= used <= 100:
                continue
            windows.append({"windowMinutes": window.get("windowDurationMins"),
                            "usedPercent": used, "remainingPercent": 100 - used,
                            "resetsAt": window.get("resetsAt")})
        output.append({"id": key, "label": bucket.get("limitName") or ("Codex 主额度" if key == "codex" else key), "windows": windows})
    return {"updatedAt": time.time(), "error": None, "buckets": output}


def positive_conversion(raw):
    if isinstance(raw, bool) or not isinstance(raw, (str, int, float)) or len(str(raw)) > 30:
        raise ValueError("Credits 单价或货币换算比例无效")
    try:
        rate = decimal.Decimal(str(raw))
    except decimal.InvalidOperation as exc:
        raise ValueError("Credits 单价或货币换算比例无效") from exc
    if not rate.is_finite() or rate < decimal.Decimal("0.000000000001") or rate > 1_000_000_000:
        raise ValueError("Credits 单价和货币比例必须大于零，范围为 0.000000000001 到 10 亿")
    return str(rate)


def validate_settings(value):
    if not isinstance(value, dict) or set(value) - set(DEFAULT_SETTINGS):
        raise ValueError("设置字段无效")
    out = {}
    for key, choices in (("pricingMode", ("official", "custom")), ("speedMode", ("auto", "standard", "fast"))):
        selected = value.get(key, DEFAULT_SETTINGS[key])
        if selected not in choices:
            raise ValueError("计价方式或速度选项无效")
        out[key] = selected
    for key, limit in (("currencyName", 24), ("currencySymbol", 8)):
        text = value.get(key, DEFAULT_SETTINGS[key])
        if not isinstance(text, str) or not text.strip() or len(text) > limit or any(ord(c) < 32 for c in text):
            raise ValueError("货币名称或符号无效")
        out[key] = text.strip()
    for key in ("usdPerCredit", "currencyPerUsd"):
        out[key] = positive_conversion(value.get(key, DEFAULT_SETTINGS[key]))
    raw_rates = value.get("exchangeRates", EXCHANGE_RATES)
    if not isinstance(raw_rates, dict) or set(raw_rates) != set(EXCHANGE_RATES):
        raise ValueError("需要人民币、美元和港元三个汇率")
    out["exchangeRates"] = {key: positive_conversion(raw_rates[key]) for key in EXCHANGE_RATES}
    if decimal.Decimal(out["exchangeRates"]["USD"]) != 1:
        raise ValueError("美元基准汇率必须为 1")
    out["exchangeRates"]["USD"] = "1"
    code = value.get("currencyCode")
    if code is None:
        code = "USD" if all(out[key] == DEFAULT_SETTINGS[key] for key in ("currencyName", "currencySymbol", "currencyPerUsd")) else "CUSTOM"
    if not isinstance(code, str) or code not in (*CURRENCIES, "CUSTOM"):
        raise ValueError("货币选项无效")
    # Legacy total-token pricing is denominated in the user's chosen units.
    # Keep that mode custom so a preset cannot silently reinterpret its rate.
    if out["pricingMode"] == "custom":
        code = "CUSTOM"
    out["currencyCode"] = code
    if code in CURRENCIES:
        out["currencyName"], out["currencySymbol"] = CURRENCIES[code]
        out["currencyPerUsd"] = out["exchangeRates"][code]
    raw = value.get("ratePerMillion")
    if raw is None or raw == "":
        out["ratePerMillion"] = None
    else:
        if isinstance(raw, bool) or len(str(raw)) > 30:
            raise ValueError("换算单价无效")
        try:
            rate = decimal.Decimal(str(raw))
        except decimal.InvalidOperation as exc:
            raise ValueError("换算单价无效") from exc
        if not rate.is_finite() or rate < 0 or rate > 1_000_000_000:
            raise ValueError("换算单价应在 0 到 10 亿之间")
        out["ratePerMillion"] = str(rate)
    return out


class Meter:
    def __init__(self, folder):
        self.folder = folder
        self.folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.cache = {}
        self.quota = read_json(folder / "quota.json", {"updatedAt": None, "error": "尚未查询额度", "buckets": []})
        self.refresh_lock = threading.Lock()
        self.last_attempt = 0
        self.last_client = time.time()
        self.csrf = secrets.token_urlsafe(32)
        self.titles = read_json(folder / "titles.json", {})
        if not isinstance(self.titles, dict):
            self.titles = {}
        self.title_lock = threading.Lock()
        self.last_title_attempt = 0

    def refresh_titles(self):
        if not self.title_lock.acquire(blocking=False):
            return
        try:
            if time.time() - self.last_title_attempt < 60:
                return
            self.last_title_attempt = time.time()
            records = read_json(self.folder / "registry.json", {})
            if not isinstance(records, dict) or not records:
                return
            found = fetch_conversation_titles(list(records), codex_command())
            self.titles = {**self.titles, **found}
            write_json(self.folder / "titles.json", self.titles)
        except Exception:
            # Metadata availability does not change token accounting. Keep
            # cached names or stable local fallback names until the next try.
            pass
        finally:
            self.title_lock.release()

    def refresh(self):
        if not self.refresh_lock.acquire(blocking=False):
            return
        try:
            if time.time() - self.last_attempt < 30:
                return
            self.last_attempt = time.time()
            try:
                self.quota = fetch_quota()
                write_json(self.folder / "quota.json", self.quota)
            except Exception as exc:
                self.quota = {**self.quota, "error": str(exc) if isinstance(exc, RuntimeError) else "额度查询暂不可用"}
        finally:
            self.refresh_lock.release()

    def settings(self):
        try:
            saved = read_json(self.folder / "settings.json", DEFAULT_SETTINGS)
            if isinstance(saved, dict) and "pricingMode" not in saved:
                # Preserve intentional old custom pricing; a blank legacy rate
                # has no conversion to migrate, so use the new USD default.
                saved = ({**saved, "pricingMode": "custom"} if saved.get("ratePerMillion") not in (None, "")
                         else dict(DEFAULT_SETTINGS))
            if isinstance(saved, dict) and "currencyCode" not in saved:
                # Migrate legacy meter settings to the bundled non-Fast default.
                # This only updates meter settings, not Codex settings.
                saved = {**saved, "speedMode": "standard"}
            return validate_settings(saved)
        except ValueError:
            return deepcopy(DEFAULT_SETTINGS)

    def snapshot(self):
        self.last_client = time.time()
        records = read_json(self.folder / "registry.json", {})
        turns, errors = [], []
        if not isinstance(records, dict):
            records = {}
            errors.append("本地登记记录格式无效")
        settings = self.settings()
        for thread_id, record in records.items():
            try:
                path = Path(record["path"])
                stat = path.stat()
                stamp = (stat.st_mtime_ns, stat.st_size)
                old = self.cache.get(thread_id)
                if not old or old[0] != stamp:
                    result = read_usage_log(path, thread_id)
                    self.cache[thread_id] = (stamp, result)
                result = self.cache[thread_id][1]
                turns.extend(dict(turn) for turn in result["turns"])
                if result.get("warnings"):
                    errors.extend(result["warnings"])
            except Exception:
                # Do not display stale last-good token totals as a new successful read.
                errors.append("某个已登记任务的记录暂不可读或格式已变化")
        turns.sort(key=lambda turn: turn.get("startedAt") or 0, reverse=True)
        conversations = conversation_catalog(records, turns, self.titles)
        for turn in turns:
            turn["pricing"] = estimate_turn(turn, settings)
            turn["amount"] = turn["pricing"]["amount"]
        status = "partial" if errors else "active" if records else "waiting"
        message = ("；".join(dict.fromkeys(errors)) if errors else
                   "已登记任务的本地记录；每条仅统计该任务自身，子代理单独列出" if records else
                   "等待登记任务。安装后在 Codex 中审阅并信任本插件 Hooks，新问题才会自动登记。")
        return {"monitoring": {"status": status, "message": message, "lastUpdate": time.time() if records and not errors else None},
                "quota": self.quota, "turns": turns[:200], "conversations": conversations, "settings": settings, "csrfToken": self.csrf,
                "fxReference": {**FX_REFERENCE, "customized": any(decimal.Decimal(settings["exchangeRates"][key]) != decimal.Decimal(EXCHANGE_RATES[key]) for key in EXCHANGE_RATES)}}


class Handler(BaseHTTPRequestHandler):
    server_version = "CodexUsageMeter/0.5"

    def log_message(self, *_):
        pass

    def allowed(self, write=False):
        origin = "http://127.0.0.1:" + str(self.server.server_port)
        if self.headers.get("Host") != origin[7:]:
            return False
        if self.headers.get("Origin") not in (None, origin):
            return False
        if self.headers.get("Sec-Fetch-Site") == "cross-site":
            return False
        if write:
            supplied = self.headers.get("X-Meter-Token", "")
            if not supplied.isascii() or not secrets.compare_digest(supplied, self.server.meter.csrf):
                return False
        return True

    def respond(self, value, status=200, kind="application/json; charset=utf-8"):
        data = value if isinstance(value, bytes) else json.dumps(value, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if not self.allowed():
            return self.respond({"error": "拒绝跨来源访问"}, 403)
        if self.path == "/":
            return self.respond((ROOT / "web/index.html").read_bytes(), kind="text/html; charset=utf-8")
        if self.path == "/api/state":
            snapshot = self.server.meter.snapshot()
            if any(row["titleSource"] == "fallback" for row in snapshot["conversations"]) and time.time() - self.server.meter.last_title_attempt >= 60:
                threading.Thread(target=self.server.meter.refresh_titles, daemon=True).start()
            return self.respond(snapshot)
        if self.path == "/health":
            return self.respond({"app": "codex-usage-meter", "version": "0.5.0", "pid": os.getpid()})
        self.respond({"error": "不存在"}, 404)

    def do_POST(self):
        if not self.allowed(write=True):
            return self.respond({"error": "请求校验失败，请刷新面板"}, 403)
        if self.path == "/api/shutdown":
            self.respond({"ok": True})
            self.server.stop_event.set()
            threading.Thread(target=self.server.shutdown, daemon=True).start()
            return
        if self.path == "/api/refresh":
            threading.Thread(target=self.server.meter.refresh, daemon=True).start()
            return self.respond({"ok": True})
        if self.path not in ("/api/settings", "/api/conversation-label"):
            return self.respond({"error": "不存在"}, 404)
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if size < 1 or size > 4096 or self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                raise ValueError("请求格式无效")
            value = json.loads(self.rfile.read(size))
            if self.path == "/api/conversation-label":
                return self.respond(set_conversation_label(self.server.meter.folder, value))
            settings = validate_settings(value)
            write_json(self.server.meter.folder / "settings.json", settings)
            self.respond({"ok": True, "settings": settings})
        except (ValueError, UnicodeError) as exc:
            self.respond({"error": str(exc)}, 400)


class MeterHTTPServer(ThreadingHTTPServer):
    def server_bind(self):
        if is_windows():
            # Windows SO_REUSEADDR can let a second process share the same port.
            self.allow_reuse_address = False
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def serve(folder, port=0):
    with locked(folder, "service"):
        if port == 0:
            endpoint = read_json(folder / "endpoint.json", {})
            url = endpoint.get("url", "") if isinstance(endpoint, dict) else ""
            if local_url(url):
                port = parse.urlsplit(url).port
        meter = Meter(folder)
        try:
            server = MeterHTTPServer(("127.0.0.1", port), Handler)
        except OSError as exc:
            if port:
                raise RuntimeError("本地面板无法使用端口 " + str(port) + "，未更换地址") from exc
            raise
        server.meter = meter
        server.daemon_threads = True
        server.stop_event = threading.Event()
        endpoint = {"url": "http://127.0.0.1:" + str(server.server_port) + "/", "pid": os.getpid()}
        write_json(folder / "endpoint.json", endpoint)
        def refresh_loop():
            while not server.stop_event.is_set():
                if time.time() - meter.last_client < 120:
                    meter.refresh()
                    if time.time() - meter.last_title_attempt >= 300:
                        meter.refresh_titles()
                server.stop_event.wait(60)
        threading.Thread(target=refresh_loop, daemon=True).start()
        print(json.dumps(endpoint), flush=True)
        try:
            server.serve_forever()
        finally:
            server.stop_event.set()
            server.server_close()


def local_url(url):
    try:
        parsed = parse.urlsplit(url)
        return (parsed.scheme == "http" and parsed.hostname == "127.0.0.1" and
                parsed.username is None and parsed.password is None and
                parsed.port is not None and 0 < parsed.port <= 65535 and
                not parsed.query and not parsed.fragment and parsed.path in ("/", "/health", "/api/state"))
    except (ValueError, TypeError):
        return False


class NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        raise RuntimeError("本地面板不允许重定向")


def local_get(url):
    if not local_url(url):
        raise RuntimeError("本地面板地址无效")
    opener = request.build_opener(request.ProxyHandler({}), NoRedirect())
    with opener.open(url, timeout=2) as response:
        return json.load(response)


def local_shutdown(url, token):
    if (not local_url(url) or parse.urlsplit(url).path != "/" or
            not isinstance(token, str) or not token or not token.isascii() or len(token) > 256):
        raise RuntimeError("本地面板停止请求无效")
    opener = request.build_opener(request.ProxyHandler({}), NoRedirect())
    message = request.Request(url + "api/shutdown", data=b"{}", method="POST", headers={
        "Content-Type": "application/json", "Origin": url.rstrip("/"), "X-Meter-Token": token})
    with opener.open(message, timeout=2) as response:
        result = json.load(response)
    if not isinstance(result, dict) or result.get("ok") is not True:
        raise RuntimeError("本地面板未确认停止请求")


def stop_service(folder):
    """Ask only the authenticated meter at its saved local address to shut down."""
    endpoint = read_json(folder / "endpoint.json", {})
    url = endpoint.get("url", "") if isinstance(endpoint, dict) else ""
    if not local_url(url) or parse.urlsplit(url).path != "/":
        return False
    try:
        health = local_get(url + "health")
    except OSError:
        return False
    if (not isinstance(health, dict) or health.get("app") != "codex-usage-meter" or
            health.get("pid") != endpoint.get("pid")):
        return False
    state = local_get(url + "api/state")
    if not isinstance(state, dict):
        raise RuntimeError("本地面板状态无效，未停止任何进程")
    local_shutdown(url, state.get("csrfToken"))
    return True


def ensure_service(folder):
    with locked(folder, "launch"):
        endpoint = read_json(folder / "endpoint.json", {})
        url = endpoint.get("url", "") if isinstance(endpoint, dict) else ""
        if local_url(url):
            try:
                if local_get(url + "health").get("app") == "codex-usage-meter":
                    return url
            except Exception:
                pass
        subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--data-dir", str(folder), "serve"],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         **subprocess_options(background=True))
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            endpoint = read_json(folder / "endpoint.json", {})
            url = endpoint.get("url", "") if isinstance(endpoint, dict) else ""
            try:
                if local_url(url) and local_get(url + "health").get("app") == "codex-usage-meter":
                    return url
            except Exception:
                pass
            time.sleep(0.1)
        raise RuntimeError("本地面板未能启动")


def handle_hook(folder, data):
    if not isinstance(data, dict) or data.get("hook_event_name") not in EVENTS:
        return
    # Prompt bodies, tool arguments and agent messages are deliberately ignored.
    path, thread_id = data.get("transcript_path"), data.get("session_id")
    if isinstance(path, str) and isinstance(thread_id, str):
        register(folder, path, thread_id)
    if data.get("hook_event_name") == "SubagentStop":
        child_path, child_id = data.get("agent_transcript_path"), data.get("agent_id")
        if isinstance(child_path, str) and isinstance(child_id, str):
            register(folder, child_path, child_id)


def mcp(folder):
    tools = [
        {"name": "open_usage_meter", "description": "打开本地 Codex 用量面板，返回可在应用内浏览器打开的地址。", "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False}, "annotations": {"readOnlyHint": True, "openWorldHint": False}},
        {"name": "get_usage_summary", "description": "读取已登记任务的 Token、credits 及金额估算和官方额度快照；金额不是实际扣款，Token 不代表订阅剩余余额。", "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False}, "annotations": {"readOnlyHint": True, "openWorldHint": False}},
    ]
    for line in sys.stdin:
        req = None
        try:
            req = json.loads(line)
            if not isinstance(req, dict):
                raise ValueError("Invalid Request")
            if "id" not in req:
                continue
            method = req.get("method")
            if method == "initialize":
                result = {"protocolVersion": req.get("params", {}).get("protocolVersion", "2024-11-05"), "capabilities": {"tools": {}}, "serverInfo": {"name": "codex-usage-meter", "version": "0.5.0"}}
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": tools}
            elif method == "tools/call":
                name = req.get("params", {}).get("name")
                if name not in {tool["name"] for tool in tools}:
                    raise ValueError("未知工具")
                url = ensure_service(folder)
                value = {"url": url, "message": "在 Codex 应用内浏览器打开此地址"}
                if name == "get_usage_summary":
                    value = local_get(url + "api/state")
                    value.pop("csrfToken", None)
                    value["turns"] = value["turns"][:10]
                result = {"content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False)}]}
            else:
                print(json.dumps({"jsonrpc": "2.0", "id": req["id"], "error": {"code": -32601, "message": "Method not found"}}), flush=True)
                continue
            print(json.dumps({"jsonrpc": "2.0", "id": req["id"], "result": result}, ensure_ascii=False), flush=True)
        except Exception:
            if isinstance(req, dict) and "id" in req:
                print(json.dumps({"jsonrpc": "2.0", "id": req["id"], "error": {"code": -32603, "message": "用量工具暂不可用；请检查本地运行环境"}}), flush=True)
            elif req is None:
                print(json.dumps({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}), flush=True)


def main():
    # MCP and hook JSON are UTF-8 even when Windows redirects a legacy codepage.
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir")
    sub = parser.add_subparsers(dest="command", required=True)
    service = sub.add_parser("serve")
    service.add_argument("--port", type=int, default=0)
    sub.add_parser("open")
    sub.add_parser("mcp")
    sub.add_parser("hook")
    reg = sub.add_parser("register")
    reg.add_argument("--transcript", required=True)
    reg.add_argument("--thread-id", required=True)
    sub.add_parser("summary")
    sub.add_parser("stop")
    args = parser.parse_args()
    folder = data_dir(args.data_dir)
    if args.command == "serve":
        serve(folder, args.port)
    elif args.command == "hook":
        # A meter must never block, continue, or alter the agent's work.
        try:
            payload = sys.stdin.read(2_000_000)
            handle_hook(folder, json.loads(payload))
        except Exception:
            pass
    elif args.command == "register":
        print(json.dumps(register(folder, args.transcript, args.thread_id)))
    elif args.command == "mcp":
        mcp(folder)
    elif args.command == "open":
        print(ensure_service(folder))
    elif args.command == "summary":
        state = local_get(ensure_service(folder) + "api/state")
        state.pop("csrfToken", None)
        print(json.dumps(state, ensure_ascii=False, indent=2))
    elif args.command == "stop":
        print("已请求停止用量面板" if stop_service(folder) else "没有可确认的本地用量服务")


if __name__ == "__main__":
    main()
