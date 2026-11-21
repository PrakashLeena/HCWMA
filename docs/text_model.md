# Daily model: text-only Qwen3.5-4B vs qwen3.5:4b (2026-11-21)

Owner request: switch the daily model to a text-only Qwen3.5-4B (no vision projector) if accuracy
is not lower. Measured on the owner's laptop (i5-1135G7, 16 GB, CPU only, Ollama 0.40.16, num_ctx 8192,
thinking off), both models on the same Ollama, the other model unloaded before each run.

## How the text-only model was made (no download, no conversion)

`qwen3.5:4b` stores two separate layers: the weights (`model.gguf`, 2.65 GB, Q4_K_M) and the vision
projector (`mmproj.gguf`, 0.68 GB). `python -m model.serving.text_model` creates `qwen3.5-text:4b`
from the **same weights layer** (same file on disk, nothing copied) with the same renderer and
parser (`qwen3.5`: chat template and tool-call format), template and parameters, and no projector.
Ollama reports it as `completion, tools, thinking` (no `vision`).

Not used: building a GGUF from the safetensors in `Qwen3.5-4B-Base/`. That folder is the
**pre-trained Base** model ("not direct interaction" in its README), not the instruct model behind
`qwen3.5:4b`, so it would be a different, weaker chat model, and the conversion would need
llama.cpp's Python tools (torch etc.). No official text-only GGUF download was needed either.

## Results

| | qwen3.5:4b (with vision) | qwen3.5-text:4b | change |
|---|---:|---:|---:|
| Routing eval, all 215 cases (`eval_routing`) | 194/215 | 194/215 | same 21 cases fail |
| Fast path | 64/64 | 64/64 | |
| Tool-call / JSON validity (`benchmark`, 3 runs) | 9/9 | 9/9 | |
| Generation speed, median (tok/s) | 7.63 | 7.59 | same |
| Prefill, long context (tok/s) | 34.1 | 33.6 | same |
| Load time, median (s) | 4.94 | 3.21 | -1.7 s |
| Runtime RAM after load (benchmark, MB) | 3921 | 3209 | -712 |
| Runtime RAM peak (benchmark, MB) | 5017 | 4308 | -709 |
| Lowest free system RAM during benchmark (MB) | 2388 | 2936 | +548 |
| llama-server private MB after request 1 / 20 (`ram_soak`) | 5049 / 5523 | 4011 / 4484 | **-1038 / -1039** |
| llama-server working set MB after request 1 / 20 | 3749 / 4223 | 3102 / 3297 | -647 / -926 |

The routing results are the same case for case (same pass/fail list; the 21 failures are the known
ones, e.g. 'Draw me a picture of a cat wearing a hat' -> clarify). The `smoke_test` through the
desktop app is run by the owner (it needs the app started with this code).

Switching back is one line in `configs/model.yaml` (`ollama_model: qwen3.5:4b`); both tags stay installed.
Images: HCWMA never sends pictures to Qwen today; with the text-only model it could not.

## Raw tables
### qwen3.5:4b

| request | working set MB | private MB | seconds |
|---:|---:|---:|---:|
| 1 | 3749 | 5049 | 39.7 |
| 2 | 3870 | 5170 | 26.2 |
| 3 | 3989 | 5289 | 17.1 |
| 4 | 4101 | 5401 | 24.0 |
| 5 | 4223 | 5523 | 12.5 |
| 6 | 4214 | 5514 | 29.1 |
| 7 | 4215 | 5515 | 24.1 |
| 8 | 4223 | 5523 | 13.9 |
| 9 | 4213 | 5512 | 32.6 |
| 10 | 4223 | 5522 | 18.6 |
| 11 | 4215 | 5515 | 27.7 |
| 12 | 4217 | 5516 | 28.1 |
| 13 | 4225 | 5525 | 17.1 |
| 14 | 4216 | 5515 | 24.0 |
| 15 | 4224 | 5523 | 15.5 |
| 16 | 4216 | 5516 | 23.9 |
| 17 | 4217 | 5516 | 23.9 |
| 18 | 4225 | 5524 | 14.0 |
| 19 | 4215 | 5514 | 22.7 |
| 20 | 4223 | 5523 | 15.1 |

