# bonsai-ai-deploy · 部署指令（供 AI agent 执行）

> **你是一个 AI 编码助手，用户让你在这台机器上部署 Bonsai-2-27B 并接入 Claude Code。**
>
> 本文件是**操作指南**，不是脚本。请按顺序执行，**每步都验证**，遇到分支按「如果…则…」处理。
> 用户的机器情况各不相同，**不要盲目照抄命令** —— 先看清实际输出再决定下一步。

---

## 0. 动手之前：先向用户确认三件事

| 要确认的 | 为什么 |
|---|---|
| **磁盘有 ≥ 20GB 空闲** | 模型 5.95GB + 编译产物 + CUDA 约 15GB |
| **网络能访问 GitHub 和 HuggingFace（或其镜像）** | 要 clone 源码、下载模型 |
| **可以重启机器**（如果没装 NVIDIA 驱动） | 装驱动后需要重启才能生效 |

> 如果用户对下载量敏感，提前说明：**模型约 6GB，CUDA Toolkit 约 5GB**。

---

## 1. 环境探查（不要跳过，也不要猜）

```bash
# GPU：型号、显存、驱动
nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv 2>&1

# 显卡当前绑定在哪个驱动上
lspci -nnk | grep -iA3 "vga\|3d"

# 内核模块
lsmod | grep -E "^nouveau|^nvidia"

# 系统资源
free -h ; df -h / ; nproc ; uname -r ; lsb_release -a 2>/dev/null

# 已有工具
for t in nvidia-smi nvcc gcc cmake git curl pidstat; do
  printf "%-12s %s\n" "$t" "$(command -v $t || echo '缺失')"
done

# CUDA 是否已装
ls -d /usr/local/cuda* 2>/dev/null
```

### 根据探查结果决定路线

| 现象 | 含义 | 去哪一步 |
|---|---|---|
| `nvidia-smi` 正常输出 | 驱动 OK | → §3 |
| `nvidia-smi: command not found`，且 `lsmod` 有 `nouveau` | 未装驱动，nouveau 占卡 | → §2 |
| `nvidia-smi` 报 `couldn't communicate with NVIDIA driver` | 驱动装了但没生效 | → §2（多半要重启） |
| `/usr/local/cuda-12.8/bin/nvcc` 存在 | CUDA 已装 | → §4 |
| `nvidia-smi` 里显存 < 7GB | 小显存 | ⚠️ 记下，后面 `CTX` 要调小 |

**记下这些数字，后面要用**：
- 显存大小（MiB）—— 决定上下文上限
- GPU 计算能力（`nvidia-smi --query-gpu=compute_cap --format=csv,noheader`，如 `8.6`）—— 决定 CUDA 架构
- 内存大小（GB）

---

## 2. 安装 NVIDIA 驱动（仅在 §1 判定需要时装）

**目标**：580 或更新版本。

### 2.1 安装

```bash
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y --no-install-recommends dkms nvidia-headless-580 build-essential

# 验证内核模块是否编译并安装成功
dkms status
ls /lib/modules/$(uname -r)/updates/dkms/     # 应看到 nvidia.ko.zst 等
```

**如果 `dkms status` 显示 failed**：
- 看 `/var/lib/dkms/nvidia/*/build/make.log` 找原因
- 常见：缺 `linux-headers-$(uname -r)` → `apt install linux-headers-$(uname -r)`
- 装完 headers 后重跑：`dkms autoinstall`

### 2.2 屏蔽 nouveau

```bash
cat > /etc/modprobe.d/blacklist-nouveau.conf <<'EOF'
blacklist nouveau
options nouveau modeset=0
EOF
update-initramfs -u
```

### 2.3 ⚠️⚠️ 最容易出事的一步 —— 必须先停显示栈

**这是本流程最大的坑，务必理解后再动手：**

有些机器上**图形桌面正通过 nouveau 使用这块显卡**。此时直接 `modprobe -r nouveau`
会让 `Xorg` / `gnome-shell` 卡死在**不可中断状态（D 状态，kill 不掉）**，
nouveau 卸载不掉，nvidia 无法接管 —— **只能重启**。

**正确顺序**：

