"""Voice pipeline: wake word -> record -> faster-whisper -> Ollama -> Piper.

Runs in a background thread and talks to the eyes window through Qt signals.
"""
import collections
import datetime
import json
import logging
import queue
import re
import threading
import time
from pathlib import Path

import numpy as np
import requests
import sounddevice as sd

from eyes import MOODS

log = logging.getLogger("buddy")

RATE = 16000          # what openWakeWord and Whisper expect
BLOCK = 1280          # 80 ms blocks, the size openWakeWord wants
BLOCK_SEC = BLOCK / RATE

TAG_RE = re.compile(r"\[(\w+)\]")
EMOJI_RE = re.compile("[\U0001F000-\U0001FAFF☀-➿️‍]")

EMOTION_RULES = (
    "Your replies are spoken aloud by a text-to-speech voice, so write plain "
    "conversational sentences: no markdown, lists, code or emoji. "
    "Start every reply with exactly one emotion tag that matches how you feel, "
    "chosen from: [happy] [excited] [sad] [angry] [surprised] [confused] [thinking] [neutral]. "
    "You may add another tag later in the reply if your feeling changes."
)


# ------------------------------------------------------------------ text helpers
def clean_for_speech(text):
    """Strip emotion tags, markdown symbols and emoji so TTS reads it naturally."""
    text = TAG_RE.sub(" ", text)
    text = EMOJI_RE.sub("", text)
    text = re.sub(r"[*_#`>~|]", "", text)
    text = re.sub(r"\s+", " ", text)
    return re.sub(r"\s+([.,!?;:…])", r"\1", text).strip()


def split_sentences(buf):
    """Return (complete sentences, leftover) from a streaming text buffer."""
    out = []
    while True:
        m = re.search(r"[.!?…]+[\"')\]]?(?=\s)|\n", buf)
        if not m:
            return out, buf
        sentence, buf = buf[:m.end()], buf[m.end():]
        if sentence.strip():
            out.append(sentence)


def visible_text(raw):
    """Hide <think>…</think> blocks some reasoning models emit inline."""
    text = re.sub(r"<think>.*?</think>", "", raw, flags=re.S)
    if "<think>" in text:
        text = text[:text.index("<think>")]
    return text


def resample(x, src_rate, dst_rate):
    if src_rate == dst_rate or len(x) == 0:
        return x
    n = int(round(len(x) * dst_rate / src_rate))
    return np.interp(np.linspace(0, len(x) - 1, n), np.arange(len(x)), x).astype(np.int16)


def rms(x):
    return float(np.sqrt(np.mean(x.astype(np.float32) ** 2))) if len(x) else 0.0


