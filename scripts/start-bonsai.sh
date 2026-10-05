#!/bin/bash
# ============================================================
# Bonsai-2-27B 一键启动（llama-server + Anthropic 桥接代理）
#
#   ./start-bonsai.sh          启动（默认）
#   ./start-bonsai.sh start    启动
#   ./start-bonsai.sh stop     停止全部并释放显存
#   ./start-bonsai.sh status   查看状态
#   ./start-bonsai.sh restart  重启
#
# 环境变量可覆盖：
#   CTX=131072  上下文（默认 102400）
#   HOST=0.0.0.0  监听地址（默认 0.0.0.0 = 内网可访问；改 127.0.0.1 则仅本机）
#   PORT=8080   llama-server 端口
#   BPORT=8081  桥接代理端口
#   API_KEY=xxx  可选 API 密钥（内网暴露时强烈建议设置）
#
# 所有路径都相对于**本脚本所在目录**，整个文件夹可随意移动/改名。
# ============================================================
set -u

# ---------- 以脚本自身位置为基准解析路径 ----------
# 脚本位于 <仓库>/scripts/ 下，所以仓库根是它的上一级。
SELF="${BASH_SOURCE[0]}"
SCRIPT_DIR="$(cd "$(dirname "$SELF")" && pwd)"
BASE_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"          # 仓库根

LLAMA_DIR="$BASE_DIR/llama.cpp"                    # 仓库根/llama.cpp
MODEL="$BASE_DIR/models/Ternary-Bonsai-2-27B-PTQ1_0.gguf"
BRIDGE="$SCRIPT_DIR/anthropic-bridge.py"           # 与本脚本同目录
LOG_DIR="$BASE_DIR/logs"
SETTINGS="$SCRIPT_DIR/claude-settings-bonsai.json"
CTX=${CTX:-102400}
PORT=${PORT:-8080}
BPORT=${BPORT:-8081}
HOST=${HOST:-0.0.0.0}          # 监听地址；0.0.0.0 = 内网可访问，127.0.0.1 = 仅本机
API_KEY=${API_KEY:-}           # 可选：设置后调用方必须带 key（强烈建议内网暴露时设置）
KEYFILE="$BASE_DIR/.bonsai-api-key"   # 记住上次用过的密钥（已 gitignore）

# 没显式传 API_KEY 时，沿用上次的密钥。
# 否则重启一次就会把已经设过密钥的端口**静默地**重新敞开。
if [ -z "$API_KEY" ] && [ -f "$KEYFILE" ]; then
  API_KEY=$(cat "$KEYFILE" 2>/dev/null)
fi

mkdir -p "$LOG_DIR" "$BASE_DIR/reqdump"

# ---------- 工具函数 ----------
c_ok()   { printf '\033[32m✓\033[0m %s\n' "$*"; }
c_bad()  { printf '\033[31m✗\033[0m %s\n' "$*"; }
c_warn() { printf '\033[33m!\033[0m %s\n' "$*"; }
c_info() { printf '\033[36m·\033[0m %s\n' "$*"; }

# 找可用的 python（桥接只用标准库，系统 python3 即可）
PY=""
for p in "$BASE_DIR/.venv/bin/python" /usr/bin/python3 python3; do
  command -v "$p" >/dev/null 2>&1 && { PY="$p"; break; }
done

# 按端口找 pid（避免 pkill 自杀）
pid_on_port() {
  ss -ltnp 2>/dev/null | grep ":$1 " | grep -oP 'pid=\K[0-9]+' | head -1
}

# ---------- stop ----------
do_stop() {
  c_info "停止服务..."
  local n=0
  # 桥接代理
  local bp; bp=$(pid_on_port "$BPORT")
  [ -n "$bp" ] && { kill "$bp" 2>/dev/null && { c_ok "已停桥接代理 (pid $bp)"; n=$((n+1)); }; }
  # llama-server：优先按端口，其次按 GPU 占用进程
  local lp; lp=$(pid_on_port "$PORT")
  [ -n "$lp" ] && { kill "$lp" 2>/dev/null && { c_ok "已停 llama-server (pid $lp)"; n=$((n+1)); }; }
  # 兜底：仍在占显存的进程
  for p in $(nvidia-smi --query-compute-apps=pid --format=csv,noheader 2>/dev/null); do
    kill "$p" 2>/dev/null && { c_ok "已停 GPU 进程 (pid $p)"; n=$((n+1)); }
  done
  [ "$n" = "0" ] && c_info "没有正在运行的服务"

  sleep 3
  local used; used=$(nvidia-smi --query-gpu=memory.used --format=csv,noheader 2>/dev/null)
  c_info "显存占用: $used"
}