```bash
# 1) 先查有没有桌面在跑
systemctl is-active gdm lightdm sddm 2>/dev/null
pgrep -a Xorg

# 2) 有就停掉，并确认 Xorg 真的退出了
systemctl stop gdm        # 换成本机实际的管理器
sleep 2
pgrep -a Xorg             # 必须为空！

# 3) 现在才卸 nouveau
modprobe -r nouveau

# 4) 加载 nvidia
modprobe nvidia && modprobe nvidia_uvm && modprobe nvidia_drm
nvidia-smi
```

**如果用户是通过 SSH/远程桌面在操作**，停显示管理器不影响他；如果是**本地图形界面**，
要提醒用户「屏幕会黑一下，属正常，别关电源」。

### 2.4 开机自动加载（关键取舍）

```bash
cat > /etc/modules-load.d/nvidia.conf <<'EOF'
nvidia
nvidia_uvm
EOF
```

> **故意不加载 `nvidia_drm`**：否则桌面会重新抢占 nvidia 卡，重现 §2.3 的事故。
> 计算场景不需要 `nvidia_drm`。

### 2.5 如果需要重启

若 `modprobe nvidia` 报 `No such device`，或 `nvidia-smi` 仍失败：
**告诉用户需要重启**，重启后从 §1 重新检查。不要反复尝试加载。

---

## 3. 安装 CUDA Toolkit

```bash
# 3.1 加官方源（把 ubuntu2404 换成你的发行版代号，如 ubuntu2204）
mkdir -p /usr/share/keyrings
curl -fsSL https://developer.download.nvidia.com/compute/cuda/repos/ubuntu2404/x86_64/cuda-archive-keyring.gpg \
  -o /usr/share/keyrings/cuda-archive-keyring.gpg

echo "deb [signed-by=/usr/share/keyrings/cuda-archive-keyring.gpg] \
https://developer.download.nvidia.com/compute/cuda/repos/ubuntu2404/x86_64/ /" \
  > /etc/apt/sources.list.d/cuda-nvidia.list

# 3.2 ⚠️ 钉住驱动包 —— 否则装工具链可能顺带覆盖刚装好的驱动
cat > /etc/apt/preferences.d/10-nvidia-pin <<'EOF'
Package: nvidia-driver-* nvidia-dkms-* nvidia-kernel-* nvidia-headless-* nvidia-utils-* nvidia-compute-* libnvidia-* cuda-drivers*
Pin: origin developer.download.nvidia.com
Pin-Priority: -1
EOF

apt-get update

# 3.3 ⚠️ 装前 dry-run，确认不会碰驱动
apt-get install -s --no-install-recommends cuda-toolkit-12-8 | grep -i "Inst.*driver"
#   ✅ 期望：只出现 cuda-driver-dev-12-8（头文件包）
#   ❌ 如果出现 nvidia-driver-* / cuda-drivers-*，说明 pin 没生效，先排查再装

apt-get install -y --no-install-recommends cuda-toolkit-12-8
export PATH=/usr/local/cuda-12.8/bin:$PATH
nvcc --version
```

**如果 nvcc 仍找不到**：确认 `ls /usr/local/cuda-12.8/bin/nvcc` 存在，然后把它加到 PATH。

---

## 4. 获取并编译 PrismML 版 llama.cpp

### 4.1 为什么不用官方 llama.cpp

**这一点必须向用户解释清楚**（如果他在意的话）：

- Bonsai 的三元权重需要**自定义 CUDA 内核**（PTQ1_0/PQ2_0、Hadamard 激活旋转）
- 这些内核**只在 `PrismML-Eng/llama.cpp` 的 `prism` 分支**里
- **官方预编译二进制只含 `sm_120a`**（RTX 50 系）——**RTX 20/30/40 系一律用不了，必须源码编译**
- 官方 llama.cpp 会**拒绝加载** PTQ1_0，或**静默加载 Q2_0 并产出乱码**

### 4.2 获取源码

```bash
git clone --depth 1 -b prism https://github.com/PrismML-Eng/llama.cpp.git ./llama.cpp
```

**如果 git 失败**（网络不稳常见），用 tarball：

```bash
curl -fL https://codeload.github.com/PrismML-Eng/llama.cpp/tar.gz/refs/heads/prism \
  -o /tmp/llama-prism.tar.gz
tar -xzf /tmp/llama-prism.tar.gz && mv llama.cpp-prism llama.cpp
```

