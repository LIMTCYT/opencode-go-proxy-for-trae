#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
本地反代理：把请求完整双向转发到 OpenCode Go 上游，
在"发包前"注入 OpenCode 要求的会话/请求头，并做模型名别名重写。
接口为 OpenAI 兼容格式（/chat/completions），Trae 或任意 OpenAI 兼容客户端均可接入。

背景：OpenCode Go / Zen API 要求每个请求带
  - x-opencode-session : 会话 ID，同一会话内保持稳定（用于提示词缓存路由）
  - x-opencode-request : 请求 ID，同一会话内递增 (msg_1, msg_2, ...)
  - x-opencode-client  : "cli"
  - x-opencode-project : "global"
缺 x-opencode-session 时上游返回 400:
  "Request is missing x-opencode-session and cannot be routed efficiently"

Trae 注意：自定义模型的"模型 ID"不能与官方预设重名（如 glm-5.3-flash），
否则对话流量会被路由到 Trae 云端预设通道，完全绕过本代理。
请在 Trae 里填带 proxy- 前缀的别名（如 proxy-glm-5.3-flash），
本代理会自动剥掉前缀还原为真实模型名再发给上游。

运行：
  python opencode-go-proxy-for-trae.py
Trae 自定义模型请求地址填: http://127.0.0.1:9355/zen/go/v1（老路由，推荐）
或 /inference/go/v1（新路由）
（按客户端路径原样透传，上游两条路由都还在，填哪个都能用）
推荐用老路由：新路由 /inference/go/v1 会间歇性报 429（Endpoint is unavailable），
同一模型换老路由 /zen/go/v1 即可正常对话。
注意：model ID 与请求地址要配套，填错会 404 —— 老 model ID（如 deepseek-flash）
只在旧路由 /zen/go/v1 提供，新 model ID（如 deepseek-v4.1-flash）只在新路由
/inference/go/v1 提供，请在 Trae 里按模型所属路由填对应 Base URL。
OpenCode Go 官方文档: https://opencode.ai/v2/docs/console/go/

配置（环境变量）：
  PROXY_PORT     监听端口，默认 9355
  UPSTREAM_HOST  上游主机，默认 opencode.ai
  UPSTREAM_PROTO 上游协议，默认 https
  OPENCODE_UA    注入的 User-Agent，默认 opencode/1.18.29 cli
  PID_FILE       pid 文件路径，默认脚本同目录 .opencode-go-proxy-for-trae.pid
  STATS_WINDOW   /stats 汇总使用的内存样本窗口，默认 200

指标观测：
  每次请求记录 TTFT（发包 -> 首个内容 token）、总耗时、输出 token 与吞吐，
  输出一行 [ttft] 日志；GET /stats 返回按模型聚合的 avg/p50/p95 等汇总。
  /stats 为代理本地保留路径，不会转发给上游。