# ---------- status ----------
do_status() {
  echo "───────────────────────────────"
  echo "  基准目录: $BASE_DIR"
  echo "───────────────────────────────"
  # llama-server
  if timeout 5 curl -s "http://127.0.0.1:$PORT/health" 2>/dev/null | grep -q ok; then
    c_ok "llama-server   :$PORT  运行中"
  else
    local hp; hp=$(pid_on_port "$PORT")
    [ -n "$hp" ] && c_warn "llama-server   :$PORT  已启动但未就绪（模型加载中）" \
                 || c_bad  "llama-server   :$PORT  未运行"
  fi
  # 桥接（设置了密钥时必须带上，否则会返回 401）
  if [ -n "$(pid_on_port "$BPORT")" ]; then
    local -a AUTH=()
    [ -n "$API_KEY" ] && AUTH=(-H "x-api-key: $API_KEY")
    local code; code=$(timeout 5 curl -s -o /dev/null -w '%{http_code}' \
      ${AUTH[@]+"${AUTH[@]}"} "http://127.0.0.1:$BPORT/v1/models" 2>/dev/null)
    [ "$code" = "200" ] && c_ok "桥接代理       :$BPORT  运行中" \
                        || c_warn "桥接代理       :$BPORT  进程在但返回 HTTP $code"
  else
    c_bad "桥接代理       :$BPORT  未运行"
  fi
  echo "───────────────────────────────"
  if [ -n "$API_KEY" ]; then
    c_info "鉴权: 已启用（调用方需带 x-api-key）"
  else
    c_warn "鉴权: 未启用 —— 任何能访问 $BPORT 端口的人都能用模型"
  fi
  c_info "显存: $(nvidia-smi --query-gpu=memory.used,memory.total --format=csv,noheader 2>/dev/null)"
  command -v claude >/dev/null 2>&1 && \
    c_info "本机接入: claude --settings $SETTINGS"
  local lanip; lanip=$(hostname -I 2>/dev/null | awk '{print $1}')
  [ -n "$lanip" ] && c_info "内网接入: ANTHROPIC_BASE_URL=http://$lanip:$BPORT"
  # 实际在听的地址（可能和 HOST 变量不一致，比如上次是用别的参数启的）
  local listen; listen=$(ss -ltn 2>/dev/null | awk -v p=":$BPORT" '$4 ~ p {print $4}' | head -1)
  [ -n "$listen" ] && c_info "桥接实际监听: $listen"
}