### 4.3 ⚠️ 验证拿到的是正确分支（别跳过）

```bash
grep -o "PTQ1_0\|PQ2_0" llama.cpp/ggml/src/ggml-common.h | sort -u
#   ✅ 期望输出两行：PQ2_0 和 PTQ1_0
#   ❌ 空输出 = 拿错分支，重来
```

### 4.4 编译

```bash
cd llama.cpp

# 按 §1 记下的计算能力填架构：7.5→75  8.6→86  8.9→89  12.0→120
ARCH=86

cmake -B build -DGGML_CUDA=ON \
  -DCMAKE_CUDA_ARCHITECTURES=$ARCH \
  -DCMAKE_BUILD_TYPE=Release -DLLAMA_CURL=OFF -DLLAMA_BUILD_TESTS=OFF

cmake --build build -j4        # ⚠️ 内存 <16GB 用 -j2，nvcc 很吃内存
```

- 耗时 **30–60 分钟**（CUDA 模板实例是大头），**不要因为长时间没输出就中断**
- 若被 OOM Killer 杀掉：降 `-j`，或加 swap

### 4.5 验证编译产物

```bash
ls -l build/bin/llama-server build/bin/llama-cli build/bin/libggml-cuda.so
```

---

## 5. 下载模型

**目标文件**：`Ternary-Bonsai-2-27B-PTQ1_0.gguf`（约 5.95 GB）
**来源**：`prism-ml/Ternary-Bonsai-2-27B-gguf`

### ⚠️ 关于 "T3 slim" 的说明

如果用户说的是 **"T3 slim"**，**要立刻向他澄清**：

> `bonsai2-27b-t3-slim.q27` 是 **q27 引擎**的私有格式，**llama.cpp 无法加载**。
> llama.cpp 侧的对应物就是 **PTQ1_0** —— 同样是官方定义的「8GB 显卡包」。

### 5.1 下载（三种情况）

**情况 A：能直连 huggingface.co**
```bash
aria2c -j1 -x8 -s8 -k1M -d ./models -o Ternary-Bonsai-2-27B-PTQ1_0.gguf \
  https://huggingface.co/prism-ml/Ternary-Bonsai-2-27B-gguf/resolve/main/Ternary-Bonsai-2-27B-PTQ1_0.gguf
```

**情况 B：huggingface.co 不通（国内常见）→ 用镜像**
```bash
aria2c -j1 -x8 -s8 -k1M -d ./models -o Ternary-Bonsai-2-27B-PTQ1_0.gguf \
  https://hf-mirror.com/prism-ml/Ternary-Bonsai-2-27B-gguf/resolve/main/Ternary-Bonsai-2-27B-PTQ1_0.gguf
```

**情况 C：没有 aria2c**
```bash
apt install -y aria2     # 或
curl -fL --retry 5 -C - <url> -o ./models/Ternary-Bonsai-2-27B-PTQ1_0.gguf
```

### 5.2 三个网络坑（遇到再处理）

| 症状 | 原因 | 处理 |
|---|---|---|
| `Network is unreachable` | `huggingface.co` 解析到 IPv6 但本机无 IPv6 路由 | 换 `hf-mirror.com` |
| `Invalid range header` | ⚠️ **不要用 `-x16`**：hf-mirror 上报尺寸与 CDN 实际不符 | 改用 `-x8` |
| `pip` 极慢 | PyPI 直连慢 | `-i https://pypi.tuna.tsinghua.edu.cn/simple` |

### 5.3 校验

```bash
ls -l ./models/Ternary-Bonsai-2-27B-PTQ1_0.gguf   # 应为 5946648928 字节左右
head -c 4 ./models/Ternary-Bonsai-2-27B-PTQ1_0.gguf    # 应输出 GGUF
```

---

## 6. 首次冒烟测试（**必须先做**，别急着接 Claude Code）

```bash
cd llama.cpp
export LD_LIBRARY_PATH=$PWD/build/bin

./build/bin/llama-cli \
  -m ../models/Ternary-Bonsai-2-27B-PTQ1_0.gguf \
  -ngl 99 -c 4096 -fa on -ctk q4_0 -ctv q4_0 -t 8 \
  -n 512 --no-warmup -st --reasoning-effort medium \
  -p "The capital of France is" < /dev/null
```

