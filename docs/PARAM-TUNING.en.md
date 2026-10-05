# Parameter Tuning: adapting to the machine and the use case

**English** | [中文](PARAM-TUNING.md)

> For an AI agent to decide llama.cpp parameters **based on the user's actual machine**, rather than
> copying defaults. Every number below was measured on an RTX 3070 8 GB — **the formulas and decision
> logic transfer to other hardware**.

---

## 1. Measure three things first (don't guess)

```bash
# 1) VRAM (MiB) — determines the context ceiling
nvidia-smi --query-gpu=name,memory.total --format=csv,noheader,nounits

# 2) RAM (GB) — determines whether -nkvo / longer contexts are viable
free -g | awk '/^Mem:/{print $2}'

# 3) CPU cores — determines -t
nproc
```

**Write these three numbers down.** All decisions below depend on them.

---

## 2. VRAM budget formula (compute before setting `-c`)

Actual allocation on an 8 GB card (measured, RTX 3070):

| Item | Usage |
|---|---|
| Model weights (PTQ1_0) | **≈ 5.53 GiB** |
| CUDA context + compute buffers | **≈ 0.5 GiB** |
| KV cache | see formula below |
| **Left for KV** | **≈ 2.2 GiB** |

### KV cache size formula

This model: **64 layers = 16 full-attention + 48 linear-attention**, `n_kv_heads=4`, `head_dim=256`.

```
KV_bytes ≈ 2 × n_kv_heads × head_dim × bytes_per_element × full_attention_layers × ctx
         = 2 × 4 × 256 × bytes_per_element × 16 × ctx
         = 32768 × (bytes_per_element) × ctx
```

| KV quantization | bytes/element | Simplified | At 100K |
|---|---|---|---|
| `f16` | 2 | `65536 × ctx` | 6.25 GiB |
| **`q4_0`** | **0.5625** | `18432 × ctx` | **1.76 GiB** |

> ⚠️ Measured: at 128K + q4_0 the engine requests **2304 MiB** (more than the formula suggests — alignment/overhead).
> **Trust the `allocating X MiB` number in the `cudaMalloc` error.**

### Implication

```bash
# 8 GB card
Left for KV ≈ 2.2 GiB
With q4_0, max ctx ≈ 2.2 GiB / 18432 ≈ 125K   ← theoretical
Measured ceiling = 102400 (106496 OOMs)        ← actual (extra overhead)
```

**Conclusion: don't set the ceiling from the formula — approach it empirically** (see §6).

---

## 3. Flag reference

| Flag | Purpose | Default | How to set | When |
|---|---|---|---|---|
| `-ngl N` | Layers on GPU | 0 | **99 = all on GPU**; lower if it doesn't fit | VRAM < model size |
| `-c N` | Context length | 4096 | Per §2, then §6 | Always |
| `-fa on` | Flash Attention | auto | **Set explicitly to `on`** | Always |
| `-ctk` / `-ctv` | K / V cache quantization | f16 | **Both must be the same** | Context > 32K → use q4_0 |
| `--parallel N` | Concurrent slots | 4 | **Set to 1** | Server mode, context > 32K → **required** |
| `-t N` | CPU threads | auto | **No effect** when GPU-bound | Only matters with `-ngl 0` |
| `-b` / `-ub` | Logical/physical batch | 2048/512 | Measured: 256–1024 makes no difference | Generally leave alone |
| `-nkvo` | KV in system RAM | off | Enable for very long contexts | When VRAM can't hold the KV |
| `--jinja` | Use the model's own chat template | off | **Required** | Always |
| `--chat-template-kwargs` | Pass template params | — | `{"reasoning_effort":"medium"}` | Required for reasoning models |

---

## 4. Three "must be consistent" constraints (violations cause problems)

### 4.1 K and V quantization must match

Measured (same model, same context):

| K / V | Generation | Prefill |
|---|---|---|
| `q4_0` / `q4_0` | **51.7 tok/s** | 888 tok/s |
| `f16` / `f16` | 52.3 tok/s | 894 tok/s |
| **`q4_0` / `f16`** | **40.9 tok/s** ❌ | **303 tok/s** ❌ |
| **`f16` / `q4_0`** | **40.8 tok/s** ❌ | **240 tok/s** ❌ |

