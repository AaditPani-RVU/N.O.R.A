"""Phase 6's exit number, from what the phone reported: median first audio.

    .venv/bin/python android/tools/voice_latency.py [N]      # last N turns, default 20

Reads `nora_voice_latency.jsonl` in the repo root (written by the hub on each
`voice.turn` event). Times are milliseconds from the moment the phone last
heard speech. Turns cut short before any audio played are not counted.
"""
from __future__ import annotations

import json
import statistics
import sys
from pathlib import Path

PATH = Path(__file__).resolve().parents[2] / "nora_voice_latency.jsonl"


def main() -> None:
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 20
    if not PATH.exists():
        sys.exit(f"no measurements yet ({PATH})")
    rows = [json.loads(line) for line in PATH.read_text().splitlines() if line.strip()]
    rows = [r for r in rows if isinstance(r.get("first_audio_ms"), int)][-n:]
    if not rows:
        sys.exit("no measured turns yet")
    for r in rows:
        print(f"stt {r.get('stt_ms', '—'):>5}  first say {r.get('core_first_say_ms', '—'):>5}  "
              f"first audio {r['first_audio_ms']:>5}  {r.get('tts', '')}/{r.get('route', '')}")
    for key in ("stt_ms", "core_first_say_ms", "first_audio_ms"):
        vals = [r[key] for r in rows if isinstance(r.get(key), int)]
        if vals:
            print(f"median {key}: {statistics.median(vals):.0f} ms")
    med = statistics.median(r["first_audio_ms"] for r in rows)
    print(f"{len(rows)} turns · median first audio {med / 1000:.2f} s · "
          f"{'PASS' if med <= 1500 and len(rows) >= 20 else 'not yet'} (≤ 1.5 s over 20)")


if __name__ == "__main__":
    main()
