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
  UPSTREAM_TIMEOUT
                 单次上游尝试的 socket 超时（秒），默认 120；涵盖建连、TLS 握手、
                 等响应头与每次读数据块，静默超过该值即判定这条连接失败
  HEARTBEAT_AFTER
                 慢路径阈值（秒），默认 30；上游超过该时长仍无响应头时，先回 200 + SSE
                 并周期发心跳吊住客户端（仅流式请求）
  RETRY_AFTER    竞速重试阈值（秒），默认 60；到点另开一条连接重发同一请求，谁先回数据用谁
  HEARTBEAT_INTERVAL
                 慢路径心跳间隔（秒），默认 10
  CONTENT_IDLE_TIMEOUT
                 最后兜底（秒），默认 1800；距上一次内容产出（content /
                 reasoning_content / tool_calls）超过该时长即主动终止请求，0 表示不限制
  RETRY_ON_5XX_MAX
                 上游错误自动重试上限（次），默认 10；仅在尚未向客户端吐出任何数据时生效，
                 覆盖 5xx 与"Endpoint is unavailable"这类被标成 429 的瞬时端点故障，
                 0 表示不重试
  RETRY_429_BACKOFFS
                 429 重试的退避序列（秒），默认 4,8,16,32,64；指数递增，给服务端留恢复窗口
  RETRY_BUDGET   重试总时长预算（秒），默认 60；仅在无法心跳续命的请求（非流式）上生效，
                 累计等待将超过该预算即放弃重试、原样透传真实状态码，0 表示不限制
  PID_FILE       pid 文件路径，默认脚本同目录 .opencode-go-proxy-for-trae.pid
  STATS_WINDOW   /stats 汇总使用的内存样本窗口，默认 200

慢路径（上游迟迟不响应时）：
  上游超过 HEARTBEAT_AFTER 秒仍没回响应头，代理会先向客户端回 200 + SSE，
  每 HEARTBEAT_INTERVAL 秒发一个 SSE 注释心跳（": keepalive"）避免客户端自己超时；
  到 RETRY_AFTER 秒再开第二条连接竞速重发，谁先回数据用谁，另一条关掉。
  代价：进入慢路径后响应头已提交，上游若随后报错只能以 SSE error 事件下发；
  竞速重发可能被上游计费两次（被丢弃的那份）。

最后兜底（防持续扣费）：
  无论走哪条路径，只要距上一次"内容产出"超过 CONTENT_IDLE_TIMEOUT 秒（默认 1800，
  即整整 30 分钟没吐出任何内容 token），代理就主动终止请求并以 SSE error 事件收尾。
  判的是"没产出"，不是"总时长"，所以正常慢慢出字的长任务不会被误伤。