**Mixing them tanks both generation and prefill.**

### 4.2 `-parallel` vs context length

`llama-server` allocates an `rs cache` (recurrent state of the linear-attention layers) **per slot**.
With the default `--parallel 4`, **a 100K context fails to start**:

```
failed to allocate buffer for rs cache
```

**Rules**:
- `-c` ≤ 32768 → the default works, but 1 uses less memory
- `-c` > 32768 → **must be `--parallel 1`**

> This is also why "`llama-cli` handles 100K but `llama-server` doesn't" — extra slot overhead.

### 4.3 `CLAUDE_CODE_MAX_CONTEXT_TOKENS` must equal the server's `-c`

```
Usable prompt budget = n_ctx − CLAUDE_CODE_MAX_OUTPUT_TOKENS
```

Example: `102400 − 16384 = 86016`. Mismatches cause either requests exceeding the server limit
(declared too high) or wasted context (declared too low).

---

## 5. Recommended configs by machine

### By VRAM

| VRAM | `-ngl` | `-c` | KV | Generation (measured/extrapolated) |
|---|---|---|---|---|
| **8 GB** | 99 | **102400** | q4_0 | ~39 tok/s |
| 10 GB | 99 | 131072 | q4_0 | ~35 tok/s |
| 12 GB | 99 | 196608 | q4_0 | ~30 tok/s |
| 16 GB | 99 | **262144** (native max) | q4_0 | ~30 tok/s |
| 6 GB | 99 | 65536 | q4_0 | ~40 tok/s |
| **< 6 GB** | lower `-ngl` | by remaining VRAM | q4_0 | degrades sharply |

> The `-c` above is the **full-GPU ceiling**. To go beyond it, see §5.3.

### 5.1 By RAM (affects whether `-nkvo` is viable)

| RAM | Longest context with `-nkvo` | Note |
|---|---|---|
| 8 GB | Don't use `-nkvo` | Model + KV won't fit |
| 16 GB | ≤ 131072 | KV(q4_0) ≈ 2.3 GiB |
| **48 GB** | **262144** | Measured working |
| 64 GB+ | 262144 | |

### 5.2 By CPU (only matters when `-ngl` < 99)

| Cores | Note |
|---|---|
| ≥ 8 | `-t 8` is fine (GPU is the bottleneck) |
| 4–8 | `-t $(nproc)` |
| < 4 | Don't lower `-ngl`; pure CPU will be painfully slow |

### 5.3 Going beyond the full-GPU ceiling: three measured approaches

Example: 128K on an 8 GB card.

| Approach | Context | Generation | Verdict |
|---|---|---|---|
| `-ngl 58 -c 131072` (6 layers on CPU) | 128K | **7.5 tok/s** | ❌ Not recommended |
| **`-ngl 99 -nkvo -c 131072`** (KV in RAM) | 128K | **6.7 tok/s** | |
| **`-ngl 99 -nkvo -c 262144`** | **262K** | **10.9 tok/s** | ✅ **Go all the way if you use it** |

> ⚠️ **Key finding**: under `-nkvo`, 128K and 262K run at **nearly identical speed** (the bottleneck is
> PCIe transfer of KV, not length). So **either don't use `-nkvo`, or go straight to the model's native max**.

---

## 6. Empirical tuning workflow (use llama-bench to approach the real ceiling)

**Don't set the context ceiling from a formula — measure it.**

### 6.1 Find the context ceiling (binary search)

```bash
LLAMA=./llama.cpp/build/bin/llama-cli
MODEL=./models/Ternary-Bonsai-2-27B-PTQ1_0.gguf
export LD_LIBRARY_PATH=./llama.cpp/build/bin

# Repeat to find the boundary between "works" and "OOM"
$LLAMA -m $MODEL -ngl 99 -c 131072 --parallel 1 -fa on -ctk q4_0 -ctv q4_0 \
       -n 8 --no-warmup -st -p "hi" < /dev/null 2>&1 \
  | grep -oE "allocating [0-9.]+ MiB|out of memory|Generation: [0-9.]+ t/s"
```