# ---------- start ----------
do_start() {
  # 记住密钥，供下次不带 API_KEY 重启时沿用
  if [ -n "$API_KEY" ]; then
    printf '%s' "$API_KEY" > "$KEYFILE" 2>/dev/null && chmod 600 "$KEYFILE" 2>/dev/null
  fi

  # --- 前置检查 ---
  echo "═══ 前置检查 ═══"
  echo "  基准目录: $BASE_DIR"
  [ -x "$LLAMA_DIR/build/bin/llama-server" ] || { c_bad "找不到 $LLAMA_DIR/build/bin/llama-server"; exit 1; }
  c_ok "llama-server 二进制存在"
  [ -f "$MODEL" ] || { c_bad "找不到模型 $MODEL"; exit 1; }
  c_ok "模型存在 ($(du -h "$MODEL" | cut -f1))"
  [ -f "$BRIDGE" ] || { c_bad "找不到桥接脚本 $BRIDGE"; exit 1; }
  c_ok "桥接脚本存在"
  [ -n "$PY" ] || { c_bad "找不到 python"; exit 1; }
  c_ok "python: $PY"
  nvidia-smi >/dev/null 2>&1 || { c_bad "nvidia-smi 不可用（驱动问题？）"; exit 1; }
  c_ok "GPU: $(nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null)"

  # 已经在跑就直接用，不重复启动
  if timeout 5 curl -s "http://127.0.0.1:$PORT/health" 2>/dev/null | grep -q ok; then
    c_warn "llama-server 已在 :$PORT 运行，跳过启动"
  else
    # --- ① 启动 llama-server ---
    echo
    echo "═══ ① 启动 llama-server (:${PORT}, ctx=${CTX}, 监听 ${HOST}) ═══"
    export LD_LIBRARY_PATH="$LLAMA_DIR/build/bin:${LD_LIBRARY_PATH:-}"
    # 用数组拼参数：API_KEY 里即便有空格/特殊字符也不会被拆错
    local -a SRV_ARGS=(
      -m "$MODEL"
      -ngl 99 -c "$CTX" -fa on -ctk q4_0 -ctv q4_0
      -b 2048 -ub 512 -t 8
      --host "$HOST" --port "$PORT" --parallel 1
      --jinja
      --chat-template-kwargs '{"reasoning_effort":"medium"}'
    )
    [ -n "$API_KEY" ] && SRV_ARGS+=(--api-key "$API_KEY")
    setsid nohup "$LLAMA_DIR/build/bin/llama-server" "${SRV_ARGS[@]}" \
      > "$LOG_DIR/llama-server.log" 2>&1 < /dev/null &
    disown
    c_info "已启动，等待模型加载（首次约 1–2 分钟）..."

    local waited=0
    while [ "$waited" -lt 600 ]; do
      if timeout 5 curl -s "http://127.0.0.1:$PORT/health" 2>/dev/null | grep -q ok; then break; fi
      # ⚠️ 只匹配真正的致命错误。
      # 注意 `common_fit_params: failed to fit params ...` 只是「跳过自动调优」的警告，
      # 因为我们已经显式指定了 -ngl 99，属于正常输出，不能当作错误。
      if grep -aE " E |out of memory|CUDA error|failed to allocate|failed to initialize the context|exiting due to" \
           "$LOG_DIR/llama-server.log" 2>/dev/null | grep -qv "common_fit_params"; then
        c_bad "llama-server 报错，日志末尾："
        tail -15 "$LOG_DIR/llama-server.log"
        exit 1
      fi
      sleep 5; waited=$((waited+5))
      [ $((waited % 30)) -eq 0 ] && c_info "  ...已等待 ${waited}s"
    done
    if ! timeout 5 curl -s "http://127.0.0.1:$PORT/health" 2>/dev/null | grep -q ok; then
      c_bad "600s 内未就绪，日志末尾："; tail -15 "$LOG_DIR/llama-server.log"; exit 1
    fi
    c_ok "llama-server 就绪"
    c_info "显存: $(nvidia-smi --query-gpu=memory.used --format=csv,noheader)"
  fi

  # --- ② 启动桥接代理 ---
  echo
  echo "═══ ② 启动桥接代理 (:${BPORT} → :${PORT}) ═══"
  if [ -n "$(pid_on_port "$BPORT")" ]; then
    c_warn "桥接代理已在 :$BPORT 运行，跳过"
  else
    # 参数：端口、基准目录（用于定位 reqdump）、监听地址、可选 API 密钥
    # 代理是唯一鉴权边界：校验客户端密钥后，用同一个密钥去访问上游。
    setsid nohup "$PY" "$BRIDGE" "$BPORT" "$BASE_DIR" "$HOST" "$API_KEY" \
      > "$LOG_DIR/bridge.log" 2>&1 < /dev/null &
    disown
    sleep 2
    if [ -n "$(pid_on_port "$BPORT")" ]; then
      c_ok "桥接代理已启动"
    else
      c_bad "桥接代理启动失败："; tail -10 "$LOG_DIR/bridge.log"; exit 1
    fi
  fi

  # --- ③ 端到端校验 ---
  echo
  echo "═══ ③ 端到端校验（经代理调 Anthropic 接口）═══"
  local -a AUTH=()
  [ -n "$API_KEY" ] && AUTH=(-H "x-api-key: $API_KEY")
  local resp
  resp=$(timeout 180 curl -s "http://127.0.0.1:$BPORT/v1/messages" \
    -H 'Content-Type: application/json' -H 'anthropic-version: 2023-06-01' \
    ${AUTH[@]+"${AUTH[@]}"} \
    -d '{"model":"bonsai","max_tokens":512,
         "messages":[{"role":"user","content":"Reply with exactly: OK"}]}' 2>/dev/null)

  if echo "$resp" | grep -q '"type"'; then
    local txt; txt=$(echo "$resp" | "$PY" -c "
import json,sys
try:
    d=json.load(sys.stdin)
    print(''.join(c.get('text','') for c in d.get('content',[]) if c.get('type')=='text'))
except Exception: print('(解析失败)')" 2>/dev/null)
    if [ -n "$txt" ]; then
      c_ok "接口正常，模型回复: $txt"
    else
      c_warn "接口通了但没返回正文（推理模型的思考吃掉了输出预算）"
      c_info "调大 max_tokens 或降低 reasoning_effort 再试；接口本身是正常的"
    fi
  else
    c_bad "校验失败，返回："
    echo "$resp" | head -c 400; echo
    c_info "排查：tail -20 $LOG_DIR/bridge.log"
    exit 1
  fi

  # --- 完成 ---
  echo
  echo "═══════════════════════════════"
  c_ok "全部就绪"
  echo "═══════════════════════════════"
  local lanip; lanip=$(hostname -I 2>/dev/null | awk '{print $1}')
  echo "  监听地址   : $HOST  (llama-server :$PORT / 桥接 :$BPORT)"
  if [ -n "$lanip" ]; then
    echo
    echo "  ── 本机使用 ──"
    echo "    claude --settings $SETTINGS"
    echo
    echo "  ── 内网其他机器使用 ──"
    echo "    本机 IP  : $lanip"
    echo "    Claude Code 环境变量："
    echo "      ANTHROPIC_BASE_URL=http://$lanip:$BPORT"
    echo "      ANTHROPIC_API_KEY=${API_KEY:-local-bonsai}"
    echo "      ANTHROPIC_MODEL=bonsai"
    echo "      CLAUDE_CODE_MAX_CONTEXT_TOKENS=$CTX"
    echo "    或直接用 curl 自测："
    printf '      curl http://%s:%s/v1/messages \\\n' "$lanip" "$BPORT"
    echo "        -H 'content-type: application/json' -H 'anthropic-version: 2023-06-01' \\"
    [ -n "$API_KEY" ] && echo "        -H 'x-api-key: $API_KEY' \\"
    echo "        -d '{\"model\":\"bonsai\",\"max_tokens\":64,\"messages\":[{\"role\":\"user\",\"content\":\"hi\"}]}'"
  fi
  # 常见坑：设了 API_KEY，但本机 Claude Code 的 settings 还是老 key → 自己反而连不上
  if [ -n "$API_KEY" ] && [ -f "$SETTINGS" ]; then
    local cur; cur=$(grep -oP '"ANTHROPIC_API_KEY"\s*:\s*"\K[^"]*' "$SETTINGS" 2>/dev/null)
    if [ -n "$cur" ] && [ "$cur" != "$API_KEY" ]; then
      c_warn "settings 里的 ANTHROPIC_API_KEY (\"$cur\") 与本次 API_KEY 不一致"
      c_info "改这里，否则本机 Claude Code 会 401：$SETTINGS"
    fi
  fi

  echo
  if [ -n "$API_KEY" ]; then
    c_ok "已启用 API 密钥鉴权（调用方必须带 x-api-key 或 Authorization: Bearer）"
    c_info "密钥已存到 $KEYFILE（下次不带 API_KEY 重启会自动沿用）"
    c_info "想改成不鉴权：rm $KEYFILE 后重启"
  else
    c_warn "未设 API_KEY —— 能连到本机 $BPORT 端口的人都能免费用模型！"
    c_info "建议重启时带上密钥：API_KEY=\$(openssl rand -hex 16) $0 restart"
  fi
  # 如果对外监听，顺手看一眼防火墙
  if [ "$HOST" = "0.0.0.0" ] && command -v ufw >/dev/null 2>&1; then
    if ufw status 2>/dev/null | grep -q "^Status: active"; then
      local lan; lan=$(hostname -I 2>/dev/null | awk '{print $1}')
      local net="${lan%.*}.0/24"
      c_info "检测到 ufw 已启用，如需内网访问请放行："
      echo "      ufw allow from $net to any port $BPORT proto tcp"
    fi
  fi
  echo
  echo "  停止：$0 stop      状态：$0 status"
  echo "  日志：$LOG_DIR/"
}

case "${1:-start}" in
  start)   do_start ;;
  stop)    do_stop ;;
  status)  do_status ;;
  restart) do_stop; echo; do_start ;;
  *) echo "用法: $0 {start|stop|status|restart}"; exit 0 ;;
esac
