# bonsai-ai-deploy · Deployment Instructions (for AI agents)

**English** | [中文](AI-DEPLOY.md)

> **You are an AI coding assistant. The user asked you to deploy Bonsai-2-27B on this machine and
> wire it into Claude Code.**
>
> This file is an **operating guide**, not a script. Work through it in order, **verify each step**,
> and follow the "if … then …" branches. User machines differ — **do not blindly copy commands**;
> read the actual output first, then decide.

---

## 0. Before you start: confirm three things with the user

| Confirm | Why |
|---|---|
| **≥ 20 GB free disk** | Model 5.95 GB + build artifacts + CUDA ≈ 15 GB |
| **Network access to GitHub and HuggingFace (or a mirror)** | To clone the source and download the model |
| **The machine can be rebooted** (if no NVIDIA driver is installed) | The driver needs a reboot to take effect |

> If the user is bandwidth-sensitive, tell them up front: **~6 GB for the model, ~5 GB for the CUDA Toolkit**.

---

## 1. Probe the environment (don't skip, don't guess)

```bash
# GPU: model, VRAM, driver
nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv 2>&1

# Which driver the card is currently bound to
lspci -nnk | grep -iA3 "vga\|3d"

# Kernel modules
lsmod | grep -E "^nouveau|^nvidia"

# System resources
free -h ; df -h / ; nproc ; uname -r ; lsb_release -a 2>/dev/null

# Available tools
for t in nvidia-smi nvcc gcc cmake git curl; do
  printf "%-12s %s\n" "$t" "$(command -v $t || echo 'MISSING')"
done

# Is CUDA already installed?
ls -d /usr/local/cuda* 2>/dev/null
```

### Route based on what you find

| Observation | Meaning | Go to |
|---|---|---|
| `nvidia-smi` prints normally | Driver OK | → §3 |
| `nvidia-smi: command not found` and `lsmod` shows `nouveau` | No driver; nouveau holds the card | → §2 |
| `nvidia-smi` says `couldn't communicate with NVIDIA driver` | Driver installed but not active | → §2 (likely needs a reboot) |
| `/usr/local/cuda-12.8/bin/nvcc` exists | CUDA already installed | → §4 |
| VRAM < 7 GB | Small card | ⚠️ Note it; reduce `CTX` later |

**Record these numbers — you'll need them**:
- VRAM (MiB) — determines the context ceiling
- GPU compute capability (`nvidia-smi --query-gpu=compute_cap --format=csv,noheader`, e.g. `8.6`) — determines the CUDA arch
- RAM (GB)

---

## 2. Install the NVIDIA driver (only if §1 says you must)

**Target**: version 580 or newer.

### 2.1 Install

```bash
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y --no-install-recommends dkms nvidia-headless-580 build-essential

# Verify the kernel module was built and installed
dkms status
ls /lib/modules/$(uname -r)/updates/dkms/     # expect nvidia.ko.zst etc.
```

**If `dkms status` shows failed**:
- Read `/var/lib/dkms/nvidia/*/build/make.log`
- Common cause: missing `linux-headers-$(uname -r)` → `apt install linux-headers-$(uname -r)`
- Then re-run: `dkms autoinstall`

### 2.2 Blacklist nouveau

```bash
cat > /etc/modprobe.d/blacklist-nouveau.conf <<'EOF'
blacklist nouveau
options nouveau modeset=0
EOF
update-initramfs -u
```

### 2.3 ⚠️⚠️ The step that goes wrong most often — stop the display stack first

**This is the biggest trap in the whole procedure. Understand it before acting.**

On some machines the **graphical desktop is using this GPU through nouveau**. Running
`modprobe -r nouveau` in that state wedges `Xorg` / `gnome-shell` in **uninterruptible (D) state**
— they cannot be killed, nouveau cannot be unloaded, and nvidia can never take over.
**The only recovery is a reboot.**

**Correct order**:

```bash
# 1) Check whether a desktop is running
systemctl is-active gdm lightdm sddm 2>/dev/null
pgrep -a Xorg

# 2) If so, stop it and confirm Xorg is really gone
systemctl stop gdm        # substitute the actual display manager
sleep 2
pgrep -a Xorg             # MUST be empty!

# 3) Only now unload nouveau
modprobe -r nouveau

# 4) Load nvidia
modprobe nvidia && modprobe nvidia_uvm && modprobe nvidia_drm
nvidia-smi
```

**If the user is working over SSH / remote desktop**, stopping the display manager won't affect them.
If they're at a **local graphical session**, warn them: "the screen will go black for a moment —
that's expected, don't power off."

### 2.4 Auto-load at boot (important trade-off)

```bash
cat > /etc/modules-load.d/nvidia.conf <<'EOF'
nvidia
nvidia_uvm
EOF
```

> **Deliberately not loading `nvidia_drm`**: otherwise the desktop re-grabs the nvidia card and
> reproduces the §2.3 accident. Compute workloads don't need `nvidia_drm`.

### 2.5 If a reboot is needed

If `modprobe nvidia` reports `No such device`, or `nvidia-smi` still fails:
**tell the user a reboot is required**, then re-check from §1. Don't keep retrying the module load.

---

## 3. Install the CUDA Toolkit

```bash
# 3.1 Add the official repo (replace ubuntu2404 with your release, e.g. ubuntu2204)
mkdir -p /usr/share/keyrings
curl -fsSL https://developer.nvidia.com/compute/cuda/repos/ubuntu2404/x86_64/cuda-archive-keyring.gpg \
  -o /usr/share/keyrings/cuda-archive-keyring.gpg

echo "deb [signed-by=/usr/share/keyrings/cuda-archive-keyring.gpg] \
https://developer.download.nvidia.com/compute/cuda/repos/ubuntu2404/x86_64/ /" \
  > /etc/apt/sources.list.d/cuda-nvidia.list

# 3.2 ⚠️ Pin the driver packages — otherwise installing the toolkit may overwrite the driver you just set up
cat > /etc/apt/preferences.d/10-nvidia-pin <<'EOF'
Package: nvidia-driver-* nvidia-dkms-* nvidia-kernel-* nvidia-headless-* nvidia-utils-* nvidia-compute-* libnvidia-* cuda-drivers*
Pin: origin developer.download.nvidia.com
Pin-Priority: -1
EOF

apt-get update

# 3.3 ⚠️ Dry-run first, to confirm it won't touch the driver
apt-get install -s --no-install-recommends cuda-toolkit-12-8 | grep -i "Inst.*driver"
#   ✅ Expected: only cuda-driver-dev-12-8 (a headers package)
#   ❌ If nvidia-driver-* / cuda-drivers-* appear, the pin didn't take — investigate before installing

apt-get install -y --no-install-recommends cuda-toolkit-12-8
export PATH=/usr/local/cuda-12.8/bin:$PATH
nvcc --version
```

**If `nvcc` is still not found**: confirm `ls /usr/local/cuda-12.8/bin/nvcc` exists, then add it to PATH.

---

## 4. Get and build the PrismML fork of llama.cpp

### 4.1 Why not upstream llama.cpp

**Explain this clearly to the user if they care**:

- Bonsai's ternary weights need **custom CUDA kernels** (PTQ1_0/PQ2_0, Hadamard activation rotation)
- Those kernels exist **only in the `prism` branch of `PrismML-Eng/llama.cpp`**
- **The official prebuilt binaries contain only `sm_120a`** (RTX 50 series) — **RTX 20/30/40 cards
  cannot use them and must build from source**
- Upstream llama.cpp will either **refuse to load** PTQ1_0, or **silently load Q2_0 and produce garbage**

### 4.2 Get the source

```bash
git clone --depth 1 -b prism https://github.com/PrismML-Eng/llama.cpp.git ./llama.cpp
```

**If git fails** (common with unstable networks), use the tarball:

```bash
curl -fL https://codeload.github.com/PrismML-Eng/llama.cpp/tar.gz/refs/heads/prism \
  -o /tmp/llama-prism.tar.gz
tar -xzf /tmp/llama-prism.tar.gz && mv llama.cpp-prism llama.cpp
```

