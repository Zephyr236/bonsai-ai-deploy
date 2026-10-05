# 故障速查

**中文** | [English](TROUBLESHOOTING.en.md)


> 详细版见 [`../AI-DEPLOY.md`](../AI-DEPLOY.md) §9。这里只列最常遇到的。

## 按症状查

| 症状 | 原因 | 处理 |
|---|---|---|
| `libggml-cuda.so: cannot open shared object file` | 没设 `LD_LIBRARY_PATH` | `export LD_LIBRARY_PATH=<llama.cpp>/build/bin` |
| `unknown model architecture` | 用了官方 llama.cpp | 换 **PrismML-Eng/llama.cpp** 的 `prism` 分支 |
| 加载成功但输出全是乱码 | 官方 llama.cpp 静默加载了 Q2_0，缺 Hadamard 激活运行时 | 同上 |
| `cudaMalloc failed: out of memory` | 上下文 + 权重 > 显存 | 降 `-c`，或加 `-nkvo`。**先看报错里的 `allocating X MiB` 数字** |
| `failed to allocate buffer for rs cache` | llama-server 多 slot 分配循环状态缓存 | 加 **`--parallel 1`** |
| `NVRM: GPU ... already bound to nouveau` | 卸载 nouveau 前没停显示栈 | 先 `systemctl stop gdm`，确认 `pgrep -a Xorg` 为空，再 `modprobe -r nouveau` |
| **输出为空 / 只有思考没有结论** | 推理模型思考吃掉了输出预算 | `-n 16384` 起步；`--reasoning-effort medium` |
| `System message must be at the beginning` | Claude Code 直连了 8080 | `ANTHROPIC_BASE_URL` 必须指向桥接 **8081** |
| `[claude-code:unrecognized_model]` | 它不认识 `bonsai` 这个模型名 | **仅提示，不影响使用** |
| GPU 报 `illegal memory access` (Xid 13/43) | 用 PrismML fork 跑**非 Bonsai** 的 MoE 模型 | 该 fork 仅为 Bonsai 定制，别的模型要换上游 llama.cpp |
| 长会话末尾回答变短 | `MAX_OUTPUT_TOKENS` 挤占了 prompt 预算 | 调小它，或调大服务端 `-c`（两者保持 `n_ctx = prompt + output`） |

## 不是错误的输出（别误判）

```
common_fit_params: failed to fit params to free device memory:
  n_gpu_layers already set by user to 99, abort
```
→ 这是**警告**：用户已显式指定 `-ngl`，跳过自动内存调优。**正常**。

```
llama_server: NOTICE: server default port will be changed to :9931 ...
```
→ 上游的端口变更预告，**不影响**当前 `--port 8080`。

## 操作安全

**不要用 `pkill -f "<关键字>"`** —— 如果命令本身包含该关键字（比如
`pkill -f "llama-server"`），可能把执行它的 shell 一起杀掉。安全做法：

```bash
# 按端口
kill $(ss -ltnp 2>/dev/null | grep ':8080' | grep -oP 'pid=\K[0-9]+')
# 按 GPU 占用
kill $(nvidia-smi --query-compute-apps=pid --format=csv,noheader)
```
