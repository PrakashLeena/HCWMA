# HCWMA Owner Decisions

Fill this in during Step 0. Claude Code reads it and must not guess missing values.

## PC

OS: Windows (version podu)
CPU: Intel i5-1135G7 (4 cores / 8 threads)
RAM: 16 GB
GPU: Intel Iris Xe (no NVIDIA) → CPU-only
Free SSD: ~546 GB
Laptop or desktop: Laptop



## Model

Qwen checkpoint: Qwen3.5-4B (fallback: Qwen3.5-2B)
License: Apache 2.0 (Step 3 la model card la confirm pannu)
Runtime: Ollama (CPU)
Quantization: Q4_K_M

/build-step 3
Model is already downloaded at <E:\ML\project\hcwma / Qwen3.5-4B-Base>.
Do NOT download any model. CPU-only, no NVIDIA GPU.
Use the existing model, write the wrapper and run the benchmark.

CPU-only, no NVIDIA GPU. Use Ollama with qwen3.5:4b (Q4).
Full safetensors copy already exists at <path> — keep it for research only, do not load it for serving.
## Access

- Folders HCWMA may index and read: none yet (approved list starts empty; decided 2026-11-16)
- Folders that are always off-limits (in addition to the protected paths): none extra (2026-11-16)
- HCWMA workspace folder: `workspace/` in the repo, gitignored (2026-11-16)

## Product

- Desktop shell (Tauri / Electron / PySide / browser-only for now):
- Daily-life agents wanted first:
- Self-built tools need owner approval before first use (default: yes):
- Image editing needs generative features (default: no):
- Languages for voice:
- Phone / second-device access wanted in this version (default: no):
