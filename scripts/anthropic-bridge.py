#!/usr/bin/env python3
"""Anthropic -> llama-server 桥接代理。

解决的问题：Claude Code 会把 `role: system` 的消息混在 messages 数组里，
而 Bonsai 的 Qwen 系 chat template 要求 system 必须在最前，否则报
"System message must be at the beginning"。

做法：把所有 system 内容（顶层 system 字段 + messages 里的 system 角色）
合并成：顶层 system 字段（放在最前） + messages 只保留 user/assistant。

用法：
    python3 anthropic-bridge.py [端口] [基准目录] [监听地址] [API密钥]

    端口     默认 8081
    基准目录  可选；reqdump 放在 <基准目录>/reqdump/ 下。不传时用本脚本所在目录。
    监听地址  默认 0.0.0.0（内网可访问）；127.0.0.1 则仅本机
    API密钥   可选；非空时调用方必须携带，且代理会用它去访问上游

关于密钥：代理是**唯一鉴权边界**。调用方带 x-api-key 或 Authorization: Bearer
都可以。校验通过后，代理把客户端那套头**换成**上游密钥再转发，
所以上游 llama-server 的 `--api-key` 用同一个值即可，客户端无需知道更多。
未设置密钥时不做鉴权（仅适合纯内网可信环境）。

接入（本机或内网其他机器都一样）：
    ANTHROPIC_BASE_URL=http://<本机IP>:8081
    ANTHROPIC_API_KEY=<API密钥>          # 设置了密钥时必填
"""
import http.server, urllib.request, urllib.error, json, sys, os, re

# llama.cpp 超上下文时的措辞（Claude Code 不认识它，见 _translate_upstream_error）
_TOO_LONG_RE = re.compile(r"exceeds the available context size", re.I)

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8081

# 基准目录：由调用方传入（start-bonsai.sh 会传仓库根）；
# 未传时退回本脚本所在目录。reqdump 就放在其下。
BASE_DIR = os.path.abspath(sys.argv[2]) if len(sys.argv) > 2 \
    else os.path.dirname(os.path.abspath(__file__))
REQDUMP = os.path.join(BASE_DIR, "reqdump", "last_raw.json")
os.makedirs(os.path.dirname(REQDUMP), exist_ok=True)

# 监听地址：0.0.0.0 = 内网可访问；127.0.0.1 = 仅本机
HOST = sys.argv[3] if len(sys.argv) > 3 else "0.0.0.0"
# 可选 API 密钥：非空时校验调用方，并用于访问上游
API_KEY = sys.argv[4] if len(sys.argv) > 4 else ""

# 上游 llama-server 始终走本机回环：即使对外监听 0.0.0.0，
# 也没有必要让流量绕一圈网卡出去。
UPSTREAM = "http://127.0.0.1:8080"

# 转发时丢弃的头：
#  - 逐跳头 / 由 urllib 自己算的头
#  - accept-encoding：urllib 不会自动解压，若上游压缩而我们转发的是压缩体、
#    又得改 content-encoding，很容易搞出乱码。直接要求上游发明文最稳。
_DROP_HEADERS = ("host", "content-length", "connection", "transfer-encoding",
                 "keep-alive", "proxy-connection", "upgrade", "accept-encoding")


def _text_of(content):
    """把 string / list-of-blocks 统一成字符串列表"""
    if isinstance(content, str):
        return [content]
    out = []
    if isinstance(content, list):
        for b in content:
            if isinstance(b, dict) and b.get("type") == "text":
                out.append(b.get("text", ""))
            elif isinstance(b, str):
                out.append(b)
    return out