**期望**：看到 `[Start thinking] ... [End thinking] ... Paris` 和 `[ Prompt: xx t/s | Generation: xx t/s ]`。

### ⚠️ 最常见的误判：输出为空

**这是推理模型**，默认 `xhigh` 思考，**思考会吃掉输出预算**。若只看到 thinking 没有结论：

| 处理 | 说明 |
|---|---|
| `-n` 调到 **16384 以上** | 最有效 |
| 加 `--reasoning-effort medium` | 缩短思考 |
| 服务端加 `--reasoning-budget N` | 硬限制思考 token |

> ⚠️ `low` 档无效（官方说接近 `xhigh`）；`high` 在部分模板下返回 500，用 `xhigh`/`medium`。

### ⚠️ 另一种误判：`failed to fit params ... abort`

日志里出现这行**不是错误**：
```
common_fit_params: failed to fit params to free device memory: n_gpu_layers already set by user to 99, abort
```
它只是说「用户已显式指定 `-ngl`，跳过自动内存调优」。**属正常输出**，别当成失败。

---

## 7. 启动服务

把仓库里的 `scripts/` 放在合适位置（默认就在仓库内），然后：

```bash
./scripts/start-bonsai.sh start
```

脚本会：前置检查 → 启动 llama-server → 等就绪 → 启动桥接 → 端到端校验。

**可覆盖环境变量**：
```bash
CTX=65536 ./scripts/start-bonsai.sh restart        # 换上下文
PORT=9090 BPORT=9091 ./scripts/start-bonsai.sh restart
```

### 7.1 ⚠️ 关于 `--parallel 1`（脚本已带，但要知道为什么）

`llama-server` 会为**每个 slot** 分配 `rs cache`（线性注意力层的循环状态）。
默认 4 slot 时，**100K 上下文会因 `failed to allocate buffer for rs cache` 启动失败**。

> 这也是「`llama-cli` 能吃 100K 而 `llama-server` 不行」的原因 —— server 多了 slot 开销。

### 7.2 ⚠️ 参数必须按用户机器调整 —— 见 [`docs/PARAM-TUNING.md`](docs/PARAM-TUNING.md)

**不要照抄默认值**。启动前先读 [`docs/PARAM-TUNING.md`](docs/PARAM-TUNING.md)，
按用户的**显存 / 内存 / CPU / 用途**决定参数。要点：

| 要让 AI 做的 | 去哪 |
|---|---|
| 从 `nvidia-smi` / `free` / `nproc` 读出真实资源 | PARAM-TUNING §1 |
| 用 KV 公式估上下文上限 | PARAM-TUNING §2 |
| **实测**逼近真实上限（不靠公式） | PARAM-TUNING §6.1 |
| 按显存/内存/用途选配置档 | PARAM-TUNING §5、§7 |
| 测速度随上下文的衰减并告知用户 | PARAM-TUNING §6.2 |
| 知道哪些参数"调了也没用" | PARAM-TUNING §6.3 |

**三条硬约束**（违反必出问题）：

1. **`-ctk` 与 `-ctv` 必须完全相同** —— 混用会让速度从 51.7 掉到 40.9 t/s、预填充从 888 掉到 303
2. **`-c` > 32768 时必须 `--parallel 1`** —— 否则 `failed to allocate buffer for rs cache`
3. **`CLAUDE_CODE_MAX_CONTEXT_TOKENS` 必须等于服务端 `-c`**

**快速参考**：8GB 卡全 GPU 上限 **102400**（实测，106496 OOM）；
要更长就用 `-nkvo`（KV 放内存），**直接开满 262144**（128K 与 262K 速度相同）。

---

## 8. 接入 Claude Code

### 8.1 ⚠️ 桥接代理是必需的

llama-server **原生支持** Anthropic API（有 `/v1/messages`），但**直连会报错**：

```
Error: Jinja Exception: System message must be at the beginning.
```

**原因**：Claude Code 会把 `role: system` 的消息**混在 messages 数组里**
（实测 `roles=['user','system']`），而 Bonsai 的 Qwen 系 chat template **要求 system 置顶**。

**解决**：`scripts/anthropic-bridge.py` 会把所有 system 内容合并到顶层字段并置顶。
（`start-bonsai.sh` 会自动启动它。）

