"""Animated robot eyes in a small always-on-top, transparent window."""
import math
import random
import time

from PySide6.QtCore import QPointF, QRectF, QSettings, Qt, QTimer
from PySide6.QtGui import (QAction, QColor, QCursor, QFont, QPainter,
                           QPainterPath, QPen, QPolygonF)
from PySide6.QtWidgets import QMenu, QWidget

# Shape parameters for both eyes. Sizes are multipliers of the base eye size,
# lid values are how far a lid comes down (0..1 of the eye height) at the
# inner corner (towards the nose) and the outer corner.
BASE = dict(lw=1.0, lh=1.0, rw=1.0, rh=1.0,
            l_in=0.0, l_out=0.0, r_in=0.0, r_out=0.0,
            bottom=0.0, open=1.0)

# "gaze" pins where the eyes look (-1..1); without it they wander or follow the cursor.
MOODS = {
    "neutral":   {},
    "listening": dict(lw=1.06, lh=1.15, rw=1.06, rh=1.15),
    "thinking":  dict(rh=0.85, r_in=0.28, r_out=0.28, gaze=(0.6, -0.7)),
    "happy":     dict(bottom=0.45),
    "excited":   dict(lw=1.12, lh=1.12, rw=1.12, rh=1.12, bottom=0.42),
    "sad":       dict(l_out=0.45, r_out=0.45, l_in=0.02, r_in=0.02, gaze=(0, 0.45)),
    "angry":     dict(l_in=0.45, r_in=0.45, l_out=0.05, r_out=0.05),
    "surprised": dict(lw=1.18, lh=1.28, rw=1.18, rh=1.28),
    "confused":  dict(lh=0.72, l_in=0.2, l_out=0.2, rh=1.12, gaze=(-0.25, -0.2)),
    "sleepy":    dict(l_in=0.5, l_out=0.55, r_in=0.5, r_out=0.55, gaze=(0, 0.35)),
    "asleep":    dict(open=0.06, gaze=(0, 0.5)),
}
HOP_MOODS = {"listening", "surprised", "excited"}