### 4.3 ⚠️ Verify you have the right branch (don't skip)

```bash
grep -o "PTQ1_0\|PQ2_0" llama.cpp/ggml/src/ggml-common.h | sort -u
#   ✅ Expected: two lines, PQ2_0 and PTQ1_0
#   ❌ Empty output = wrong branch, start over
```

### 4.4 Build

```bash
cd llama.cpp

# Fill in the arch from the compute capability recorded in §1:
#   7.5→75   8.6→86   8.9→89   12.0→120
ARCH=86

cmake -B build -DGGML_CUDA=ON \
  -DCMAKE_CUDA_ARCHITECTURES=$ARCH \
  -DCMAKE_BUILD_TYPE=Release -DLLAMA_CURL=OFF -DLLAMA_BUILD_TESTS=OFF

cmake --build build -j4        # ⚠️ use -j2 with <16GB RAM; nvcc is memory-hungry
```

- Takes **30–60 minutes** (CUDA template instances dominate). **Don't interrupt it for being quiet.**
- If the OOM killer strikes: lower `-j`, or add swap

### 4.5 Verify the artifacts

```bash
ls -l build/bin/llama-server build/bin/llama-cli build/bin/libggml-cuda.so
```

---

## 5. Download the model

**Target file**: `Ternary-Bonsai-2-27B-PTQ1_0.gguf` (~5.95 GB)
**Source**: `prism-ml/Ternary-Bonsai-2-27B-gguf`

### ⚠️ About "T3 slim"

If the user says **"T3 slim"**, **clarify immediately**:

> `bonsai2-27b-t3-slim.q27` is the **q27 engine**'s private format; **llama.cpp cannot load it**.
> The llama.cpp equivalent is **PTQ1_0** — also an official "8 GB card" pack.

### 5.1 Download (three cases)

**Case A: direct access to huggingface.co**
```bash
aria2c -j1 -x8 -s8 -k1M -d ./models -o Ternary-Bonsai-2-27B-PTQ1_0.gguf \
  https://huggingface.co/prism-ml/Ternary-Bonsai-2-27B-gguf/resolve/main/Ternary-Bonsai-2-27B-PTQ1_0.gguf
```

**Case B: huggingface.co unreachable (common in China) → use a mirror**
```bash
aria2c -j1 -x8 -s8 -k1M -d ./models -o Ternary-Bonsai-2-27B-PTQ1_0.gguf \
  https://hf-mirror.com/prism-ml/Ternary-Bonsai-2-27B-gguf/resolve/main/Ternary-Bonsai-2-27B-PTQ1_0.gguf
```

**Case C: no aria2c**
```bash
apt install -y aria2     # or
curl -fL --retry 5 -C - <url> -o ./models/Ternary-Bonsai-2-27B-PTQ1_0.gguf
```

### 5.2 Three network pitfalls (handle as they arise)

| Symptom | Cause | Fix |
|---|---|---|
| `Network is unreachable` | `huggingface.co` resolves to IPv6 but the host has no IPv6 route | Use `hf-mirror.com` |
| `Invalid range header` | ⚠️ **Don't use `-x16`**: hf-mirror's reported size disagrees with the CDN's actual size | Use `-x8` |
| `pip` extremely slow | Direct PyPI is slow | `-i https://pypi.tuna.tsinghua.edu.cn/simple` |

### 5.3 Verify

```bash
ls -l ./models/Ternary-Bonsai-2-27B-PTQ1_0.gguf   # should be ~5946648928 bytes
head -c 4 ./models/Ternary-Bonsai-2-27B-PTQ1_0.gguf    # should print GGUF
```

---

## 6. First smoke test (**do this before wiring up Claude Code**)

```bash
cd llama.cpp
export LD_LIBRARY_PATH=$PWD/build/bin

./build/bin/llama-cli \
  -m ../models/Ternary-Bonsai-2-27B-PTQ1_0.gguf \
  -ngl 99 -c 4096 -fa on -ctk q4_0 -ctv q4_0 -t 8 \
  -n 512 --no-warmup -st --reasoning-effort medium \
  -p "The capital of France is" < /dev/null
```

