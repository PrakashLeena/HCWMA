# Model RAM over 20 requests (backlog item 1, 2026-11-21)

Measured with `python -m model.evaluation.ram_soak --runs 20` on the owner's laptop (16 GB RAM,
Intel CPU, Ollama 0.40.16, `qwen3.5:4b` Q4_K_M, num_ctx 8192). The 20 requests cycle through five
real agent prompts (router, file_system, planner, communication, documents_study), max 48 output
tokens each. "Working set" is the RAM llama-server occupies now; "private" is the memory committed to
it alone (what Task Manager's commit / the ~5.7 GB in the backlog counts). Before = the Ollama
started by Ollama's own tray app (defaults); after = an `ollama serve` started by HCWMA's launcher
with the new settings (checked in llama-server's environment: `LLAMA_ARG_CACHE_RAM=512`,
`LLAMA_ARG_CTX_CHECKPOINTS=4`, `OLLAMA_NUM_PARALLEL=1`, `OLLAMA_MAX_LOADED_MODELS=1`).

## What the memory is (llama-server load log)

| part | MiB |
|---|---:|
| weights (CPU 817 + CPU_REPACK 1699; Ollama loads with `--load-mode none`, so no mmap: all private) | 2516 |
| vision projector (mmproj 644 + its compute buffer 223; HCWMA never sends images) | 867 |
| KV cache (8192 tokens, 8 attention layers) + recurrent state | 306 |
| compute buffer (batch 1024) | 174 |
| saved prompt states + context checkpoints (grow with use) | ~470 in this run |

Ollama's own "3.2 GB" counts weights + KV only. num_ctx was already fixed at 8192 on every chat
request and Ollama already started llama-server with one slot (`-np 1`), so neither caused the growth.

### before (Ollama defaults)

| request | working set MB | private MB | seconds |
|---:|---:|---:|---:|
| 1 | 3749 | 5049 | 33.2 |
| 2 | 3870 | 5170 | 23.8 |
| 3 | 3988 | 5289 | 19.1 |
| 4 | 4101 | 5400 | 26.8 |
| 5 | 4199 | 5522 | 15.5 |
| 6 | 4172 | 5513 | 31.4 |
| 7 | 4174 | 5516 | 29.2 |
| 8 | 4082 | 5524 | 14.3 |
| 9 | 4072 | 5514 | 32.4 |
| 10 | 4081 | 5523 | 20.8 |
| 11 | 4074 | 5515 | 29.1 |
| 12 | 4075 | 5516 | 27.2 |
| 13 | 4084 | 5525 | 17.3 |
| 14 | 4074 | 5515 | 28.8 |
| 15 | 4082 | 5524 | 19.6 |
| 16 | 4075 | 5516 | 25.9 |
| 17 | 4076 | 5517 | 26.1 |
| 18 | 4083 | 5524 | 18.9 |
| 19 | 4074 | 5515 | 29.5 |
| 20 | 3819 | 5523 | 20.2 |

After request 1: 3749 MB, after request 20: 3819 MB, peak 4199 MB (working set).
### after (HCWMA-started Ollama: 1 slot, 512 MiB prompt cache, 4 checkpoints)

| request | working set MB | private MB | seconds |
|---:|---:|---:|---:|
| 1 | 3749 | 5050 | 43.1 |
| 2 | 3869 | 5171 | 24.3 |
| 3 | 3988 | 5289 | 20.0 |
| 4 | 4101 | 5401 | 30.7 |
| 5 | 4222 | 5522 | 13.1 |
| 6 | 4213 | 5513 | 26.8 |
| 7 | 4215 | 5515 | 25.4 |
| 8 | 4223 | 5523 | 17.2 |
| 9 | 4214 | 5514 | 23.4 |
| 10 | 4223 | 5523 | 16.9 |
| 11 | 4215 | 5515 | 22.9 |
| 12 | 4216 | 5516 | 23.2 |
| 13 | 4224 | 5524 | 15.5 |
| 14 | 4215 | 5515 | 20.5 |
| 15 | 4223 | 5522 | 16.6 |
| 16 | 4215 | 5515 | 23.6 |
| 17 | 4216 | 5516 | 24.2 |
| 18 | 4224 | 5524 | 15.6 |
| 19 | 4215 | 5515 | 25.5 |
| 20 | 4224 | 5523 | 17.2 |

After request 1: 3749 MB, after request 20: 4224 MB, peak 4224 MB (working set).
In this run the prompt cache stayed under 512 MiB anyway (it is also capped at num_ctx = 8192 tokens),
so the cache limits changed nothing measurable; they only cap the worst case (default 8192 MiB and
32 checkpoints per slot). Speed was the same (mean 24.5 s before, 22.6 s after per request).

## Idle RAM guard (live, same session)

| moment | working set MB | private MB |
|---|---:|---:|
| after the 20 requests | 4224 | 5523 |
| guard check (idle, 5523 > 5300) -> model unloaded | 0 | 0 |
| next request (reload took 8.8 s) | 3658 | 5043 |
| guard check again (5043 < 5300) | kept | kept |

So on a 16 GB PC the model holds ~5.0-5.5 GB while in use and gives it all back after 5 idle
minutes once it has grown past 5.3 GB.
