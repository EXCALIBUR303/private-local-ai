"""
Voice-cloning worker.

Runs inside its own virtual environment (PyTorch + Chatterbox) and is spoken
to over stdin/stdout as JSON, so the main server never imports torch.

The Perth watermarker stays enabled: generated audio is marked as synthetic.
"""
from __future__ import annotations

import json
import struct
import sys
import wave

_MODEL = None


def load():
    global _MODEL
    if _MODEL is None:
        import torch
        from chatterbox.tts import ChatterboxTTS
        device = "mps" if torch.backends.mps.is_available() else "cpu"
        _MODEL = ChatterboxTTS.from_pretrained(device=device)
    return _MODEL


def main() -> int:
    request = json.loads(sys.stdin.read() or "{}")
    text = (request.get("text") or "").strip()
    reference = request.get("reference")
    out = request.get("out")
    if not (text and reference and out):
        print("missing text, reference or out", file=sys.stderr)
        return 2

    model = load()
    wav = model.generate(text, audio_prompt_path=reference)

    # torchaudio.save now requires TorchCodec; the stdlib writes WAV directly.
    samples = wav.detach().cpu().flatten().clamp(-1, 1).numpy()
    with wave.open(out, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(model.sr)
        w.writeframes(b"".join(struct.pack("<h", int(s * 32767)) for s in samples))

    print(json.dumps({"ok": True, "seconds": len(samples) / model.sr,
                      "watermarked": model.watermarker is not None}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