- `Generation: xx t/s` → it works
- `allocating X MiB ... out of memory` → over the limit; **halve `-c` and try again**, approaching the boundary

> The `allocating X MiB` figure **is the KV memory actually required** — the most accurate way to
> back out the ceiling.

### 6.2 Measure generation speed at different depths

```bash
./llama.cpp/build/bin/llama-bench -m $MODEL \
  -ngl 99 -fa 1 -ctk q4_0 -ctv q4_0 -ub 512 -t 8 \
  -d 0,16384,32768,65536 -n 128 -r 2 -p 0
```

`-d` is the context depth. Measured curve (8 GB card):

| Depth | Generation |
|---|---|
| 0 | 51.5 tok/s |
| 16K | 42.5 tok/s |
| 32K | 35.7 tok/s |
| 64K | 26.7 tok/s |

**Tell the user about this decay** so they know "longer context = slower" is expected.

### 6.3 Verify the flags are already saturated (avoid wasted effort)

Measured — **these flags "make no difference" on this model**:

| Flag | Measured difference | Verdict |
|---|---|---|
| `-t 4/6/8` | 51.67 / 51.61 / 51.52 | No difference (GPU-bound) |
| `-ub 256/512/1024` | < 1% | Leave alone |
| `GGML_CUDA_FORCE_MMQ=1` | No gain | Don't add it |

**This model is memory-bandwidth-bound. Don't over-tune.**

---

## 7. Recommended presets by use case

| Use case | Context | Key flags | Rationale |
|---|---|---|---|
| **Claude Code** | **102400** | `--parallel 1`, `MAX_OUTPUT_TOKENS=16384` | Agent work is multi-turn; **speed matters more than an ultra-long context** |
| General chat | 32768 | defaults | Balanced |
| Long-document QA | 131072 | `-nkvo` | Accept the speed drop |
| Very long context (a whole book) | 262144 | `-nkvo` | Same speed as 128K — no downside |
| Fastest response | 4096 | defaults | 51 tok/s |

### 7.1 Extra tuning for Claude Code

```bash
# Server: a reasoning model needs a generous output budget
-n 16384                    # too small → empty answers (thinking eats the budget)
--reasoning-effort medium   # the default xhigh is too verbose

# Claude Code side (in settings.json)
"CLAUDE_CODE_MAX_CONTEXT_TOKENS": "102400",   # = the server's -c
"CLAUDE_CODE_MAX_OUTPUT_TOKENS": "16384"      # works together with the server
```

**A too-small `-n` is the most common "the model is broken" false alarm**: you see thinking but no
conclusion — the budget ran out. **Suspect the budget first, the model second.**

---

## 8. Two things to do after tuning

### 8.1 Write the values back into the script

Put the measured optimum into `scripts/start-bonsai.sh`:

```bash
CTX=${CTX:-<your measured ceiling>}      # e.g. 102400
# leave -c "$CTX" --parallel 1 in the launch flags as-is
```

Or override at runtime:
```bash
CTX=131072 ./scripts/start-bonsai.sh restart
```

### 8.2 Tell the user

| Tell them | Why |
|---|---|
| The measured context ceiling | They'll want to know "how large can it go" |
| The speed-vs-context decay curve | So "it got slower" isn't mistaken for breakage |
| The relationship between `-n` and `reasoning_effort` | So "no output" isn't mistaken for a broken model |
| Whether `-nkvo` is in use and its speed cost | So they can weigh "long context vs speed" knowingly |

---

## 9. Quick self-check

- [ ] `-fa on` set explicitly
- [ ] `-ctk` and `-ctv` **identical**
- [ ] `--parallel 1` set when `-c` > 32768
- [ ] `-c` is **measured**, not computed from a formula
- [ ] `-n` ≥ 16384 (reasoning model)
- [ ] `reasoning_effort` set in `--chat-template-kwargs`
- [ ] `CLAUDE_CODE_MAX_CONTEXT_TOKENS` == the server's `-c`
- [ ] After startup, `nvidia-smi` shows sensible VRAM usage (nothing left over, nothing blown)
- [ ] Recorded the real speed with `llama-bench` and told the user