**Expect**: `[Start thinking] ... [End thinking] ... Paris` plus `[ Prompt: xx t/s | Generation: xx t/s ]`.

### ⚠️ The most common false alarm: empty output

**This is a reasoning model.** It thinks by default (`xhigh`), and **thinking consumes the output budget**.
If you see thinking but no conclusion:

| Fix | Note |
|---|---|
| Raise `-n` to **16384 or more** | Most effective |
| Add `--reasoning-effort medium` | Shortens thinking |
| Server-side `--reasoning-budget N` | Hard cap on thinking tokens |

> ⚠️ `low` doesn't help (the vendor says it behaves close to `xhigh`);
> `high` returns HTTP 500 on some templates — use `xhigh`/`medium`.

### ⚠️ Another false alarm: `failed to fit params ... abort`

This line in the log is **not an error**:
```
common_fit_params: failed to fit params to free device memory: n_gpu_layers already set by user to 99, abort
```
It only means "the user set `-ngl` explicitly, skipping automatic memory tuning." **Normal output.**

---

## 7. Start the service

Place the repo's `scripts/` where you like (by default inside the repo), then:

```bash
./scripts/start-bonsai.sh start
```

The script runs: pre-flight checks → start llama-server → wait for readiness → start the bridge →
end-to-end verification.

**Overridable environment variables**:
```bash
CTX=65536 ./scripts/start-bonsai.sh restart        # different context
PORT=9090 BPORT=9091 ./scripts/start-bonsai.sh restart
```

### 7.1 ⚠️ About `--parallel 1` (already in the script, but you should know why)

`llama-server` allocates an `rs cache` (recurrent state of the linear-attention layers) **per slot**.
With the default 4 slots, **a 100K context fails to start**:

```
failed to allocate buffer for rs cache
```

> This is also why "`llama-cli` handles 100K but `llama-server` doesn't" — the server has extra slot overhead.

### 7.2 ⚠️ Tune parameters to the user's machine — see [`docs/PARAM-TUNING.md`](docs/PARAM-TUNING.md)

**Do not copy the defaults.** Before starting, read [`docs/PARAM-TUNING.md`](docs/PARAM-TUNING.md)
and choose parameters based on the user's **VRAM / RAM / CPU / use case**. Key points:

| Task for the AI | Where |
|---|---|
| Read real resources from `nvidia-smi` / `free` / `nproc` | PARAM-TUNING §1 |
| Estimate the context ceiling with the KV formula | PARAM-TUNING §2 |
| **Empirically** approach the real ceiling (not by formula) | PARAM-TUNING §6.1 |
| Pick a config tier by VRAM/RAM/use case | PARAM-TUNING §5, §7 |
| Measure the speed-vs-context decay and tell the user | PARAM-TUNING §6.2 |
| Know which flags "make no difference" | PARAM-TUNING §6.3 |

**Three hard constraints** (violating them always causes problems):

1. **`-ctk` and `-ctv` must be identical** — mixing drops generation from 51.7 to 40.9 tok/s and
   prefill from 888 to 303
2. **`-c` > 32768 requires `--parallel 1`** — otherwise `failed to allocate buffer for rs cache`
3. **`CLAUDE_CODE_MAX_CONTEXT_TOKENS` must equal the server's `-c`**

**Quick reference**: on an 8 GB card the full-GPU ceiling is **102400** (measured; 106496 OOMs).
For longer, use `-nkvo` (KV in system RAM) and **go straight to 262144** (128K and 262K run at the same speed).

---

## 8. Wire up Claude Code

### 8.1 ⚠️ The bridge proxy is mandatory

llama-server **natively supports** the Anthropic API (it has `/v1/messages`), but **connecting directly
fails**:

```
Error: Jinja Exception: System message must be at the beginning.
```

**Cause**: Claude Code mixes `role: system` messages **into the `messages` array**
(measured: `roles=['user','system']`), while Bonsai's Qwen-family chat template **requires `system` first**.

**Fix**: `scripts/anthropic-bridge.py` merges all system content into the top-level field and moves it
to the front. (`start-bonsai.sh` starts it automatically.)

