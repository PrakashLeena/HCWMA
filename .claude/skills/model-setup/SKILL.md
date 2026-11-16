---
name: model-setup
description: Set up, switch or benchmark the local Qwen model for HCWMA. Use for build step 3, when changing checkpoint or runtime, or when the model is too slow or runs out of memory.
---

# Local Qwen model setup

Never guess a checkpoint name, size or license. Model line-ups change; confirm on the
official model card every time.

## 1. Read the hardware

Read `docs/DECISIONS.md`. If the PC section is blank, detect what you can
(`nvidia-smi`, OS system info, free disk) and show it to the owner to confirm.

## 2. Choose the checkpoint (with the owner)

1. Open the official Qwen organization page and the model card of the candidate checkpoint.
2. Record in `docs/DECISIONS.md`: exact checkpoint name, license, whether it accepts images,
   whether it supports tool/function calling, and the published memory needs.
3. Pick the largest checkpoint that fits in VRAM with room left for the context window and
   for the owner's other programs. On low VRAM choose a smaller or quantized checkpoint.
4. Check that the chosen runtime supports this exact architecture before downloading.
5. Ask the owner before the download starts. State the download size.

## 3. Install the runtime

- Daily use: a quantized runtime (llama.cpp or Ollama) if it supports the checkpoint.
- Research and source inspection: PyTorch + Transformers.
- Store weights outside the repository, in the models folder from `configs/model.yaml`.
- Pin the runtime and library versions.

## 4. Wrap it

- One module `model/serving/` exposing `generate(messages, tools=None, max_tokens=...)`.
- The endpoint, if any, binds to `127.0.0.1`.
- The rest of HCWMA must not know which runtime is used. Changing checkpoint or runtime
  is a change to `configs/model.yaml` only.

## 5. Benchmark

Write `model/evaluation/benchmark.py` and record results in `docs/benchmarks.md`:

- load time, peak VRAM and RAM,
- time to first token and tokens per second,
- a fixed set of 10 prompts: plain chat, a routing question ("which agent handles ...?"),
  a tool-call request with a JSON schema, a long-context prompt, one Tamil and one
  Sinhala prompt,
- whether the tool-call output parsed as valid JSON each time.

Run it three times and record the spread.

## 6. Decide

Show the owner the numbers. If routing or tool calls take too long to be usable, or
tool-call JSON is unreliable, try a different quantization or checkpoint size and
benchmark again. Record the final choice and the reason in `docs/DECISIONS.md`.

## Done when

`docs/benchmarks.md` has repeatable numbers for the chosen checkpoint, the wrapper passes
a smoke test, and `pytest -q` passes with the model mocked (tests must not need the GPU).