上游错误自动重试：
  上游返回 5xx，或把"端点暂时不可用"标成 429（错误体含 "Endpoint is unavailable"）时，
  只要还没向客户端吐出任何数据，代理就会重发（上限由 RETRY_ON_5XX_MAX 控制，默认 10 次）。
  退避策略按状态码区分：5xx 固定退避 1 秒（瞬时故障，下一次通常就能成功）；
  429 按 RETRY_429_BACKOFFS 指数退避（默认 4/8/16/32/64 秒），避免密集重试加剧服务端压力。
  注意：按默认参数，10 次 429 重试的累计等待约 444 秒（4+8+16+32+64×6），
  远超客户端自身的超时；流式请求靠心跳保活可撑住，非流式请求则由 RETRY_BUDGET 兜住：
  该请求无法心跳续命，累计等待将超过预算（默认 60 秒）时就放弃重试，
  原样透传上游的真实状态码（保留 429/503 语义，而不是拖到客户端超时）。
  退避等待期间不阻塞主循环，流式请求的心跳保活仍在继续，客户端不会因等待而超时。
  真正的限流类 429 与其他 4xx 不重试，避免把限流放大成多倍压力。

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
import queue
import re
import secrets
import socket
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
# 单次上游尝试的 socket 超时（秒）：作用于建连、TLS 握手、发送、等响应头以及每次读数据块。
# 任一次尝试静默超过该值即判定这条连接失败（慢路径下另有竞速重试兜底）。
UPSTREAM_TIMEOUT = float(os.environ.get("UPSTREAM_TIMEOUT") or 120)
# 慢路径阈值（秒）：上游超过 HEARTBEAT_AFTER 仍无响应头时，先回 200 + SSE 并周期发心跳，
# 吊住客户端；到 RETRY_AFTER 再开第二条连接竞速重发，谁先回数据用谁。
HEARTBEAT_AFTER = float(os.environ.get("HEARTBEAT_AFTER") or 30)
RETRY_AFTER = float(os.environ.get("RETRY_AFTER") or 60)
HEARTBEAT_INTERVAL = float(os.environ.get("HEARTBEAT_INTERVAL") or 10)
# 最后兜底（秒）：距上一次"内容产出"（content / reasoning_content / tool_calls）超过该时长
# 即主动终止请求，避免上游一直慢速输出却拿不到结果、白扣 token。0 表示不限制。
CONTENT_IDLE_TIMEOUT = float(os.environ.get("CONTENT_IDLE_TIMEOUT") or 1800)
# 上游错误自动重试上限（次）：只在"还没向客户端吐出任何数据"时生效，此时重试不会产生重复内容。
# 覆盖 5xx，以及上游把"端点暂时不可用"标成 429 的情况（错误体含 Endpoint is unavailable）。
# 0 表示不重试。
RETRY_ON_5XX_MAX = int(os.environ.get("RETRY_ON_5XX_MAX") or 10)
# 5xx 重试前的退避秒数：这类是瞬时网关故障，短退避即可快速恢复
RETRY_ON_5XX_BACKOFF = 1.0
# 429 重试的退避序列（秒）：默认 4/8/16/32/64，指数递增，给服务端留出恢复窗口；
# 用逗号分隔可自定义，超出序列长度后沿用最后一个值
RETRY_429_BACKOFFS = [float(x) for x in
                      (os.environ.get("RETRY_429_BACKOFFS") or "4,8,16,32,64").split(",")
                      if x.strip()]
# 上游把瞬时端点故障标成 429 时的错误体特征，用于区别于真正的限流（限流不重试）
TRANSIENT_ERROR_MARKERS = ("endpoint is unavailable",)
# 重试总时长预算（秒）：仅在"无法用心跳续命"的请求（非流式）上生效。
# 累计等待（自请求开始计时）将超过该预算时就不再重试，直接原样透传上游的真实状态码，
# 避免客户端早已超时后还在白跑重试、白占上游配额。流式请求有心跳续命，不受此限。
# 0 表示不限制。
RETRY_BUDGET = float(os.environ.get("RETRY_BUDGET") or 60)
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
    # 只要不是 200 就算有问题：整行标黄，一眼可见（含 3xx / 4xx / 5xx 及其他 2xx）
    NON_200_MARK = re.compile(r"-> (?!200\b)\d{3}\b"
                              r"|status=(?!200\b)\d{3}\b"
                              r"|HTTP/1\.1\" (?!200\b)\d{3}\b")
    WARN_COLOR = "\x1b[1;33m"  # 高亮黄色
    RESET = "\x1b[0m"

    def __init__(self, fmt=None, datefmt=None, color=False):
        super().__init__(fmt, datefmt)
        self.color = color

    def format(self, record):
        msg = super().format(record)
        if not self.color:
            return msg
        text = record.getMessage()
        if self.NON_200_MARK.search(text):
            return self.WARN_COLOR + msg + self.RESET
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
        self.last_content_at = None
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
            self.last_content_at = now
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

    def content_idle_exceeded(self, now=None):
        """
        距上一次内容 token（content / reasoning_content / tool_calls）是否已超过兜底上限。
        从未产出过内容时以发包时刻起算；CONTENT_IDLE_TIMEOUT <= 0 表示不限制。
        """
        if CONTENT_IDLE_TIMEOUT <= 0:
            return False
        anchor = self.last_content_at if self.last_content_at is not None else self.t_start
        return ((now or time.perf_counter()) - anchor) >= CONTENT_IDLE_TIMEOUT

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


