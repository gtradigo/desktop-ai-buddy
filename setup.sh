#!/usr/bin/env bash
# One-time setup: virtualenv, Python packages, wake-word models, Piper voice, Ollama model.
set -euo pipefail
cd "$(dirname "$0")"

say() { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }

say "Creating virtual environment in .venv"
python3 -m venv .venv
# shellcheck disable=SC1091
. .venv/bin/activate
pip install --upgrade pip -q

say "Installing Python packages (this takes a few minutes the first time)"
pip install -r requirements.txt

say "Installing openWakeWord"
# openWakeWord 0.6 asks for tflite-runtime, which has no wheels for Python 3.12+.
# We only use its ONNX backend, so install it without that dependency if needed.
if ! pip install -q "openwakeword>=0.6" 2>/dev/null; then
  pip install -q --no-deps "openwakeword>=0.6"
  pip install -q tqdm scipy scikit-learn
fi
python -c "import openwakeword.utils as u; u.download_models()"

say "Downloading the Piper voice from config.yaml"
VOICE=$(python -c "import yaml,pathlib; print(pathlib.Path(yaml.safe_load(open('config.yaml'))['tts']['voice']).stem)")
mkdir -p voices
python -m piper.download_voices "$VOICE" --download-dir voices

say "Checking audio"
if ! python -c "import sounddevice" 2>/dev/null; then
  echo "PortAudio is missing. Install it, e.g.:  sudo apt install libportaudio2"
fi

say "Checking Ollama"
MODEL=$(python -c "import yaml; print(yaml.safe_load(open('config.yaml'))['llm']['model'])")
if command -v ollama >/dev/null; then
  if ollama list 2>/dev/null | awk '{print $1}' | grep -qx "$MODEL"; then
    echo "Model $MODEL is already installed."
  else
    echo "Pulling $MODEL…"
    ollama pull "$MODEL" || echo "Could not pull $MODEL. Is the Ollama service running? (ollama serve)"
  fi
else
  echo "Ollama not found. Install it from https://ollama.com, then run:  ollama pull $MODEL"
fi

chmod +x run.sh
say "Done! Start your buddy with:  ./run.sh   (or ./run.sh --demo to just see the eyes)"