class EyesWidget(QWidget):
    def __init__(self, eye_color="#5BE7FF", face_plate=True, scale=1.0,
                 follow_cursor=True, captions=True, **_ignored):
        super().__init__()
        self.setWindowTitle("Desk Buddy")
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground)

        self.s = float(scale)
        self.fw, self.fh = int(240 * self.s), int(150 * self.s)   # face plate
        self.cap_h = int(66 * self.s) if captions else 0
        self.resize(self.fw, self.fh + self.cap_h)

        self.color = QColor(eye_color)
        self.face_plate = face_plate
        self.follow_cursor = follow_cursor
        self.captions = captions

        # state set from outside
        self.mood = "neutral"
        self.speaking = False
        self.muted = False
        self.amp = 0.0                      # speech loudness 0..1, written by the audio thread
        self.on_talk = lambda: None         # callbacks wired up by buddy.py
        self.on_mute = lambda muted: None

        # animation state
        now = time.monotonic()
        self.cur = dict(BASE)
        self.gx = self.gy = 0.0
        self.amp_s = 0.0
        self.wander = (0.0, 0.0)
        self.next_wander = now + 2
        self.next_blink = now + random.uniform(1.5, 4)
        self.blink_t = None
        self.hop_t = None
        self.caption = ""
        self.caption_until = 0.0
        self.last_cursor = None
        self.cursor_moved_at = 0.0

        self.settings = QSettings("desk-buddy", "desk-buddy")
        self._place()

        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(16)

    # ---------------------------------------------------------------- public API
    def set_mood(self, mood):
        mood = mood if mood in MOODS else "neutral"
        if mood != self.mood and mood in HOP_MOODS:
            self.hop_t = time.monotonic()
        self.mood = mood

    def set_caption(self, text, seconds=5.0):
        text = " ".join(str(text).split())
        if len(text) > 110:
            text = text[:107] + "…"
        self.caption = text
        self.caption_until = time.monotonic() + float(seconds)

    def set_speaking(self, speaking):
        self.speaking = bool(speaking)

    # ---------------------------------------------------------------- placement
    def _place(self):
        pos = self.settings.value("pos")
        screen = self.screen()
        if pos is not None and screen and any(
                s.availableGeometry().contains(pos) for s in screen.virtualSiblings()):
            self.move(pos)
        elif screen:
            g = screen.availableGeometry()
            self.move(g.right() - self.width() - 40, g.bottom() - self.height() - 40)

    def moveEvent(self, e):
        self.settings.setValue("pos", self.pos())
        super().moveEvent(e)

    # ---------------------------------------------------------------- mouse
    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton and self.windowHandle():
            self.windowHandle().startSystemMove()   # works on X11 and Wayland

    def mouseDoubleClickEvent(self, e):
        self.on_talk()

    def contextMenuEvent(self, e):
        menu = QMenu(self)
        talk = QAction("Talk now", menu)
        talk.triggered.connect(self.on_talk)
        mute = QAction("Mute", menu, checkable=True, checked=self.muted)
        mute.toggled.connect(self._toggle_mute)
        quit_ = QAction("Quit", menu)
        quit_.triggered.connect(self.close)
        menu.addAction(talk)
        menu.addAction(mute)
        menu.addSeparator()
        menu.addAction(quit_)
        menu.exec(e.globalPos())

    def _toggle_mute(self, muted):
        self.muted = muted
        self.on_mute(muted)

    # ---------------------------------------------------------------- animation
    def _gaze_target(self, now, spec):
        if "gaze" in spec:
            return spec["gaze"]
        if self.follow_cursor:
            p = QCursor.pos()
            if p != self.last_cursor:
                if self.last_cursor is not None:
                    self.cursor_moved_at = now
                self.last_cursor = p
            if now - self.cursor_moved_at < 2.5:
                c = self.mapToGlobal(QPointF(self.fw / 2, self.fh / 2).toPoint())
                dx = max(-1.0, min(1.0, (p.x() - c.x()) / 500))
                dy = max(-1.0, min(1.0, (p.y() - c.y()) / 350))
                return dx, dy
        if now >= self.next_wander:
            if random.random() < 0.35:
                self.wander = (0.0, 0.0)
            else:
                self.wander = (random.uniform(-0.75, 0.75), random.uniform(-0.5, 0.5))
            self.next_wander = now + random.uniform(1.2, 4.5)
        return self.wander

    def _tick(self):
        now = time.monotonic()
        spec = MOODS[self.mood]
        target = dict(BASE)
        target.update({k: v for k, v in spec.items() if k != "gaze"})
        for k, v in target.items():
            self.cur[k] += (v - self.cur[k]) * 0.22

        tx, ty = self._gaze_target(now, spec)
        self.gx += (tx - self.gx) * 0.2
        self.gy += (ty - self.gy) * 0.2

        if self.mood != "asleep" and self.blink_t is None and now >= self.next_blink:
            self.blink_t = now
            # occasionally a quick double blink
            self.next_blink = now + (0.3 if random.random() < 0.15 else random.uniform(2, 6))

        self.amp_s += ((self.amp if self.speaking else 0.0) - self.amp_s) * 0.35
        self.update()

    # ---------------------------------------------------------------- drawing
    def paintEvent(self, _):
        now = time.monotonic()
        s, fw, fh = self.s, self.fw, self.fh
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)

        if self.face_plate:
            p.setPen(QPen(QColor(self.color.red(), self.color.green(), self.color.blue(), 55), 1.5 * s))
            p.setBrush(QColor(18, 20, 26, 238))
            p.drawRoundedRect(QRectF(2, 2, fw - 4, fh - 4), fh * 0.24, fh * 0.24)

        blink = 1.0
        if self.blink_t is not None:
            t = now - self.blink_t
            if t < 0.17:
                blink = 1 - 0.95 * math.sin(math.pi * t / 0.17)
            else:
                self.blink_t = None

        hop = 0.0
        if self.hop_t is not None:
            t = now - self.hop_t
            if t < 0.3:
                hop = -math.sin(math.pi * t / 0.3) * fh * 0.06
            else:
                self.hop_t = None

        pulse = 1 + self.amp_s * 0.14
        ew, eh = fw * 0.25, fh * 0.48
        ox, oy = self.gx * fw * 0.09, self.gy * fh * 0.13
        c = self.cur

        for side, cx in (("l", fw / 2 - fw * 0.2), ("r", fw / 2 + fw * 0.2)):
            w = ew * c[side + "w"]
            h = eh * c[side + "h"] * pulse
            hv = max(h * c["open"] * blink, 3.0 * s)
            x, y = cx - w / 2 + ox, fh / 2 - hv / 2 + oy + hop
            r = min(w, hv) * 0.28

            path = QPainterPath()
            path.addRoundedRect(QRectF(x, y, w, hv), r, r)

            if hv > 6 * s:
                d_in, d_out = c[side + "_in"], c[side + "_out"]
                # left eye: outer corner is on the left; right eye: on the right
                d_left, d_right = (d_out, d_in) if side == "l" else (d_in, d_out)
                if max(d_left, d_right) > 0.01:
                    lid = QPainterPath()
                    lid.addPolygon(QPolygonF([
                        QPointF(x - 2, y - 2), QPointF(x + w + 2, y - 2),
                        QPointF(x + w + 2, y + hv * d_right), QPointF(x - 2, y + hv * d_left)]))
                    lid.closeSubpath()
                    path = path.subtracted(lid)
                if c["bottom"] > 0.01:
                    cheek = QPainterPath()
                    cheek.addEllipse(QRectF(x - w * 0.25, y + hv * (1 - c["bottom"]), w * 1.5, hv * 1.2))
                    path = path.subtracted(cheek)

            p.setBrush(Qt.NoBrush)
            for width, alpha in ((12, 18), (7, 32), (3.5, 60)):   # soft glow
                glow = QColor(self.color)
                glow.setAlpha(alpha)
                p.setPen(QPen(glow, width * s))
                p.drawPath(path)
            p.setPen(Qt.NoPen)
            p.setBrush(self.color)
            p.drawPath(path)

        if self.muted:
            p.setBrush(QColor(255, 80, 80, 220))
            p.drawEllipse(QPointF(fw - 18 * s, 18 * s), 4 * s, 4 * s)

        if self.captions and self.caption and now < self.caption_until:
            rect = QRectF(6 * s, fh + 4 * s, fw - 12 * s, self.cap_h - 8 * s)
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(18, 20, 26, 225))
            p.drawRoundedRect(rect, 12 * s, 12 * s)
            f = QFont()
            f.setPointSizeF(9.5 * s)
            p.setFont(f)
            p.setPen(QColor(235, 240, 245))
            p.drawText(rect.adjusted(10 * s, 4 * s, -10 * s, -4 * s),
                       Qt.AlignCenter | Qt.TextWordWrap, self.caption)
        p.end()
