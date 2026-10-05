#!/usr/bin/env python3
"""Anthropic -> llama-server 桥接代理。

解决的问题：Claude Code 会把 `role: system` 的消息混在 messages 数组里，
而 Bonsai 的 Qwen 系 chat template 要求 system 必须在最前，否则报
"System message must be at the beginning"。

做法：把所有 system 内容（顶层 system 字段 + messages 里的 system 角色）
合并成：顶层 system 字段（放在最前） + messages 只保留 user/assistant。

用法：
    python3 anthropic-bridge.py [端口] [基准目录]

    端口     默认 8081
    基准目录  可选；reqdump 放在 <基准目录>/reqdump/ 下。
              不传时用本脚本所在目录。

然后让 Claude Code 指向：ANTHROPIC_BASE_URL=http://127.0.0.1:8081
"""
import http.server, urllib.request, urllib.error, json, sys, os

UPSTREAM = "http://127.0.0.1:8080"
PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8081

# 基准目录：由调用方传入（start-bonsai.sh 会传脚本自身所在目录）；
# 未传时退回本脚本所在目录。reqdump 就放在其下。
BASE_DIR = os.path.abspath(sys.argv[2]) if len(sys.argv) > 2 \
    else os.path.dirname(os.path.abspath(__file__))
REQDUMP = os.path.join(BASE_DIR, "reqdump", "last_raw.json")
os.makedirs(os.path.dirname(REQDUMP), exist_ok=True)


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


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _canon(self, msg, kind):
        sys.stderr.write(f"[bridge] {kind}: {msg}\n")
        sys.stderr.flush()

    def do_POST(self):
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
            headers={k: v for k, v in self.headers.items()
                     if k.lower() not in ("host", "content-length")})
        try:
            r = urllib.request.urlopen(req, timeout=1800)
            data = r.read()
            self.send_response(r.status)
            for k, v in r.headers.items():
                if k.lower() not in ("transfer-encoding", "connection",
                                     "content-length"):
                    self.send_header(k, v)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except urllib.error.HTTPError as e:
            d = e.read()
            self._canon(f"upstream {e.code}: {d[:300]!r}", "err")
            self.send_response(e.code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(d)))
            self.end_headers()
            self.wfile.write(d)
        except Exception as e:
            self._canon(f"proxy error: {e}", "err")
            d = json.dumps({"type": "error",
                            "error": {"type": "api_error",
                                      "message": str(e)}}).encode()
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(d)))
            self.end_headers()
            self.wfile.write(d)

    def do_GET(self):
        try:
            r = urllib.request.urlopen(UPSTREAM + self.path, timeout=60)
            data = r.read()
            self.send_response(r.status)
            self.send_header("Content-Type",
                             r.headers.get("Content-Type", "application/json"))
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except Exception as e:
            self.send_response(502)
            self.send_header("Content-Length", "0")
            self.end_headers()


if __name__ == "__main__":
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    srv.daemon_threads = True
    print(f"bridge on 127.0.0.1:{PORT} -> {UPSTREAM}", flush=True)
    srv.serve_forever()
