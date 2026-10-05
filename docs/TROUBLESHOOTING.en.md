# Troubleshooting

**English** | [中文](TROUBLESHOOTING.md)

> The detailed version is in [`../AI-DEPLOY.en.md`](../AI-DEPLOY.en.md) §9. This is the quick list.

## By symptom

| Symptom | Cause | Fix |
|---|---|---|
| `libggml-cuda.so: cannot open shared object file` | `LD_LIBRARY_PATH` not set | `export LD_LIBRARY_PATH=<llama.cpp>/build/bin` |
| `unknown model architecture` | Using upstream llama.cpp | Switch to the **PrismML-Eng/llama.cpp** `prism` branch |
| Loads but output is all garbage | Upstream llama.cpp silently loaded Q2_0, lacking the Hadamard activation runtime | Same as above |
| `cudaMalloc failed: out of memory` | Context + weights exceed VRAM | Lower `-c`, or add `-nkvo`. **Read the `allocating X MiB` number first** |
| `failed to allocate buffer for rs cache` | llama-server allocating recurrent-state cache per slot | Add **`--parallel 1`** |
| `NVRM: GPU ... already bound to nouveau` | Display stack not stopped before unloading nouveau | `systemctl stop gdm`, confirm `pgrep -a Xorg` is empty, then `modprobe -r nouveau` |
| **Empty output / thinking but no conclusion** | A reasoning model's thinking consumed the output budget | `-n 16384` or more; `--reasoning-effort medium` |
| `System message must be at the beginning` | Claude Code connected directly to 8080 | `ANTHROPIC_BASE_URL` must point at the bridge, **8081** |
| `[claude-code:unrecognized_model]` | It doesn't recognize the name `bonsai` | **Informational only; harmless** |
| **`401 authentication_error`** | Server has `API_KEY` set; caller sent none or the wrong one | Client adds `-H 'x-api-key: <key>'`; locally, also sync `ANTHROPIC_API_KEY` in `claude-settings-bonsai.json` |
| **Other machines can't connect, localhost is fine** | Still bound to `127.0.0.1`, or a firewall is blocking | `HOST=0.0.0.0 ./scripts/start-bonsai.sh restart`; `ss -ltn \| grep 8081` should show `0.0.0.0:8081`; `ufw allow from <subnet>.0/24 to any port 8081 proto tcp` |
| `/v1/models` from another machine returns **502** | The bridge didn't carry a key upstream (old bug) | Use the current version: it now injects the key. If it still fails, read `logs/bridge.log` |
| GPU reports `illegal memory access` (Xid 13/43) | Running a **non-Bonsai** MoE on the PrismML fork | That fork is Bonsai-specific; use upstream llama.cpp for other models |
| Answers get shorter late in a long session | `MAX_OUTPUT_TOKENS` is squeezing the prompt budget | Lower it, or raise the server's `-c` (keep `n_ctx = prompt + output`) |

## Output that is NOT an error (don't misdiagnose)

```
common_fit_params: failed to fit params to free device memory:
  n_gpu_layers already set by user to 99, abort
```
→ This is a **warning**: the user set `-ngl` explicitly, so automatic memory tuning is skipped. **Normal.**

```
llama_server: NOTICE: server default port will be changed to :9931 ...
```
→ An upstream notice about a future default-port change. **Does not affect** the current `--port 8080`.

## Operational safety

**Never use `pkill -f "<keyword>"`** — if the command itself contains the keyword
(e.g. `pkill -f "llama-server"`), it can kill the shell running it. Safe alternatives:

```bash
# By port
kill $(ss -ltnp 2>/dev/null | grep ':8080' | grep -oP 'pid=\K[0-9]+')
# By GPU usage
kill $(nvidia-smi --query-compute-apps=pid --format=csv,noheader)
```