def is_retryable_upstream_status(status, body_text):
    """
    上游错误是否值得原样重发：5xx 一律重试；429 仅在错误体表明是"端点暂时不可用"时重试。
    真限流（如 rate limit exceeded）不重试，避免把限流放大成多倍压力。
    """
    if status >= 500:
        return True
    if status == 429:
        low = (body_text or "").lower()
        return any(m in low for m in TRANSIENT_ERROR_MARKERS)
    return False


def retry_backoff_seconds(status, retry_index):
    """
    第 retry_index 次重试（从 1 开始）前的等待秒数。
    429 按指数退避（默认 4/8/16/32/64 秒），避免密集重试加剧服务端压力；
    5xx 用固定短退避，这类瞬时故障通常下一次就能成功。
    """
    if status == 429 and RETRY_429_BACKOFFS:
        return RETRY_429_BACKOFFS[min(retry_index - 1, len(RETRY_429_BACKOFFS) - 1)]
    return RETRY_ON_5XX_BACKOFF


class UpstreamAttempt(threading.Thread):
    """
    一次上游请求尝试。放在后台线程里跑，结果经队列回传，
    这样主线程可以边等结果边发心跳、并在超时后另开一条连接竞速。
    无论成功失败都必定回传一次结果，主线程不会空等。
    """

    def __init__(self, tag, method, path, body_bytes, headers, result_q):
        super().__init__(daemon=True)
        self.tag = tag
        self.method = method
        self.path = path
        self.body_bytes = body_bytes
        self.headers = headers
        self.result_q = result_q
        self.conn = None

    def run(self):
        try:
            if UPSTREAM_PROTO == "https":
                self.conn = http.client.HTTPSConnection(UPSTREAM_HOSTNAME, UPSTREAM_PORT,
                                                        timeout=UPSTREAM_TIMEOUT)
            else:
                self.conn = http.client.HTTPConnection(UPSTREAM_HOSTNAME, UPSTREAM_PORT,
                                                       timeout=UPSTREAM_TIMEOUT)
            self.conn.request(self.method, self.path,
                              body=self.body_bytes, headers=self.headers)
            resp = self.conn.getresponse()
            self.result_q.put((self, resp, None))
        except socket.timeout:
            self.result_q.put((self, None,
                               "no response in %gs" % UPSTREAM_TIMEOUT))
        except Exception as e:  # 含 OSError/ssl 错误：必须回传，否则主线程会空等
            self.result_q.put((self, None, "upstream error: %s" % e))

    def abandon(self):
        """竞速落败后关掉这条连接；shutdown 可尽快唤醒仍在阻塞读的线程"""
        try:
            if self.conn is not None and self.conn.sock is not None:
                self.conn.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            if self.conn is not None:
                self.conn.close()
        except OSError:
            pass


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

        # ---- 发往上游（含慢路径心跳与竞速重试）----
        # 快路径：上游在 HEARTBEAT_AFTER 内返回响应头，完全按原样透传（含真实错误码）。
        # 慢路径：超时仍无响应头时先回 200 + SSE 并周期发心跳吊住客户端，
        #         到 RETRY_AFTER 另开一条连接竞速重发，谁先回数据用谁。
        metrics = RequestMetrics(model_name, stream_hint)
        result_q = queue.Queue()
        attempts = []
        pending = [0]
        retry_fired = [False]
        retry_used = [0]
        prefetched_err_body = [None]
        # 待执行的上游错误重试：(生效时刻, 状态码, 错误体, 第几次)
        retry_at = [None]
        pending_retry = [None]
        # 只有流式请求才能靠 SSE 注释续命：非流式响应无法在不提交响应头的前提下发心跳
        heartbeat_ok = stream_hint is True
        retry_deadline = metrics.t_start + RETRY_AFTER
        heartbeat_deadline = metrics.t_start + HEARTBEAT_AFTER
        committed = False
        next_heartbeat_at = None

        def start_attempt(tag):
            attempt = UpstreamAttempt(tag, method, self.path, body_bytes, out_headers, result_q)
            attempts.append(attempt)
            pending[0] += 1
            attempt.start()
            LOG.info("[attempt] %s 已发出 (session=%s, request=msg_%d)",
                     tag, session_id, req_num)

        def fire_pending_retry():
            """退避到点：发出这条重试，并清空待办"""
            status, _body, idx = pending_retry[0]
            pending_retry[0] = None
            retry_at[0] = None
            start_attempt("retry%s-%d" % ("429" if status == 429 else "5xx", idx))

        start_attempt("initial")

        winner = None
        resp = None
        err = None
        while pending[0] > 0 or retry_at[0] is not None:
            now = time.perf_counter()
            marks = []
            # 有计划的退避重试时，不再叠加"无响应竞速重试"，避免两条重试互相干扰
            if not retry_fired[0] and retry_at[0] is None:
                marks.append(retry_deadline)
            if heartbeat_ok and not committed:
                marks.append(heartbeat_deadline)
            if next_heartbeat_at is not None:
                marks.append(next_heartbeat_at)
            if retry_at[0] is not None:
                marks.append(retry_at[0])
            future = [m for m in marks if m > now]
            if future:
                wait = min(future) - now
            elif marks:
                wait = 0.0          # 有已到期的定时动作，立即处理
            else:
                wait = HEARTBEAT_INTERVAL

            try:
                attempt, r, e = result_q.get(timeout=max(0.001, wait))
            except queue.Empty:
                now = time.perf_counter()
                if heartbeat_ok and not committed and now >= heartbeat_deadline:
                    self._commit_sse_head()
                    committed = True
                    next_heartbeat_at = now + HEARTBEAT_INTERVAL
                    LOG.info("[slow-path] 上游 %gs 无响应，已提前回 200 并开始心跳 "
                             "(request=msg_%d)", HEARTBEAT_AFTER, req_num)
                if committed and next_heartbeat_at is not None and now >= next_heartbeat_at:
                    if not self._write_chunk(b": keepalive\n\n"):
                        LOG.info("[proxy] 客户端已断开，停止心跳 (request=msg_%d)", req_num)
                        for a in attempts:
                            a.abandon()
                        metrics.error = "client gone during slow path"
                        metrics.finish(200, session_id, "msg_%d" % req_num)
                        return
                    next_heartbeat_at = now + HEARTBEAT_INTERVAL
                if retry_at[0] is not None and now >= retry_at[0]:
                    fire_pending_retry()
                elif retry_at[0] is None and not retry_fired[0] and now >= retry_deadline:
                    retry_fired[0] = True
                    start_attempt("retry")
                continue

            pending[0] -= 1
            if e is None:
                err_body_raw = None
                err_body_text = ""
                # 注意：慢路径下即使已提前回 200（仅响应头 + 心跳注释，未发送任何内容），
                # 依然可以安全重试——结果会并入已提交的 SSE 流下发。
                if (retry_used[0] < RETRY_ON_5XX_MAX
                        and (r.status >= 500 or r.status == 429)):
                    try:
                        err_body_raw = r.read()
                    except Exception:
                        err_body_raw = b""
                    err_body_text = err_body_raw.decode("utf-8", errors="replace")
                if (err_body_raw is not None
                        and is_retryable_upstream_status(r.status, err_body_text)):
                    # 上游瞬时故障（5xx，或把"端点不可用"标成 429）。此时还没向客户端吐过
                    # 任何数据，丢掉这一轮并按退避计划重发即可，客户端完全无感。
                    # 429 用指数退避（默认 4/8/16/32/64 秒），避免密集重试加剧服务端压力。
                    n = retry_used[0] + 1
                    backoff = retry_backoff_seconds(r.status, n)
                    # 预算只在无法心跳续命的请求上生效：否则客户端早已超时，重试是白跑
                    if (not heartbeat_ok and RETRY_BUDGET > 0
                            and (time.perf_counter() - metrics.t_start) + backoff > RETRY_BUDGET):
                        LOG.warning("[attempt] 上游 %d，但累计等待将超过预算 %gs（该请求无法心跳"
                                    "续命），放弃重试并原样透传 (request=msg_%d)",
                                    r.status, RETRY_BUDGET, req_num)
                        metrics.error = "upstream %d (retry budget exhausted)" % r.status
                        prefetched_err_body[0] = err_body_raw
                        winner, resp = attempt, r
                        break
                    retry_used[0] = n
                    LOG.warning("[attempt] 上游 %d，退避 %gs 后重发（第 %d/%d 次）: %s "
                                "(request=msg_%d)",
                                r.status, backoff, n, RETRY_ON_5XX_MAX,
                                err_body_text[:800], req_num)
                    for a in attempts:
                        a.abandon()
                    attempts = []
                    while True:      # 丢弃被放弃那条可能已入队的陈旧结果
                        try:
                            result_q.get_nowait()
                        except queue.Empty:
                            break
                    pending[0] = 0
                    # 不阻塞等待：交给主循环按退避时刻触发，期间心跳仍可继续为客户端续命
                    pending_retry[0] = (r.status, err_body_text, n)
                    retry_at[0] = time.perf_counter() + backoff
                    continue
                # 不重试：把已读出的错误体留给下面的转发分支复用，避免重复读
                prefetched_err_body[0] = err_body_raw
                winner, resp = attempt, r
                break
            err = e
            LOG.warning("[attempt] %s 失败: %s (request=msg_%d)", attempt.tag, e, req_num)
            if pending[0] == 0 and not retry_fired[0]:
                # 首条就报错（如握手被拒）：不等定时器，立刻补一次重试
                retry_fired[0] = True
                start_attempt("retry")

        # 竞速收敛：只留胜出那条，其余关掉
        for a in attempts:
            if a is not winner:
                a.abandon()

        if winner is None:
            metrics.error = err or "upstream failed"
            metrics.finish(504, session_id, "msg_%d" % req_num)
            if committed:
                # 已提前回 200，错误只能作为 SSE 事件下发
                self._write_chunk(self._sse_error_payload(504, err or "upstream failed"))
                self._end_chunked()
            else:
                self._send_proxy_error("proxy upstream failed: %s" % (err or "unknown"), 504)
            return

        conn = winner.conn
        status = 0
        try:
            status = resp.status
            LOG.info("[upstream] %s %s -> %d (session=%s, request=msg_%d, attempt=%s)",
                     method, self.path, status, session_id, req_num, winner.tag)

            resp_is_chunked = "chunked" in (resp.getheader("Transfer-Encoding") or "").lower()
            # SSE 判定：上游 Content-Type 声明，或首个数据块以 data: 开头
            sse_mode = "event-stream" in (resp.getheader("Content-Type") or "").lower()

            if committed:
                # 已提前回 200 + SSE：上游结果并入这条流，错误以 SSE 事件下发并收尾
                if status >= 400:
                    err_body = (prefetched_err_body[0] if prefetched_err_body[0] is not None
                                else resp.read())
                    metrics.on_chunk(err_body, is_sse=False)
                    snippet = err_body[:800].decode("utf-8", errors="replace")
                    LOG.error("[upstream-error] body: %s", snippet)
                    metrics.error = "upstream %d on slow path" % status
                    self._write_chunk(self._sse_error_payload(status, snippet))
                else:
                    self._pump_stream(resp, metrics, sse_mode)
                    if metrics.error:
                        self._write_chunk(self._sse_error_payload(504, metrics.error))
                self._end_chunked()
                return

            # 转发上游响应头（跳过逐跳头与自行管理的长度/编码头）
            self.send_response(status)
            for k, v in resp.getheaders():
                lk = k.lower()
                if lk in HOP_BY_HOP or lk in ("content-length", "transfer-encoding"):
                    continue
                self.send_header(k, v)

            if status >= 400:
                # 4xx/5xx：完整读出错误体，打印便于定位，并原样回给客户端
                err_body = (prefetched_err_body[0] if prefetched_err_body[0] is not None
                            else resp.read())
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
                if self._pump_stream(resp, metrics, sse_mode):
                    if metrics.error:
                        # 兜底主动终止：明确告知客户端，而不是让它看到一个静默截断的流
                        self._write_chunk(self._sse_error_payload(504, metrics.error))
                    self._end_chunked()
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
                sent = 0
                try:
                    while True:
                        chunk = resp.read1(8192)
                        if not chunk:
                            break
                        if not sse_mode and chunk.lstrip(b"\r\n \t").startswith(b"data:"):
                            sse_mode = True
                        metrics.on_chunk(chunk, is_sse=sse_mode)
                        sent += len(chunk)
                        if self._content_idle_abort(metrics, sent):
                            break
                        collected.append(chunk)
                        self.wfile.write(chunk)
                except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
                    pass
                except socket.timeout:
                    metrics.error = "upstream stalled >%gs" % UPSTREAM_TIMEOUT
                    LOG.warning("[proxy] 上游超时（非流式，%gs 无新数据）", UPSTREAM_TIMEOUT)
                    self.close_connection = True
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

    def _pump_stream(self, resp, metrics, sse_mode):
        """
        把上游流式响应按 chunked 转发给客户端。
        返回 True 表示应当正常收尾（上游读完，或代理按兜底主动终止）；
        返回 False 表示应保持截断（客户端已断开或上游静默），不补 chunked 结束块，
        让客户端判定为响应被截断。
        """
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
                if self._content_idle_abort(metrics, sent):
                    # 兜底主动终止：由调用方补一个 SSE error 事件再收尾
                    return True
                self.wfile.write(b"%x\r\n" % len(chunk) + chunk + b"\r\n")
                self.wfile.flush()
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
            # 客户端收完提前断开，属正常现象
            LOG.info("[proxy] 客户端提前断开（已发送 %d 字节），正常忽略", sent)
            return False
        except socket.timeout:
            # 流式中途上游静默：不补结束块，让客户端判定为响应被截断
            metrics.error = "upstream stalled >%gs mid-stream" % UPSTREAM_TIMEOUT
            LOG.warning("[proxy] 上游流式中断（%gs 无新数据，已发送 %d 字节）",
                        UPSTREAM_TIMEOUT, sent)
            self.close_connection = True
            return False
        return True

    def _content_idle_abort(self, metrics, sent):
        """兜底：距上次内容产出超过 CONTENT_IDLE_TIMEOUT 即主动终止本次转发"""
        if not metrics.content_idle_exceeded():
            return False
        metrics.error = "no content output for %gs" % CONTENT_IDLE_TIMEOUT
        LOG.warning("[proxy] 兜底终止：%gs 无任何内容产出（已发送 %d 字节），主动断开",
                    CONTENT_IDLE_TIMEOUT, sent)
        self.close_connection = True
        return True

    def _commit_sse_head(self):
        """慢路径：上游尚未响应时先提交 200 + SSE，为发送心跳保活铺路"""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

    def _write_chunk(self, data):
        """按 chunked 写一个数据块；客户端已断开时返回 False"""
        try:
            self.wfile.write(b"%x\r\n" % len(data) + data + b"\r\n")
            self.wfile.flush()
            return True
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
            return False

    def _end_chunked(self):
        try:
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
            pass

    @staticmethod
    def _sse_error_payload(status, message):
        payload = json.dumps({"error": {"code": status, "message": message}},
                             ensure_ascii=False)
        return ("data: " + payload + "\n\n").encode("utf-8")

    def _send_proxy_error(self, message, status=502):
        try:
            payload = json.dumps({"error": {"message": message}}).encode("utf-8")
            self.send_response(status)
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