After request 1: 3749 MB, after request 20: 4223 MB, peak 4225 MB (working set).

### qwen3.5-text:4b

| request | working set MB | private MB | seconds |
|---:|---:|---:|---:|
| 1 | 3102 | 4011 | 31.5 |
| 2 | 3222 | 4132 | 18.6 |
| 3 | 3340 | 4250 | 17.1 |
| 4 | 3453 | 4362 | 22.5 |
| 5 | 3294 | 4483 | 16.6 |
| 6 | 3287 | 4476 | 22.4 |
| 7 | 3289 | 4478 | 26.6 |
| 8 | 3297 | 4486 | 13.2 |
| 9 | 3287 | 4475 | 25.8 |
| 10 | 3296 | 4484 | 17.5 |
| 11 | 3289 | 4476 | 23.2 |
| 12 | 3290 | 4478 | 21.0 |
| 13 | 3299 | 4487 | 10.3 |
| 14 | 3289 | 4476 | 23.0 |
| 15 | 3297 | 4485 | 15.2 |
| 16 | 3289 | 4477 | 20.1 |
| 17 | 3290 | 4478 | 21.1 |
| 18 | 3299 | 4486 | 14.0 |
| 19 | 3289 | 4476 | 19.4 |
| 20 | 3297 | 4484 | 16.6 |

After request 1: 3102 MB, after request 20: 3297 MB, peak 3453 MB (working set).


### Benchmark summaries
## qwen3.5:4b (Q4_K_M, cpu) — 2026-11-21

| Setting | Value |
|---|---|
| Checkpoint | Qwen3.5-4B |
| Runtime | ollama 0.40.16 |
| Context length | 8192 |
| Thinking mode | off |
| Runs | 3 |
| OS | Windows 10 (10.0.26300) |
| CPU | Intel64 Family 6 Model 140 Stepping 1, GenuineIntel |
| Cores | 4 cores / 8 threads |
| RAM | 15.7 GB |
| Python | 3.10.11 |

### Summary (median, with min–max over runs)

| Metric | Value |
|---|---|
| Load time (s) | 4.94 (4.71–8.69) |
| Runtime RAM idle before load (MB) | 328.47 (327.88–328.52) |
| Runtime RAM after load (MB) | 3920.62 (3919.13–3921.05) |
| Runtime RAM peak (MB) | 5017.48 (5016.95–5017.68) |
| Free system RAM, lowest during run (MB) | 2388.26 (2319.52–2406.23) |
| Generation speed, all prompts (tok/s) | 7.63 (6.71–14.84) |
| Time to first token, short prompts (s) | 2.23 (1.01–16.04) |
| Tool-call / JSON validity | 9/9 |
| Valid JSON per run | 3/3, 3/3, 3/3 |

## qwen3.5-text:4b (Q4_K_M, cpu) — 2026-11-21

| Setting | Value |
|---|---|
| Checkpoint | Qwen3.5-4B |
| Runtime | ollama 0.40.16 |
| Context length | 8192 |
| Thinking mode | off |
| Runs | 3 |
| OS | Windows 10 (10.0.26300) |
| CPU | Intel64 Family 6 Model 140 Stepping 1, GenuineIntel |
| Cores | 4 cores / 8 threads |
| RAM | 15.7 GB |
| Python | 3.10.11 |

### Summary (median, with min–max over runs)

| Metric | Value |
|---|---|
| Load time (s) | 3.21 (2.97–5.99) |
| Runtime RAM idle before load (MB) | 265.45 (263.26–265.71) |
| Runtime RAM after load (MB) | 3208.70 (3207.48–3209.85) |
| Runtime RAM peak (MB) | 4307.84 (4306.11–4308.56) |
| Free system RAM, lowest during run (MB) | 2936.18 (2837.77–3006.93) |
| Generation speed, all prompts (tok/s) | 7.59 (3.70–15.23) |
| Time to first token, short prompts (s) | 2.27 (1.02–16.13) |
| Tool-call / JSON validity | 9/9 |
| Valid JSON per run | 3/3, 3/3, 3/3 |

