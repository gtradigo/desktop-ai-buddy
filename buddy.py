#!/usr/bin/env python3
"""Desk Buddy: a little voice assistant with animated eyes that talks to your local Ollama."""
import argparse
import logging
import random
import signal
import sys
from pathlib import Path

import yaml
from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtWidgets import QApplication

from eyes import MOODS, EyesWidget

HERE = Path(__file__).resolve().parent

DEFAULTS = {
    "look": {"eye_color": "#5BE7FF", "face_plate": True, "scale": 1.0,
             "follow_cursor": True, "captions": True, "sleep_after_minutes": 10},
    "wake": {"words": ["hey_jarvis"], "threshold": 0.5, "chime": True},
    "audio": {"input_device": None, "output_device": None, "speech_level": 300,
              "silence_seconds": 0.8, "max_listen_seconds": 12, "follow_up_seconds": 4},
    "stt": {"model": "base.en", "language": "en", "device": "cpu", "compute_type": "int8"},
    "llm": {"url": "http://localhost:11434", "model": "llama3.2:3b", "temperature": 0.7,
            "keep_alive": "30m", "history_turns": 6, "think": None},
    "tts": {"voice": "voices/en_US-amy-medium.onnx", "speed": 1.0},
    "personality": "You are Pip, a small cheerful robot who lives on the user's desktop. "
                   "Keep answers short: one to three sentences.",
}


def load_config(path):
    cfg = {k: (dict(v) if isinstance(v, dict) else v) for k, v in DEFAULTS.items()}
    if path.exists():
        user = yaml.safe_load(path.read_text()) or {}
        for k, v in user.items():
            if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                cfg[k].update(v)
            else:
                cfg[k] = v
    else:
        logging.warning("No config at %s, using defaults", path)
    if isinstance(cfg["wake"]["words"], str):
        cfg["wake"]["words"] = [cfg["wake"]["words"]]
    return cfg


class Bridge(QObject):
    """Thread-safe link from the voice thread to the window (signals are queued)."""
    mood = Signal(str)
    caption = Signal(str, float)
    speaking = Signal(bool)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", type=Path, default=HERE / "config.yaml")
    ap.add_argument("--demo", action="store_true",
                    help="only show the eyes and cycle through expressions (no audio)")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    cfg = load_config(args.config)

    app = QApplication(sys.argv)
    app.setApplicationName("desk-buddy")
    signal.signal(signal.SIGINT, signal.SIG_DFL)     # Ctrl+C in the terminal quits

    eyes = EyesWidget(**cfg["look"])
    eyes.show()

    if args.demo:
        moods = list(MOODS)
        state = {"i": 0}

        def next_mood():
            m = moods[state["i"] % len(moods)]
            state["i"] += 1
            eyes.set_mood(m)
            eyes.set_caption(m, 2.2)
            eyes.set_speaking(m == "happy")
            eyes.amp = random.random() if m == "happy" else 0.0

        eyes.on_talk = next_mood
        timer = QTimer()
        timer.timeout.connect(next_mood)
        timer.start(2500)
        next_mood()
        sys.exit(app.exec())

    from brain import Brain          # heavy imports only when really running

    bridge = Bridge()
    bridge.mood.connect(eyes.set_mood)
    bridge.caption.connect(eyes.set_caption)
    bridge.speaking.connect(eyes.set_speaking)

    brain = Brain(cfg, bridge, set_amp=lambda a: setattr(eyes, "amp", a),
                  base_dir=args.config.resolve().parent)
    eyes.on_talk = brain.talk_now.set
    eyes.on_mute = brain.set_muted
    brain.start()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