# ------------------------------------------------------------------ the brain
class Brain(threading.Thread):
    def __init__(self, cfg, ui, set_amp, base_dir):
        super().__init__(daemon=True)
        self.cfg = cfg
        self.ui = ui                    # Bridge with mood / caption / speaking signals
        self.set_amp = set_amp
        self.base_dir = Path(base_dir)
        self.talk_now = threading.Event()
        self.muted = False
        self.mic_q = queue.Queue()
        self.history = []
        self.noise = 150.0
        self.last_active = time.monotonic()
        self.sleep_state = 0

    # ---------------------------------------------------------- small helpers
    def show(self, mood=None, caption=None, seconds=4.0):
        if mood:
            self.ui.mood.emit(mood)
        if caption:
            self.ui.caption.emit(caption, float(seconds))

    def path(self, p):
        p = Path(p).expanduser()
        return p if p.is_absolute() else self.base_dir / p

    def set_muted(self, muted):
        self.muted = muted
        self.show("asleep" if muted else "neutral", "Muted" if muted else "Listening again", 2)

    def drain(self):
        try:
            while True:
                self.mic_q.get_nowait()
        except queue.Empty:
            pass

    # ---------------------------------------------------------- startup
    def run(self):
        try:
            self.setup()
        except Exception as e:      # show the problem on the buddy itself
            log.exception("Startup failed")
            self.show("confused", f"Startup failed: {e}", 3600)
            return
        self.loop()

    def setup(self):
        c = self.cfg
        self.show("sleepy", "Waking up…", 30)

        log.info("Loading wake word models: %s", c["wake"]["words"])
        from openwakeword.model import Model
        words = [str(self.path(w)) if str(w).endswith((".onnx", ".tflite")) else w
                 for w in c["wake"]["words"]]
        try:
            self.oww = Model(wakeword_models=words, inference_framework="onnx")
        except Exception:
            import openwakeword.utils
            log.info("Downloading openWakeWord base models…")
            openwakeword.utils.download_models()
            self.oww = Model(wakeword_models=words, inference_framework="onnx")

        log.info("Loading Whisper model %s", c["stt"]["model"])
        from faster_whisper import WhisperModel
        self.stt = WhisperModel(c["stt"]["model"], device=c["stt"]["device"],
                                compute_type=c["stt"]["compute_type"])

        voice_path = self.path(c["tts"]["voice"])
        log.info("Loading Piper voice %s", voice_path)
        if not voice_path.exists():
            raise FileNotFoundError(f"voice not found: {voice_path} (run setup.sh)")
        from piper import PiperVoice
        self.voice = PiperVoice.load(str(voice_path))
        self.tts_rate = self.voice.config.sample_rate
        self.syn_config = None
        speed = float(c["tts"].get("speed") or 1.0)
        if speed != 1.0:
            try:
                from piper import SynthesisConfig
                self.syn_config = SynthesisConfig(length_scale=1.0 / speed)
            except ImportError:
                pass

        self.warm_up_ollama()
        self.open_audio()

        name = Path(str(c["wake"]["words"][0])).stem.split("_v0")[0].replace("_", " ")
        self.show("happy", f"Hi! Say “{name}” or double-click me.", 6)
        time.sleep(2)
        self.show("neutral")

    def warm_up_ollama(self):
        llm = self.cfg["llm"]
        try:
            r = requests.post(f"{llm['url']}/api/generate",
                              json={"model": llm["model"], "keep_alive": llm["keep_alive"]},
                              timeout=180)
            if r.status_code == 404:
                self.show("confused", f"Model not found. Run: ollama pull {llm['model']}", 15)
                time.sleep(4)
            elif r.ok:
                log.info("Ollama model %s loaded", llm["model"])
        except requests.RequestException:
            self.show("confused", f"Can't reach Ollama at {llm['url']}", 15)
            time.sleep(4)

    def open_audio(self):
        a = self.cfg["audio"]
        # microphone: prefer 16 kHz, otherwise use the device rate and resample
        try:
            sd.check_input_settings(device=a["input_device"], samplerate=RATE, channels=1, dtype="int16")
            self.mic_rate = RATE
        except Exception:
            self.mic_rate = int(sd.query_devices(a["input_device"], "input")["default_samplerate"])
        log.info("Microphone at %d Hz", self.mic_rate)

        def on_audio(indata, frames, t, status):
            x = indata[:, 0].copy()
            if self.mic_rate != RATE:
                x = np.interp(np.linspace(0, len(x) - 1, BLOCK), np.arange(len(x)), x).astype(np.int16)
            self.mic_q.put(x)

        self.mic = sd.InputStream(device=a["input_device"], samplerate=self.mic_rate, channels=1,
                                  dtype="int16", blocksize=int(self.mic_rate * BLOCK_SEC),
                                  callback=on_audio)
        self.mic.start()

        # speaker: prefer the voice's own rate
        try:
            sd.check_output_settings(device=a["output_device"], samplerate=self.tts_rate, channels=1, dtype="int16")
            self.out_rate = self.tts_rate
        except Exception:
            self.out_rate = int(sd.query_devices(a["output_device"], "output")["default_samplerate"])

    # ---------------------------------------------------------- main loop
    def loop(self):
        thr = float(self.cfg["wake"]["threshold"])
        while True:
            try:
                block = self.mic_q.get(timeout=0.5)
            except queue.Empty:
                block = None

            if self.talk_now.is_set():
                self.conversation()
                continue
            if block is None or self.muted:
                self.idle_check()
                continue

            self.noise = 0.98 * self.noise + 0.02 * rms(block)
            scores = self.oww.predict(block)
            word, score = max(scores.items(), key=lambda kv: kv[1], default=("", 0.0))
            if score >= thr:
                log.info("Wake word %s (%.2f)", word, score)
                self.conversation()
            else:
                self.idle_check()

    def idle_check(self):
        minutes = float(self.cfg["look"].get("sleep_after_minutes") or 0)
        if not minutes or self.muted:
            return
        idle = time.monotonic() - self.last_active
        if self.sleep_state == 0 and idle > minutes * 60:
            self.sleep_state = 1
            self.show("sleepy")
        elif self.sleep_state == 1 and idle > minutes * 60 + 60:
            self.sleep_state = 2
            self.show("asleep")

    # ---------------------------------------------------------- one conversation
    def conversation(self):
        self.talk_now.clear()
        self.sleep_state = 0
        a = self.cfg["audio"]
        start_timeout, first = 5.0, True
        while True:
            if first and self.cfg["wake"].get("chime", True):
                self.ui.mood.emit("listening")
                self.chime()
            self.drain()
            audio = self.record(start_timeout)
            if audio is None:
                if first:
                    self.show("confused", "…?", 2)
                    time.sleep(1.2)
                break
            text = self.transcribe(audio)
            if not text:
                self.show("confused", "Sorry, I didn't catch that.", 3)
                time.sleep(1.5)
                break
            log.info("You: %s", text)
            self.show(caption=f"“{text}”", seconds=6)
            self.reply(text)
            first = False
            follow_up = float(a.get("follow_up_seconds") or 0)
            if follow_up <= 0:
                break
            start_timeout = follow_up

        self.drain()
        try:
            self.oww.reset()
        except Exception:
            pass
        self.last_active = time.monotonic()
        self.show("asleep" if self.muted else "neutral")

    def record(self, start_timeout):
        """Record one utterance. Returns float32 audio, or None if nobody spoke."""
        a = self.cfg["audio"]
        self.ui.mood.emit("listening")
        level = max(self.noise * 2.5, float(a.get("speech_level") or 300))
        pre = collections.deque(maxlen=4)          # ~0.3 s pre-roll
        frames, started, silent = [], False, 0.0
        t0 = time.monotonic()
        speech_t0 = t0
        while True:
            try:
                b = self.mic_q.get(timeout=1)
            except queue.Empty:
                if not started and time.monotonic() - t0 > start_timeout:
                    return None
                continue
            loud = rms(b) > level
            now = time.monotonic()
            if not started:
                pre.append(b)
                if loud:
                    started, speech_t0 = True, now
                    frames.extend(pre)
                elif now - t0 > start_timeout:
                    return None
            else:
                frames.append(b)
                silent = 0.0 if loud else silent + BLOCK_SEC
                if silent >= float(a["silence_seconds"]) or now - speech_t0 > float(a["max_listen_seconds"]):
                    break
        self.ui.mood.emit("thinking")
        return np.concatenate(frames).astype(np.float32) / 32768.0

    def transcribe(self, audio):
        s = self.cfg["stt"]
        segments, _ = self.stt.transcribe(audio, language=s.get("language") or None, beam_size=1,
                                          vad_filter=True, condition_on_previous_text=False)
        text = " ".join(seg.text.strip() for seg in segments).strip()
        return text if len(text) > 1 else ""

    # ---------------------------------------------------------- talking back
    def reply(self, user_text):
        llm = self.cfg["llm"]
        now = datetime.datetime.now().strftime("%A %d %B %Y, %H:%M")
        system = f"{self.cfg['personality'].strip()}\n\n{EMOTION_RULES}\nCurrent date and time: {now}."
        keep = int(llm.get("history_turns") or 0) * 2
        messages = ([{"role": "system", "content": system}]
                    + (self.history[-keep:] if keep else [])
                    + [{"role": "user", "content": user_text}])

        speak_q = queue.Queue()
        speaker = threading.Thread(target=self.speaker, args=(speak_q,), daemon=True)
        speaker.start()
        raw_reply, buf = [], ""

        def queue_sentence(sentence):
            raw_reply.append(sentence.strip())
            for tag in TAG_RE.findall(sentence):
                if tag.lower() in MOODS:
                    speak_q.put(("mood", tag.lower()))
            spoken = clean_for_speech(sentence)
            if spoken:
                speak_q.put(("say", spoken))

        try:
            for piece in self.ollama_stream(messages):
                buf += piece
                sentences, buf = split_sentences(buf)
                for s in sentences:
                    queue_sentence(s)
            if buf.strip():
                queue_sentence(buf)
        except requests.RequestException:
            log.exception("Ollama request failed")
            speak_q.put(("mood", "confused"))
            self.show(caption=f"Can't reach Ollama at {llm['url']}", seconds=8)
        except Exception as e:
            log.exception("LLM error")
            speak_q.put(("mood", "confused"))
            self.show(caption=str(e)[:100], seconds=8)
        speak_q.put(None)
        speaker.join()

        if raw_reply:
            log.info("Buddy: %s", " ".join(raw_reply))
            self.history += [{"role": "user", "content": user_text},
                             {"role": "assistant", "content": " ".join(raw_reply)}]
            time.sleep(1.0)          # hold the final expression for a moment

    def ollama_stream(self, messages):
        llm = self.cfg["llm"]
        body = {"model": llm["model"], "messages": messages, "stream": True,
                "keep_alive": llm["keep_alive"],
                "options": {"temperature": float(llm["temperature"])}}
        if llm.get("think") is not None:
            body["think"] = bool(llm["think"])
        with requests.post(f"{llm['url']}/api/chat", json=body, stream=True, timeout=(5, 300)) as r:
            if r.status_code != 200:
                raise RuntimeError(f"Ollama: {r.text[:120]}")
            raw, shown = "", ""
            for line in r.iter_lines():
                if not line:
                    continue
                d = json.loads(line)
                if d.get("error"):
                    raise RuntimeError(f"Ollama: {d['error']}")
                raw += d.get("message", {}).get("content", "")
                vis = visible_text(raw)
                if len(vis) > len(shown):
                    yield vis[len(shown):]
                    shown = vis
                if d.get("done"):
                    break

    def synth(self, text):
        v = self.voice
        if hasattr(v, "synthesize_stream_raw"):            # piper-tts 1.2.x
            kw = {"length_scale": self.syn_config.length_scale} if self.syn_config else {}
            data = b"".join(v.synthesize_stream_raw(text, **kw))
        else:                                              # piper-tts 1.3+
            data = b"".join(ch.audio_int16_bytes for ch in v.synthesize(text, syn_config=self.syn_config))
        return resample(np.frombuffer(data, dtype=np.int16), self.tts_rate, self.out_rate)

    def speaker(self, q):
        """Consumes ("mood", m) / ("say", text) items; synthesizes and plays in order."""
        self.ui.speaking.emit(True)
        mood_set = False
        try:
            with sd.OutputStream(device=self.cfg["audio"]["output_device"], samplerate=self.out_rate,
                                 channels=1, dtype="int16") as out:
                while True:
                    item = q.get()
                    if item is None:
                        break
                    kind, val = item
                    if kind == "mood":
                        self.ui.mood.emit(val)
                        mood_set = True
                        continue
                    if not mood_set:
                        self.ui.mood.emit("neutral")
                        mood_set = True
                    self.ui.caption.emit(val, 8.0)
                    audio = self.synth(val)
                    for i in range(0, len(audio), 1024):
                        chunk = audio[i:i + 1024]
                        out.write(chunk.reshape(-1, 1))
                        self.set_amp(min(1.0, rms(chunk) / 6000))
                    self.set_amp(0.0)
        except Exception:
            log.exception("Playback failed")
        finally:
            self.set_amp(0.0)
            self.ui.speaking.emit(False)

    def chime(self):
        """A short rising two-note 'boop' when the buddy starts listening."""
        r = self.out_rate
        notes = []
        for f in (880, 1320):
            t = np.arange(int(r * 0.07)) / r
            env = np.sin(np.pi * t / t[-1])
            notes.append(0.25 * env * np.sin(2 * np.pi * f * t))
        tone = (np.concatenate(notes) * 32767).astype(np.int16)
        try:
            sd.play(tone, r, device=self.cfg["audio"]["output_device"], blocking=True)
        except Exception:
            log.warning("Chime failed", exc_info=True)