### 8.2 配置 Claude Code

用 `scripts/claude-settings-bonsai.json`，**不要改全局 `~/.claude/settings.json`**：

```bash
claude --settings <仓库>/scripts/claude-settings-bonsai.json
```

**关键项**（详见文件内注释）：
- `ANTHROPIC_BASE_URL` 指向**桥接端口 8081**（⚠️ 不是 8080）
- `CLAUDE_CODE_MAX_CONTEXT_TOKENS` 与服务端 `-c` 一致
- `CLAUDE_CODE_MAX_OUTPUT_TOKENS=16384` —— 默认 32000 会挤占 prompt 预算

**算式**：`可用 prompt 预算 = n_ctx − MAX_OUTPUT_TOKENS = 102400 − 16384 = 86016`

### 8.3 验证

```bash
claude --settings ... -p "只回复两个字：正常"
```

---

## 9. 故障速查

| 症状 | 原因 | 处理 |
|---|---|---|
| `libggml-cuda.so: cannot open` | 没设 `LD_LIBRARY_PATH` | 设成 `<llama.cpp>/build/bin` |
| `unknown model architecture` | 用了官方 llama.cpp | 换 PrismML 的 prism 分支 |
| 加载成功但输出乱码 | 官方 llama.cpp 静默加载 Q2_0 | 同上 |
| `cudaMalloc failed: out of memory` | 上下文 + 权重 > 显存 | 降 `-c`，或 `-nkvo`。**先看报错里的 `allocating X MiB`** |
| `failed to allocate buffer for rs cache` | 多 slot | 加 `--parallel 1` |
| `NVRM: GPU ... already bound to nouveau` | 没先停显示栈 | 见 §2.3 |
| 输出为空 / 只有思考 | 输出预算不足 | `-n 16384+`，`reasoning_effort=medium` |
| `System message must be at the beginning` | 直连 8080 | `ANTHROPIC_BASE_URL` 改 **8081** |
| `[claude-code:unrecognized_model]` | 它不认识模型名 | **仅提示，不影响** |
| GPU 报 `illegal memory access` | 用 PrismML fork 跑**非 Bonsai** 的 MoE | 该 fork 只为 Bonsai 定制，换模型要换引擎 |

### ⚠️ 操作安全

**不要用 `pkill -f "<关键字>"`** —— 若命令本身含该关键字，可能把执行它的 shell 也杀掉。
安全做法：

```bash
kill $(ss -ltnp 2>/dev/null | grep ':8080' | grep -oP 'pid=\K[0-9]+')
# 或
kill $(nvidia-smi --query-compute-apps=pid --format=csv,noheader)
```

---

## 10. 完成标准

全部打勾才算部署成功：

- [ ] `nvidia-smi` 正常，`lsmod` 无 nouveau
- [ ] `nvcc --version` 可用
- [ ] `llama.cpp` 是 prism 分支，含 `PTQ1_0` / `PQ2_0`
- [ ] 模型文件约 5.95GB，`head -c4` 输出 `GGUF`
- [ ] 冒烟测试产出了实际回答（非空）
- [ ] `./scripts/start-bonsai.sh start` 输出 `✓ 全部就绪`
- [ ] `curl http://127.0.0.1:8080/health` 返回 `{"status":"ok"}`
- [ ] 桥接 8081 的 `/v1/messages` 返回正常 JSON
- [ ] `claude --settings ... -p "hi"` 有正常回复

---

## 11. 部署完成后，告诉用户这些

1. **启动/停止**：`./scripts/start-bonsai.sh start|stop|status`
2. **接入 Claude Code**：`claude --settings <仓库>/scripts/claude-settings-bonsai.json`
3. **性能预期**（RTX 3070 8GB 实测）：
   - 4K 上下文约 **51 t/s**，100K 约 **39 t/s**
   - 预填充约 888 t/s
4. **已知弱点**（让用户有预期）：
   - 多轮工具循环中，若工具返回与预期**矛盾**，它不会止损而是反复重试
     → 长 agent 任务建议加循环检测与步数上限
   - 开放式/歧义问题容易陷入思考死循环
   - 速度比云端慢（简单任务约 4 分钟，复杂任务 8–30 分钟）