def normalize(payload: dict) -> dict:
    msgs = payload.get("messages") or []
    parts = []

    # 1) 顶层 system 字段
    sysf = payload.get("system")
    if sysf:
        parts.extend(_text_of(sysf))

    # 2) messages 里的 system 角色 —— 抽出并移除
    keep = []
    for m in msgs:
        if m.get("role") == "system":
            parts.extend(_text_of(m.get("content")))
        else:
            keep.append(m)

    # 3) 重写：system 合并为单个顶层字段，messages 只留 user/assistant
    if parts:
        payload["system"] = "\n\n".join(p for p in parts if p)
    elif "system" in payload:
        payload.pop("system")
    payload["messages"] = keep

    # 4) 去掉 llama-server 不认识的 Anthropic 专有字段（避免 400）
    for k in ("context_management", "safeguards", "output_config",
              "metadata", "betas"):
        payload.pop(k, None)

    # 5) thinking 字段：llama-server 用 chat template 控制，去掉以免干扰
    payload.pop("thinking", None)

    return payload


def _presented_key(headers):
    """从请求头取出调用方提供的密钥（兼容 Anthropic 的 x-api-key 与 Bearer）。"""
    k = headers.get("x-api-key")
    if k:
        return k.strip()
    auth = headers.get("Authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return ""


def _upstream_headers(headers):
    """把客户端请求头转成发往上游的头。

    - 丢掉逐跳头
    - 丢掉客户端自己的鉴权头（避免把用户密钥泄露给上游日志）
    - 代理自己带上上游密钥
    """
    out = {k: v for k, v in headers.items() if k.lower() not in _DROP_HEADERS}
    for k in list(out):
        if k.lower() in ("authorization", "x-api-key"):
            out.pop(k)
    if API_KEY:
        out["Authorization"] = f"Bearer {API_KEY}"
    return out


def _translate_upstream_error(body: bytes) -> bytes:
    """把 llama.cpp 的「超出上下文」错误改写成 Anthropic 的形状。

    为什么必须改写：Claude Code 只在错误文本里认出 `prompt is too long`
    才会走「压缩对话后重试」。llama.cpp 的原话是
        request (135013 tokens) exceeds the available context size (102400 tokens)
    它一个关键词都不匹配，于是 Claude Code 既不压缩也不重试，整轮直接失败。
    （见 claude.exe 里的检测函数： message.includes("prompt is too long")）

    改写后，即便主动压缩没赶上，被动恢复也能生效。
    """
    try:
        j = json.loads(body)
    except Exception:
        return body
    err = j.get("error") if isinstance(j, dict) else None
    if not isinstance(err, dict):
        return body

    etype = str(err.get("type", ""))
    msg = str(err.get("message", ""))
    if "exceed_context_size_error" not in etype and \
       not _TOO_LONG_RE.search(msg):
        return body          # 不是超上下文，原样放行

    # 优先用结构化字段；没有就从 "(135013 tokens) ... (102400 tokens)" 里抠
    actual, limit = err.get("n_prompt_tokens"), err.get("n_ctx")
    if actual is None or limit is None:
        nums = re.findall(r"\((\d+) tokens\)", msg)
        if len(nums) >= 2:
            actual, limit = int(nums[0]), int(nums[1])

    # Claude Code 会用这个正则反解出实际/上限 token 数：
    #   /prompt is too long[^0-9]*(\d+)\s*tokens?\s*>\s*(\d+)/i
    text = f"prompt is too long: {actual} tokens > {limit} maximum"
    return json.dumps({"type": "error",
                       "error": {"type": "invalid_request_error",
                                 "message": text}}).encode()


def _json_error(handler, code, etype, message):
    d = json.dumps({"type": "error",
                    "error": {"type": etype, "message": message}}).encode()
    handler.send_response(code)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(d)))
    handler.end_headers()
    handler.wfile.write(d)


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _authorized(self):
        """设置了 API_KEY 时必须校验；未设置则放行。"""
        if not API_KEY:
            return True
        return _presented_key(self.headers) == API_KEY

    def _canon(self, msg, kind):
        sys.stderr.write(f"[bridge] {kind}: {msg}\n")
        sys.stderr.flush()

    def _relay(self, status, headers, data):
        """整体转发（响应体已全部读进内存）——用于小体积的 JSON / 错误响应。"""
        self.send_response(status)
        for k, v in headers:
            if k.lower() not in ("transfer-encoding", "connection",
                                 "content-length", "content-encoding"):
                self.send_header(k, v)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _relay_stream(self, r):
        """边收边发（chunked）——用于 SSE 流式响应。

        必须流式转发：如果先 read() 再整体下发，客户端要等模型全部生成完才
        看到第一个字，长任务里看起来就像卡死了。
        """
        self.send_response(r.status)
        for k, v in r.headers.items():
            if k.lower() not in ("transfer-encoding", "connection",
                                 "content-length", "content-encoding"):
                self.send_header(k, v)
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        try:
            while True:
                # read1 有数据就返回，不会为了凑满缓冲区而干等
                chunk = r.read1(65536)
                if not chunk:
                    break
                self.wfile.write(b"%X\r\n" % len(chunk) + chunk + b"\r\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            # 客户端提前断开（用户按了 Esc 之类），正常现象
            pass
        finally:
            try:
                self.wfile.write(b"0\r\n\r\n")   # 结束 chunked 编码
                self.wfile.flush()
            except Exception:
                pass
            r.close()

    def _relay_response(self, r):
        """按响应类型选择：SSE 走流式，其余整体转发。"""
        ctype = (r.headers.get("Content-Type") or "").lower()
        if ctype.startswith("text/event-stream"):
            self._relay_stream(r)
        else:
            self._relay(r.status, r.headers.items(), r.read())

    def do_POST(self):
        if not self._authorized():
            self._canon("401 unauthorized (bad or missing api key)", "err")
            _json_error(self, 401, "authentication_error",
                        "invalid or missing API key")
            return

        n = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(n)
        try:
            j = json.loads(body)
            json.dump(j, open(REQDUMP, "w"), ensure_ascii=False)
            j = normalize(j)
            body = json.dumps(j).encode()
            self._canon(f"{self.path} roles=" +
                        str([m.get('role') for m in j.get('messages', [])]) +
                        f" sys={'yes' if j.get('system') else 'no'}",
                        "req")
        except Exception as e:
            self._canon(f"normalize failed: {e}", "warn")

        req = urllib.request.Request(
            UPSTREAM + self.path, data=body,
            headers=_upstream_headers(self.headers))
        try:
            r = urllib.request.urlopen(req, timeout=1800)
            self._relay_response(r)
            self._canon(f"{self.path} -> 200 {r.headers.get('Content-Type','')}",
                        "ok")
        except urllib.error.HTTPError as e:
            d = _translate_upstream_error(e.read())
            self._canon(f"upstream {e.code}: {d[:300]!r}", "err")
            self._relay(e.code, e.headers.items(), d)
        except Exception as e:
            self._canon(f"proxy error: {e}", "err")
            _json_error(self, 502, "api_error",
                        f"cannot reach upstream {UPSTREAM}: {e}")

    def do_GET(self):
        if not self._authorized():
            _json_error(self, 401, "authentication_error",
                        "invalid or missing API key")
            return
        try:
            req = urllib.request.Request(
                UPSTREAM + self.path,
                headers=_upstream_headers(self.headers))
            r = urllib.request.urlopen(req, timeout=60)
            self._relay(r.status, r.headers.items(), r.read())
        except urllib.error.HTTPError as e:
            d = e.read()
            self._canon(f"upstream {e.code}: {d[:200]!r}", "err")
            self._relay(e.code, e.headers.items(), d)
        except Exception as e:
            self._canon(f"upstream unreachable: {e}", "err")
            _json_error(self, 502, "api_error",
                        f"cannot reach upstream {UPSTREAM}: {e}")


if __name__ == "__main__":
    srv = http.server.ThreadingHTTPServer((HOST, PORT), Handler)
    srv.daemon_threads = True
    print(f"bridge on {HOST}:{PORT} -> {UPSTREAM}  "
          f"(auth: {'API key required' if API_KEY else 'OPEN - no key'})",
          flush=True)
    if not API_KEY:
        print("WARNING: no API key set - anyone who can reach this port can "
              "use the model.", flush=True)
    srv.serve_forever()
