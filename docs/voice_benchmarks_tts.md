# Step 14 — TTS benchmark

Same sentence per language (about 5 s of speech). RTF = synthesis time / audio length (below 1 = faster than real time). Listen to the wav files to judge quality.

| Engine | English | Tamil | Sinhala | RAM added MB | Download |
|---|---|---|---|---|---|
| sapi | yes, 0.07 s (RTF 0.013) | no | no | 25 | none (Windows) |
| mms | yes, 1.27 s (RTF 0.269) | yes, 1.42 s (RTF 0.276) | no | 34 | ~114 MB per language |
| espeak | yes, 0.01 s (RTF 0.001) | yes, 0.01 s (RTF 0.001) | yes, 0.0 s (RTF 0.001) | 3 | none (in the pip wheel, ~10 MB) |
| piper | yes (many voices) | no | no | — | ~60 MB per voice |

Piper has no Tamil or Sinhala voice (checked rhasspy/piper-voices). Windows can add a Tamil voice (Microsoft Valluvar) through Settings > Language, which is a system change; Windows has no Sinhala voice.

## Notes

- sapi: Microsoft David Desktop - English (United States) (LCID 409) -> en
- sapi: Microsoft Zira Desktop - English (United States) (LCID 409) -> en
- sapi English: `workspace\voice_bench\tts\sapi_en.wav` (5.72 s audio)
- mms: No Sinhala MMS-TTS model exists (facebook/mms-tts-sin is not published).
- mms English: `workspace\voice_bench\tts\mms_en.wav` (4.71 s audio)
- mms Tamil: `workspace\voice_bench\tts\mms_ta.wav` (5.14 s audio)
- espeak English: `workspace\voice_bench\tts\espeak_en.wav` (4.21 s audio)
- espeak Tamil: `workspace\voice_bench\tts\espeak_ta.wav` (4.6 s audio)
- espeak Sinhala: `workspace\voice_bench\tts\espeak_si.wav` (4.8 s audio)
