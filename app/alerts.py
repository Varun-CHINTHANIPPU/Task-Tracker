"""A short audible reminder, with a system-beep fallback."""

import io
import math
import os
import struct
import wave

from PySide6.QtCore import QObject, QTemporaryFile, QUrl
from PySide6.QtWidgets import QApplication


class ReminderSound(QObject):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.effect = None
        # This app plays a WAV chime, never video. Avoid FFmpeg probing optional
        # GPU drivers (such as VDPAU/NVIDIA) while leaving audio playback intact.
        os.environ.setdefault("QT_FFMPEG_DECODING_HW_DEVICE_TYPES", ",")
        os.environ.setdefault("QT_FFMPEG_ENCODING_HW_DEVICE_TYPES", ",")
        try:
            from PySide6.QtMultimedia import QSoundEffect
        except (ImportError, OSError):
            return
        # Generate two gentle tones locally; no downloaded audio or extra assets.
        sample_rate = 22050
        samples = []
        for frequency in (660, 0, 880):
            duration = 0.18 if frequency else 0.12
            count = int(sample_rate * duration)
            for index in range(count):
                fade = min(1.0, index / 220, (count - 1 - index) / 220)
                samples.append(
                    int(
                        12000
                        * fade
                        * math.sin(2 * math.pi * frequency * index / sample_rate)
                    )
                )
        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(sample_rate)
            output.writeframes(struct.pack(f"<{len(samples)}h", *samples))
        self.audio_file = QTemporaryFile(self)
        if not self.audio_file.open():
            return
        data = buffer.getvalue()
        if self.audio_file.write(data) != len(data):
            return
        self.audio_file.flush()
        filename = self.audio_file.fileName()
        self.audio_file.close()
        self.effect = QSoundEffect(self)
        self.effect.setVolume(0.65)
        self.effect.setSource(QUrl.fromLocalFile(filename))

    def play(self):
        if self.effect is not None and self.effect.status() == self.effect.Status.Ready:
            self.effect.play()
        else:
            QApplication.beep()