"""

import hashlib
import http.client
import json
import logging
import os
import re
import secrets
import sys
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from logging.handlers import RotatingFileHandler

PROXY_PORT = int(os.environ.get("PROXY_PORT") or 9355)
UPSTREAM_HOST = os.environ.get("UPSTREAM_HOST") or "opencode.ai"
UPSTREAM_PROTO = (os.environ.get("UPSTREAM_PROTO") or "https").lower()
OPENCODE_UA = os.environ.get("OPENCODE_UA") or "opencode/1.18.29 cli"
MODEL_ALIAS_PREFIX = "proxy-"

# 日志文件：默认脚本同目录 data\opencode-go-proxy.log，超过 5MB 自动轮转
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_FILE = os.environ.get("LOG_FILE") or os.path.join(SCRIPT_DIR, "data", "opencode-go-proxy.log")
LOG_MAX_BYTES = 5 * 1024 * 1024
LOG_BACKUPS = 2

# 上游主机可能带端口，如 "example.com:8443"
if ":" in UPSTREAM_HOST:
    _h, _p = UPSTREAM_HOST.rsplit(":", 1)
    UPSTREAM_HOSTNAME, UPSTREAM_PORT = _h, int(_p)
else:
    UPSTREAM_HOSTNAME, UPSTREAM_PORT = UPSTREAM_HOST, (443 if UPSTREAM_PROTO == "https" else 80)

# HTTP 逐跳头不能转发给上游
HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailer", "transfer-encoding", "upgrade",
}

# 会话键 -> { "session_id": str, "req_num": int }（dict 保持插入序，用于裁剪最旧）
sessions = {}
sessions_lock = threading.Lock()
MAX_SESSIONS = 256

# TTFT / 吞吐统计：本地只读接口 /stats + 内存滑动窗口
STATS_PATH = "/stats"
STATS_WINDOW = int(os.environ.get("STATS_WINDOW") or 200)
STATS_RECENT = 20
stats_records = deque(maxlen=STATS_WINDOW)
stats_lock = threading.Lock()

LOG = logging.getLogger("opencode-go-proxy")


def sha256hex(s):
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def random_id(prefix):
    """生成 opencode 风格的会话/请求 ID，如 ses_xxx / msg_xxx（26 个十六进制字符）"""
    return f"{prefix}_{secrets.token_hex(16)[:26]}"


def conversation_key_from_body(raw):
    """
    从请求体推导稳定的"会话键"：system prompt + 第一条 user 消息做哈希。
    同一会话这些内容相对稳定，因此同一会话命中同一个 session ID（缓存才命中）。
    解析失败返回 None（退化为每次随机 session，仅保证不报 400）。
    """
    if not raw:
        return None
    try:
        body = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if not isinstance(body, dict):
        return None
    anchor = ""
    sys_prompt = body.get("system")
    if isinstance(sys_prompt, list):
        anchor += "\n".join(str(x) for x in sys_prompt)
    elif sys_prompt is not None:
        anchor += str(sys_prompt)

    msgs = body.get("messages")
    if isinstance(msgs, list):
        for m in msgs:
            if isinstance(m, dict) and m.get("role") == "user":
                c = m.get("content")
                c = c if isinstance(c, str) else json.dumps(c, ensure_ascii=False)
                anchor += "\n" + c
                break
    if not anchor.strip():
        return None
    return "k_" + sha256hex(anchor)


def normalize_session_id(v):
    """会话 ID 必须是安全字符且长度受限；不合法视为未提供"""
    if not isinstance(v, str):
        return None
    s = v.strip()
    if not s or len(s) > 128:
        return None
    if not re.fullmatch(r"[A-Za-z0-9_.:-]+", s):
        return None
    return s


def extract_explicit_session(raw_body, headers):
    """
    探测客户端携带的"真实会话标识"（优先级从高到低）：
      1. 入站 header：x-opencode-session / x-session-id / x-conversation-id / x-thread-id
      2. 请求体中的会话字段：session_id / sessionId / conversation_id / ...
    返回 (id, source)，未找到返回 None。
    """
    for h in ("x-opencode-session", "x-session-id", "x-conversation-id", "x-thread-id"):
        v = normalize_session_id(headers.get(h))
        if v:
            return v, f"header:{h}"

    if not raw_body:
        return None
    try:
        body = json.loads(raw_body)
    except (ValueError, TypeError):
        return None
    if not isinstance(body, dict):
        return None
    metadata = body.get("metadata") if isinstance(body.get("metadata"), dict) else {}
    probes = [
        body.get("session_id", body.get("sessionId")),
        body.get("conversation_id", body.get("conversationId")),
        body.get("thread_id", body.get("threadId")),
        metadata.get("session_id", metadata.get("sessionId")),
        metadata.get("conversation_id", metadata.get("conversationId")),
    ]
    for p in probes:
        v = normalize_session_id(p)
        if v:
            return v, "body-session-field"
    return None


def trim_sessions():
    """简单防泄漏：超限时删掉最旧的（dict 保持插入序）"""
    while len(sessions) > MAX_SESSIONS:
        oldest = next(iter(sessions))
        del sessions[oldest]


def resolve_session(raw_body, headers):
    """
    解析本次请求应使用哪个会话：
      1. 客户端显式会话标识（header/body）——真实会话绑定，对话内恒定
      2. 内容哈希（system + 首条 user 消息）——回退
      3. 以上都没有——临时随机（仅保证不 400）
    返回 (rec, source)，rec 为共享的可变记录。
    """
    expl = extract_explicit_session(raw_body, headers)
    if expl:
        sid, source = expl
        bucket = "explicit:" + sid
        with sessions_lock:
            rec = sessions.get(bucket)
            if not rec:
                rec = {"session_id": sid, "req_num": 0}
                sessions[bucket] = rec
                trim_sessions()
        return rec, source

    conv_key = conversation_key_from_body(raw_body)
    if conv_key:
        with sessions_lock:
            rec = sessions.get(conv_key)
            if not rec:
                rec = {"session_id": random_id("ses"), "req_num": 0}
                sessions[conv_key] = rec
                trim_sessions()
        return rec, "content-hash"

    return {"session_id": random_id("ses"), "req_num": 0}, "random"


def rewrite_model_alias(raw):
    """
    模型 ID 别名重写：剥掉 "proxy-" 前缀，还原为上游真实模型名。
    （Trae 对话流量按模型 ID 路由，与官方预设重名会绕过本代理，
      故 Trae 里须填别名，由这里还原。）
    """
    if not raw:
        return raw
    try:
        body = json.loads(raw)
    except (ValueError, TypeError):
        return raw
    if isinstance(body, dict) and isinstance(body.get("model"), str) \
            and body["model"].startswith(MODEL_ALIAS_PREFIX):
        body["model"] = body["model"][len(MODEL_ALIAS_PREFIX):]
        LOG.info("[model-alias] %s <- 重写自请求体 model 字段", body["model"])
        return json.dumps(body, ensure_ascii=False)
    return raw


class ColorConsoleFormatter(logging.Formatter):
    """控制台专用格式化器：只给关键提示行加颜色，日志文件仍写纯文本。"""
    KEY_HINTS = ("Trae 请求地址填", "注意：model ID 与请求地址要配套", "OpenCode Go 官方文档")
    COLOR = "\x1b[1;36m"  # 高亮青色
    TTFT_MARK = "[ttft]"
    TTFT_COLOR = "\x1b[1;32m"  # 高亮绿色（指标行，方便一眼扫到）
    RESET = "\x1b[0m"

    def __init__(self, fmt=None, datefmt=None, color=False):
        super().__init__(fmt, datefmt)
        self.color = color

    def format(self, record):
        msg = super().format(record)
        if not self.color:
            return msg
        text = record.getMessage()
        if self.TTFT_MARK in text:
            return self.TTFT_COLOR + msg + self.RESET
        if any(h in text for h in self.KEY_HINTS):
            return self.COLOR + msg + self.RESET
        return msg


def enable_ansi():
    """Windows 下开启 ANSI 转义支持，让控制台能显示颜色（失败则忽略，不影响运行）"""
    if os.name != "nt":
        return
    try:
        import ctypes
        from ctypes import wintypes
        kernel32 = ctypes.windll.kernel32
        kernel32.GetStdHandle.restype = wintypes.HANDLE
        kernel32.GetStdHandle.argtypes = [wintypes.DWORD]
        kernel32.GetConsoleMode.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel32.SetConsoleMode.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
        for std_id in (-11, -12):  # STD_OUTPUT_HANDLE, STD_ERROR_HANDLE
            handle = kernel32.GetStdHandle(std_id)
            mode = wintypes.DWORD()
            if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
                kernel32.SetConsoleMode(handle, mode.value | ENABLE_VIRTUAL_TERMINAL_PROCESSING)
    except Exception:
        pass


def setup_logging():
    """配置 logging：时间戳 + 级别，同时输出到控制台和文件（带大小轮转）"""
    os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
    fmt = "%(asctime)s [%(levelname)s] %(message)s"
    datefmt = "%Y-%m-%d %H:%M:%S"
    console = logging.StreamHandler(sys.stderr)
    try:
        is_tty = sys.stderr.isatty()
    except Exception:
        is_tty = False
    console.setFormatter(ColorConsoleFormatter(fmt, datefmt, color=is_tty))
    rotating = RotatingFileHandler(LOG_FILE, maxBytes=LOG_MAX_BYTES,
                                   backupCount=LOG_BACKUPS, encoding="utf-8")
    rotating.setFormatter(logging.Formatter(fmt, datefmt))
    LOG.setLevel(logging.INFO)
    LOG.addHandler(console)
    LOG.addHandler(rotating)


def request_meta_from_body(raw):
    """从请求体取出 (model, stream 标记)，解析失败返回 (None, None)，用于打点标注"""
    if not raw:
        return None, None
    try:
        body = json.loads(raw)
    except (ValueError, TypeError):
        return None, None
    if not isinstance(body, dict):
        return None, None
    model = body.get("model")
    model = model if isinstance(model, str) and model else None
    return model, bool(body.get("stream"))


class RequestMetrics(object):
    """
    单次请求的指标采集：TTFT（发包 -> 首个内容 token）、总耗时、输出 token、吞吐。
    只做旁路解析（累计缓冲上限为一个 SSE 行），解析失败一律忽略，不影响转发。
    """

    MAX_SSE_BUF = 1 << 20  # 单行异常超长时丢弃，避免内存增长

    def __init__(self, model, stream_hint):
        self.t_start = time.perf_counter()
        self.model = model
        self.stream_hint = stream_hint
        self.first_byte_at = None
        self.first_token_at = None
        self.usage_tokens = None
        self.content_events = 0
        self.is_sse = False
        self.sse_buf = b""
        self.error = None
        self.finished = False

    def on_chunk(self, chunk, is_sse=False):
        """喂入一段上游响应字节；is_sse 为真时按 SSE 解析首内容 token 与 usage"""
        if not chunk:
            return
        now = time.perf_counter()
        if self.first_byte_at is None:
            self.first_byte_at = now
        if not is_sse:
            return
        self.is_sse = True
        self.sse_buf += chunk
        while b"\n" in self.sse_buf:
            line, self.sse_buf = self.sse_buf.split(b"\n", 1)
            self._on_sse_line(line.rstrip(b"\r"), now)
        if len(self.sse_buf) > self.MAX_SSE_BUF:
            self.sse_buf = b""

    def _on_sse_line(self, line, now):
        if not line.startswith(b"data:"):
            return
        payload = line[5:].strip()
        if not payload or payload == b"[DONE]":
            return
        try:
            obj = json.loads(payload.decode("utf-8", errors="replace"))
        except (ValueError, TypeError):
            return
        if not isinstance(obj, dict):
            return

        usage = obj.get("usage")
        if isinstance(usage, dict) and isinstance(usage.get("completion_tokens"), int):
            self.usage_tokens = usage["completion_tokens"]

        choices = obj.get("choices")
        if not isinstance(choices, list) or not choices:
            return
        first = choices[0]
        delta = first.get("delta") if isinstance(first, dict) else None
        if not isinstance(delta, dict):
            return

        has_content = False
        for key in ("content", "reasoning_content"):
            v = delta.get(key)
            if isinstance(v, str) and v:
                has_content = True
        tool_calls = delta.get("tool_calls")
        if isinstance(tool_calls, list) and tool_calls:
            has_content = True

        if has_content:
            if self.first_token_at is None:
                self.first_token_at = now
            self.content_events += 1

    def on_json_body(self, body_bytes):
        """非流式响应体：提取 usage.completion_tokens"""
        if not body_bytes:
            return
        try:
            obj = json.loads(body_bytes.decode("utf-8", errors="replace"))
        except (ValueError, TypeError):
            return
        if not isinstance(obj, dict):
            return
        usage = obj.get("usage")
        if isinstance(usage, dict) and isinstance(usage.get("completion_tokens"), int):
            self.usage_tokens = usage["completion_tokens"]

    def finish(self, status, session_id=None, request_id=None):
        """结算并记录一行 [ttft] 日志；重复调用只生效一次"""
        if self.finished:
            return None
        self.finished = True

        t_end = time.perf_counter()
        anchor = self.first_token_at or self.first_byte_at or t_end
        ttft_ms = max(0.0, (anchor - self.t_start) * 1000.0)
        total_ms = max(0.0, (t_end - self.t_start) * 1000.0)

        tokens = self.usage_tokens
        estimated = False
        if tokens is None and self.content_events:
            tokens = self.content_events
            estimated = True

        tokens_per_s = None
        if tokens and self.first_token_at is not None:
            span = t_end - self.first_token_at
            if span > 0:
                tokens_per_s = tokens / span

        record = {
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
            "model": self.model or "-",
            "stream": 1 if self.is_sse else 0,
            "status": status,
            "ttft_ms": round(ttft_ms, 1),
            "total_ms": round(total_ms, 1),
            "tokens": tokens,
            "tokens_estimated": estimated,
            "tokens_per_s": round(tokens_per_s, 1) if tokens_per_s is not None else None,
            "session": session_id,
            "request": request_id,
            "error": self.error,
        }
        with stats_lock:
            stats_records.append(record)

        tokens_str = ("≈%d" % tokens) if (tokens is not None and estimated) else \
                     ("%d" % tokens if tokens is not None else "-")
        level = logging.WARNING if (self.error or (status or 0) >= 400) else logging.INFO
        LOG.log(level,
                "[ttft] model=%s stream=%d status=%s ttft=%.2fs total=%.2fs tokens=%s tok/s=%s "
                "session=%s request=%s%s",
                record["model"], record["stream"], status or "-",
                ttft_ms / 1000.0, total_ms / 1000.0, tokens_str,
                ("%.1f" % tokens_per_s) if tokens_per_s is not None else "-",
                session_id or "-", request_id or "-",
                (" error=%s" % self.error) if self.error else "")
        return record


def _percentile(sorted_vals, p):
    """最近秩法取分位数（sorted_vals 已升序排序）"""
    if not sorted_vals:
        return None
    idx = int(round((len(sorted_vals) - 1) * p))
    return sorted_vals[idx]


def _agg_series(values):
    """对一组数值（忽略 None）计算 count/avg/p50/p95/min/max"""
    vals = sorted(v for v in values if v is not None)
    if not vals:
        return {"count": 0, "avg": None, "p50": None, "p95": None, "min": None, "max": None}
    return {
        "count": len(vals),
        "avg": round(sum(vals) / len(vals), 1),
        "p50": round(_percentile(vals, 0.50), 1),
        "p95": round(_percentile(vals, 0.95), 1),
        "min": round(vals[0], 1),
        "max": round(vals[-1], 1),
    }


def aggregate_stats(records):
    """汇总统计：整体 + 按模型分组"""
    def bucket(items):
        return {
            "count": len(items),
            "errors": sum(1 for r in items
                          if r.get("error") or (r.get("status") or 0) >= 400),
            "ttft_ms": _agg_series([r.get("ttft_ms") for r in items]),
            "total_ms": _agg_series([r.get("total_ms") for r in items]),
            "tokens_per_s": _agg_series([r.get("tokens_per_s") for r in items]),
        }

    by_model = {}
    for r in records:
        by_model.setdefault(r.get("model") or "-", []).append(r)
    return {
        "count": len(records),
        "overall": bucket(records),
        "by_model": {m: bucket(items) for m, items in by_model.items()},
    }


class ProxyHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def handle_one_request(self):
        # 客户端中途 RST（WinError 10053/10054）时，读写阶段会抛连接类异常，
        # 在此收口，避免 socketserver 打印整段 traceback（ProxyServer.handle_error 为兜底）
        try:
            super().handle_one_request()
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError) as e:
            LOG.debug("[proxy] 连接中断: %s", e)
            self.close_connection = True

    def do_POST(self):
        self._forward("POST")

    def do_GET(self):
        # /stats 为代理本地保留路径，不转发给上游
        if self.path.split("?", 1)[0] == STATS_PATH:
            self._send_stats()
            return
        self._forward("GET")

    def _send_stats(self):
        with stats_lock:
            records = list(stats_records)
        summary = aggregate_stats(records)
        payload = {
            "window": STATS_WINDOW,
            "count": summary["count"],
            "overall": summary["overall"],
            "by_model": summary["by_model"],
            "recent": records[-STATS_RECENT:][::-1],
        }
        data = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        try:
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except OSError:
            pass

    def _forward(self, method):
        # ---- 读入请求体并做模型别名重写 ----
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        raw = self.rfile.read(length) if length > 0 else b""
        raw_body = rewrite_model_alias(raw.decode("utf-8", errors="replace"))
        body_bytes = raw_body.encode("utf-8") if raw_body else None
        model_name, stream_hint = request_meta_from_body(raw_body)

        # ---- 会话解析与请求号递增 ----
        rec, source = resolve_session(raw_body, self.headers)
        with sessions_lock:
            rec["req_num"] += 1
            req_num = rec["req_num"]
        session_id = rec["session_id"]
        LOG.info("[session] %s %s source=%s session=%s request=msg_%d",
                 method, self.path, source, session_id, req_num)

        # ---- 构造上游请求头，转发前注入 ----
        out_headers = {}
        content_type = None
        for k, v in self.headers.items():
            lk = k.lower()
            if lk in HOP_BY_HOP or lk in ("host", "content-length", "content-type"):
                if lk == "content-type":
                    content_type = v
                continue
            out_headers[k] = v
        if content_type:
            out_headers["Content-Type"] = content_type

        # 注入 OpenCode 要求的头
        out_headers["x-opencode-session"] = session_id
        out_headers["x-opencode-request"] = f"msg_{req_num}"
        out_headers["x-opencode-client"] = "cli"
        out_headers["x-opencode-project"] = "global"
        ua = self.headers.get("User-Agent")
        out_headers["User-Agent"] = ua if (ua and "opencode" not in ua) else OPENCODE_UA
        out_headers["Host"] = UPSTREAM_HOST
        if body_bytes:
            out_headers["Content-Length"] = str(len(body_bytes))

        LOG.info("[forward] %s %s | session=%s request=%s client=%s project=%s ua=%s auth=%s",
                 method, self.path,
                 out_headers["x-opencode-session"], out_headers["x-opencode-request"],
                 out_headers["x-opencode-client"], out_headers["x-opencode-project"],
                 out_headers["User-Agent"],
                 "present" if self.headers.get("Authorization") else "MISSING")

        # ---- 发往上游 ----
        if UPSTREAM_PROTO == "https":
            conn = http.client.HTTPSConnection(UPSTREAM_HOSTNAME, UPSTREAM_PORT, timeout=600)
        else:
            conn = http.client.HTTPConnection(UPSTREAM_HOSTNAME, UPSTREAM_PORT, timeout=600)
        # t_start 即为发包时刻
        metrics = RequestMetrics(model_name, stream_hint)
        try:
            conn.request(method, self.path, body=body_bytes, headers=out_headers)
            resp = conn.getresponse()
        except OSError as e:
            metrics.error = "proxy upstream error: %s" % e
            metrics.finish(502, session_id, "msg_%d" % req_num)
            self._send_proxy_error(f"proxy upstream error: {e}")
            conn.close()
            return

        status = 0
        try:
            status = resp.status
            LOG.info("[upstream] %s %s -> %d (session=%s, request=msg_%d)",
                     method, self.path, status, session_id, req_num)

            resp_is_chunked = "chunked" in (resp.getheader("Transfer-Encoding") or "").lower()
            # SSE 判定：上游 Content-Type 声明，或首个数据块以 data: 开头
            sse_mode = "event-stream" in (resp.getheader("Content-Type") or "").lower()
            # 转发上游响应头（跳过逐跳头与自行管理的长度/编码头）
            self.send_response(status)
            for k, v in resp.getheaders():
                lk = k.lower()
                if lk in HOP_BY_HOP or lk in ("content-length", "transfer-encoding"):
                    continue
                self.send_header(k, v)

            if status >= 400:
                # 4xx/5xx：完整读出错误体，打印便于定位，并原样回给客户端
                err_body = resp.read()
                metrics.on_chunk(err_body, is_sse=False)
                snippet = err_body[:800].decode("utf-8", errors="replace")
                LOG.error("[upstream-error] body: %s", snippet)
                self.send_header("Content-Length", str(len(err_body)))
                self.end_headers()
                self.wfile.write(err_body)
                return

            if resp_is_chunked:
                # 上游为流式响应（SSE）：以 chunked 转发给客户端
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                sent = 0
                try:
                    while True:
                        # read1：有多少取多少，避免 read() 凑满 8192 字节才返回，
                        # 否则首包延迟被放大，TTFT 与客户端首字都会失真
                        chunk = resp.read1(8192)
                        if not chunk:
                            break
                        if not sse_mode and chunk.lstrip(b"\r\n \t").startswith(b"data:"):
                            sse_mode = True
                        metrics.on_chunk(chunk, is_sse=sse_mode)
                        sent += len(chunk)
                        self.wfile.write(b"%x\r\n" % len(chunk) + chunk + b"\r\n")
                        self.wfile.flush()
                    self.wfile.write(b"0\r\n\r\n")
                    self.wfile.flush()
                except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
                    # 客户端收完提前断开，属正常现象
                    LOG.info("[proxy] 客户端提前断开（已发送 %d 字节），正常忽略", sent)
            else:
                # 非流式：按上游声明的长度透传
                clen = resp.getheader("Content-Length")
                if clen:
                    self.send_header("Content-Length", clen)
                else:
                    self.send_header("Connection", "close")
                    self.close_connection = True
                self.end_headers()
                collected = []
                try:
                    while True:
                        chunk = resp.read1(8192)
                        if not chunk:
                            break
                        if not sse_mode and chunk.lstrip(b"\r\n \t").startswith(b"data:"):
                            sse_mode = True
                        metrics.on_chunk(chunk, is_sse=sse_mode)
                        collected.append(chunk)
                        self.wfile.write(chunk)
                except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
                    pass
                if not sse_mode and collected:
                    metrics.on_json_body(b"".join(collected))
        finally:
            # 所有退出路径（正常结束、4xx 提前 return、异常）都结算一次指标
            metrics.finish(status, session_id, "msg_%d" % req_num)
            try:
                resp.close()
            except Exception:
                pass
            conn.close()

    def _send_proxy_error(self, message):
        try:
            payload = json.dumps({"error": {"message": message}}).encode("utf-8")
            self.send_response(502)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        except OSError:
            pass

    def log_message(self, fmt, *args):
        LOG.info("[proxy] %s", fmt % args)


class ProxyServer(ThreadingHTTPServer):
    """吞掉客户端主动断开（RST）引发的异常，避免 keep-alive 收尾时刷屏堆栈"""

    daemon_threads = True
    CLIENT_GONE = (ConnectionAbortedError, ConnectionResetError, BrokenPipeError)

    def handle_error(self, request, client_address):
        exc = sys.exc_info()[1]
        if isinstance(exc, self.CLIENT_GONE):
            LOG.debug("[proxy] 客户端已断开连接 %s: %s", client_address, exc)
            return
        super().handle_error(request, client_address)


def write_pid_file():
    pid_path = os.environ.get("PID_FILE") or os.path.join(
        os.path.dirname(os.path.abspath(__file__)), ".opencode-go-proxy-for-trae.pid"
    )
    try:
        with open(pid_path, "w", encoding="utf-8") as f:
            f.write(str(os.getpid()) + "\n")
    except OSError as e:
        LOG.warning("[proxy] 写 pid 文件失败（不影响运行）: %s", e)


def main():
    if hasattr(sys.stderr, "reconfigure"):
        try:
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    enable_ansi()
    setup_logging()

    server = ProxyServer(("127.0.0.1", PROXY_PORT), ProxyHandler)
    write_pid_file()
    LOG.info("[opencode-go-proxy] listening on http://127.0.0.1:%d", PROXY_PORT)
    LOG.info("[opencode-go-proxy] upstream -> %s://%s", UPSTREAM_PROTO, UPSTREAM_HOST)
    LOG.info("[opencode-go-proxy] 日志文件: %s", LOG_FILE)
    LOG.info("[opencode-go-proxy] OpenCode Go 官方文档: https://opencode.ai/v2/docs/console/go/")
    LOG.info("[opencode-go-proxy] 接口为 OpenAI 兼容格式（/chat/completions），"
             "Trae 或任意 OpenAI 兼容客户端均可接入")

    # 空两行再打印关键信息，终端里更醒目
    sys.stderr.write("\n\n")
    sys.stderr.flush()
    LOG.info("[opencode-go-proxy] Trae 请求地址填: http://127.0.0.1:%d/zen/go/v1（老路由，推荐，更稳定）或 "
             "http://127.0.0.1:%d/inference/go/v1（新路由）（按客户端路径原样透传，模型 ID 填 %s真实模型名）\n"
             "注意：model ID 与请求地址要配套，填错会 404 —— "
             "老 model ID（如 deepseek-flash）走 /zen/go/v1，"
             "新 model ID（如 deepseek-v4.1-flash）走 /inference/go/v1\n"
             "提示：新路由 /inference/go/v1 会间歇性报 429（Endpoint is unavailable），优先用老路由 /zen/go/v1",
             PROXY_PORT, PROXY_PORT, MODEL_ALIAS_PREFIX)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        LOG.info("[opencode-go-proxy] 已停止")


if __name__ == "__main__":
    main()
