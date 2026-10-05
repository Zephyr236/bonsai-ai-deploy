# bonsai-deploy

**在单张消费级显卡上部署 Bonsai-2-27B（三元量化 27B 模型），并接入 Claude Code。**

模型是 [PrismML 的 Ternary Bonsai 2 27B](https://huggingface.co/prism-ml/Ternary-Bonsai-2-27B-gguf)：
Qwen3.8-27B 的权重全部三元化（−1 / 0 / +1）后 **只有 5.95 GB**，
保留了原模型 **98.2%** 的能力 —— 一张 8GB 显卡就能跑，生成速度 ~51 t/s。

---

## 怎么用（一句话）

**把 `AI-DEPLOY.md` 的内容发给你的 AI 编码助手**（Claude Code / Cursor / Codex …），
让它照着在这台机器上部署。文档里有分支判断与故障处理，比固定脚本更能应对真实环境。

```
请照着这个文档帮我部署：AI-DEPLOY.md
```

或者直接说：

```
按 https://github.com/<你的用户名>/bonsai-deploy 里的 AI-DEPLOY.md 部署 Bonsai-2-27B
```

> 部署过程中 AI 会读取 `docs/PARAM-TUNING.md`，**根据你机器的显存/内存/CPU 和用途**
> 调整上下文长度、KV 量化、并行槽位等参数，而不是套用固定默认值。

---

## 仓库内容

| 路径 | 作用 |
|---|---|
| **`AI-DEPLOY.md`** | ⭐ **核心**：给 AI agent 的部署指令（环境探查 → 驱动 → CUDA → 编译 → 下载 → 验证）。含决策分支与故障速查 |
| `scripts/start-bonsai.sh` | 一键启动 / 停止 / 状态。**相对路径**，整个目录可任意移动 |
| `scripts/anthropic-bridge.py` | Anthropic API 桥接代理（**接入 Claude Code 必需**，见下） |
| `scripts/claude-settings-bonsai.json` | Claude Code 配置 |
| **`docs/PARAM-TUNING.md`** | ⭐ **参数调优**：按显存/内存/CPU/用途调整 llama.cpp 参数，含 KV 显存公式、实测上限逼近法、速度-上下文曲线 |
| `docs/TROUBLESHOOTING.md` | 故障速查（也可直接看 AI-DEPLOY.md §9） |

> **本仓库不含**模型权重（5.95GB）与 llama.cpp 编译产物 —— 二者超出 GitHub 体积限制，
> 且编译产物与 GPU 架构绑定。部署时由 `AI-DEPLOY.md` 引导下载/编译。

---

## 硬件要求

| | |
|---|---|
| **GPU** | NVIDIA RTX 20/30/40/50 系，**显存 ≥ 8 GB**（实测 8GB 可跑，上下文上限约 100K） |
| **驱动** | 580 或更新 |
| **内存** | ≥ 16 GB |
| **磁盘** | ≥ 20 GB 空闲 |
| **系统** | Ubuntu 22.04 / 24.04（其他发行版需自行调整包名） |

实测参考硬件：RTX 3070 8GB + Xeon E5-2696 v2 + 48GB RAM
→ 4K 上下文 **51 t/s**，100K 上下文 **39 t/s**，预填充 888 t/s。

---

## 三个容易踩的坑（部署前务必知道）

### 1. "T3 slim" 不能用 llama.cpp

如果你看到的是 `bonsai2-27b-t3-slim.q27` —— 那是 **q27 引擎**的私有格式，
**llama.cpp 无法加载**。llama.cpp 侧要用 **PTQ1_0**（同为官方「8GB 显卡包」）。

### 2. 必须用 PrismML 的 fork，且必须自编译

三元权重需要自定义 CUDA 内核，只在 `PrismML-Eng/llama.cpp` 的 `prism` 分支里。
**官方预编译二进制只含 `sm_120a`（RTX 50 系）**，20/30/40 系一律要源码编译。

### 3. 接入 Claude Code 需要桥接代理

llama-server 原生支持 Anthropic API，但 Claude Code 会把 `role: system` 的消息
混在 messages 数组里，而 Bonsai 的 Qwen 系模板要求 system 置顶，会直接报错。
`scripts/anthropic-bridge.py` 负责规范化。

---

## 快速命令（部署完成后）

```bash
# 启动（含自检）
./scripts/start-bonsai.sh start

# 接入 Claude Code
claude --settings ./scripts/claude-settings-bonsai.json

# 看状态 / 停止
./scripts/start-bonsai.sh status
./scripts/start-bonsai.sh stop
```

---

## 许可

本仓库代码：Apache-2.0（见 `LICENSE`）。

模型权重：[Apache-2.0](https://huggingface.co/prism-ml/Ternary-Bonsai-2-27B-gguf)（PrismML）。
本仓库仅提供部署脚本与文档，不包含、不重新分发模型权重。

---

## 致谢

- [PrismML](https://huggingface.co/prism-ml) —— 三元量化模型与 CUDA 内核
- [PrismML-Eng/llama.cpp](https://github.com/PrismML-Eng/llama.cpp) —— 支持三元内核的 llama.cpp fork
- [llama.cpp](https://github.com/ggml-org/llama.cpp) —— 上游推理引擎
