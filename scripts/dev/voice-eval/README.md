# Live voice evaluation (issue #59, D127)

Scripts to measure the ways to a live spoken conversation on home hardware. The report with the
numbers is the comment on issue #59; these scripts repeat it.

| Script | Measures | Needs |
|---|---|---|
| `cascade_latency.py` | streaming cascade: speech to text (whisper.cpp) → first sentence of the text agent (any OpenAI-compatible endpoint, e.g. the hub's gateway, token in `TOPCOAT_TOKEN`) → text to speech (Piper); time to first sound per step, word error rate | `whisper-cli`, `piper`, a whisper ggml model, a Piper voice; Python 3 standard library only |
| `haru_eval.ps1` | Haru-4B through speech.cpp's Vulkan build on a Windows GPU: one turn from a file, then how to talk live | Windows, a GPU with ~7 GB free VRAM for `q8_0`; downloads into `%LOCALAPPDATA%\topcoat-voice-eval` |

Notes from the first run (2026-11-25):

- Piper voices must come from a `rhasspy/piper-voices` revision before 2026-11-17 (e.g. `a60bfb135881`):
  the standalone binary aborts on the newer voice configs.
- Haru-4B runs without PyTorch through [speech.cpp](https://github.com/nyosegawa/speech.cpp) (MIT; ggml on
  Vulkan, Metal or the CPU; Linux builds need glibc 2.34+). Its GGUF files (`sakasegawa/Haru-4B-GGUF`) run
  only there, not in llama.cpp. On the CPU it is too slow for a live conversation; use a GPU.
- Moshi (Kyutai) speaks only English; Qwen3-Omni is a 30B model: neither was run.