### 8.2 Configure Claude Code

Use `scripts/claude-settings-bonsai.json`, and **do not modify the global `~/.claude/settings.json`**:

```bash
claude --settings <repo>/scripts/claude-settings-bonsai.json
```

**Key entries** (see the comments in the file):
- `ANTHROPIC_BASE_URL` points at the **bridge port 8081** (⚠️ not 8080)
- `CLAUDE_CODE_MAX_CONTEXT_TOKENS` must match the server's `-c`
- `CLAUDE_CODE_MAX_OUTPUT_TOKENS=16384` — the default 32000 squeezes the prompt budget

**Formula**: `usable prompt budget = n_ctx − MAX_OUTPUT_TOKENS = 102400 − 16384 = 86016`

### 8.3 Verify

```bash
claude --settings ... -p "Reply with exactly: OK"
```

---

## 9. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `libggml-cuda.so: cannot open` | `LD_LIBRARY_PATH` not set | Set it to `<llama.cpp>/build/bin` |
| `unknown model architecture` | Using upstream llama.cpp | Switch to the PrismML `prism` branch |
| Loads but output is garbage | Upstream llama.cpp silently loaded Q2_0 | Same as above |
| `cudaMalloc failed: out of memory` | Context + weights exceed VRAM | Lower `-c`, or use `-nkvo`. **Read the `allocating X MiB` line first** |
| `failed to allocate buffer for rs cache` | Multiple slots | Add `--parallel 1` |
| `NVRM: GPU ... already bound to nouveau` | Display stack wasn't stopped | See §2.3 |
| Empty output / thinking only | Insufficient output budget | `-n 16384+`, `reasoning_effort=medium` |
| `System message must be at the beginning` | Connected directly to 8080 | Point `ANTHROPIC_BASE_URL` at **8081** |
| `[claude-code:unrecognized_model]` | It doesn't know the model name | **Informational only; harmless** |
| GPU reports `illegal memory access` | Running a **non-Bonsai** MoE on the PrismML fork | That fork is Bonsai-specific; use another engine for other models |

### ⚠️ Operational safety

**Never use `pkill -f "<keyword>"`** — if the command itself contains that keyword
(e.g. `pkill -f "llama-server"`), it can kill the shell running it. Safe alternatives:

```bash
kill $(ss -ltnp 2>/dev/null | grep ':8080' | grep -oP 'pid=\K[0-9]+')
# or
kill $(nvidia-smi --query-compute-apps=pid --format=csv,noheader)
```

---

## 10. Definition of done

All boxes ticked = deployment succeeded:

- [ ] `nvidia-smi` works, `lsmod` shows no nouveau
- [ ] `nvcc --version` works
- [ ] `llama.cpp` is the prism branch and contains `PTQ1_0` / `PQ2_0`
- [ ] The model file is ~5.95 GB and `head -c4` prints `GGUF`
- [ ] The smoke test produced an actual answer (not empty)
- [ ] `./scripts/start-bonsai.sh start` prints `✓ 全部就绪` (all ready)
- [ ] `curl http://127.0.0.1:8080/health` returns `{"status":"ok"}`
- [ ] The bridge on 8081 returns valid JSON from `/v1/messages`
- [ ] `claude --settings ... -p "hi"` gets a normal reply

---

## 11. After deployment, tell the user

1. **Start/stop**: `./scripts/start-bonsai.sh start|stop|status`
2. **Claude Code**: `claude --settings <repo>/scripts/claude-settings-bonsai.json`
3. **Expected performance** (measured on RTX 3070 8 GB):
   - ~**51 tok/s** at 4K context, ~**39 tok/s** at 100K
   - Prefill ~888 tok/s
4. **Known weaknesses** (set expectations):
   - In multi-turn tool loops, when a tool result **contradicts** expectations, it does not stop and
     report — it retries → add loop detection and a step cap for long agent tasks
   - Open-ended / ambiguous questions can send it into a reasoning loop
   - Slower than cloud (simple tasks ~4 min, complex tasks 8–30 min)
