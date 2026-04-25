from __future__ import annotations

import asyncio
import logging
import time

import keyboard
import numpy as np
import sounddevice as sd

from nora.config import get_config

logger = logging.getLogger("nora.listener")


class Listener:
    def __init__(self) -> None:
        cfg = get_config().get("listener", {})
        self.hotkey = cfg.get("push_to_talk_key", "ctrl+`")
        self.sample_rate = cfg.get("sample_rate", 16000)
        self.silence_timeout = cfg.get("silence_timeout_sec", 1.5)
        self.max_duration = cfg.get("max_record_sec", 15)

        # Clap detection settings
        clap_cfg = cfg.get("clap_detection", {})
        self.clap_threshold = clap_cfg.get("threshold", 0.08)
        self.clap_min_gap = clap_cfg.get("min_gap_sec", 0.1)
        self.clap_max_gap = clap_cfg.get("max_gap_sec", 1.0)

    # â”€â”€ Clap detection (for wake sequence) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    async def wait_for_double_clap(self) -> bool:
        """Continuously listen for two loud spikes (claps) in quick succession."""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self._detect_double_clap)

    def _detect_double_clap(self) -> bool:
        """Block until a double clap is detected."""
        logger.debug("Listening for double clap...")

        spike_times: list[float] = []
        last_peak_log: float = 0.0

        def callback(indata: np.ndarray, frame_count: int, time_info: dict, status: sd.CallbackFlags) -> None:
            nonlocal spike_times, last_peak_log

            mono = indata[:, 0] if indata.ndim > 1 else indata.flatten()
            peak = np.max(np.abs(mono))
            now = time.time()

            # Periodically log peak levels so user can see if mic is working
            if now - last_peak_log > 2.0 and peak > 0.001:
                logger.debug(f"Mic level: {peak:.4f} (clap threshold: {self.clap_threshold})")
                last_peak_log = now

            if peak >= self.clap_threshold:
                # Only count if enough time since last spike (debounce)
                if not spike_times or (now - spike_times[-1]) >= self.clap_min_gap:
                    spike_times.append(now)
                    logger.debug(f"Spike detected! peak={peak:.4f}")

                # Prune old spikes
                spike_times[:] = [t for t in spike_times if now - t < 2.0]

        try:
            with sd.InputStream(
                samplerate=self.sample_rate,
                channels=1,
                dtype="float32",
                callback=callback,
                blocksize=512,
            ):
                while True:
                    time.sleep(0.03)

                    if len(spike_times) >= 2:
                        gap = spike_times[-1] - spike_times[-2]
                        if self.clap_min_gap <= gap <= self.clap_max_gap:
                            logger.info(f"Double clap detected! (gap: {gap:.2f}s)")
                            return True

        except Exception as e:
            logger.error(f"Clap detection error: {e}")
            return False

    # â”€â”€ Passive voice listening (for wake phrase after clap) â”€â”€â”€â”€â”€â”€â”€

    async def listen_for_wake_phrase(self, timeout: float = 4.0) -> np.ndarray | None:
        """Record audio for a short window after double clap."""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self._record_timed, timeout)

    def _record_timed(self, duration: float) -> np.ndarray | None:
        """Record for a fixed duration."""
        frames: list[np.ndarray] = []

        def callback(indata: np.ndarray, frame_count: int, time_info: dict, status: sd.CallbackFlags) -> None:
            frames.append(indata.copy())

        try:
            with sd.InputStream(
                samplerate=self.sample_rate,
                channels=1,
                dtype="float32",
                callback=callback,
                blocksize=1024,
            ):
                time.sleep(duration)
        except Exception as e:
            logger.error(f"Timed recording error: {e}")
            return None

        if not frames:
            return None

        audio = np.concatenate(frames, axis=0).flatten()
        rms = np.sqrt(np.mean(audio ** 2))
        if rms < 0.003:
            return None

        return audio

    # â”€â”€ Passive listening (continuous) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    async def listen_passive(self, chunk_seconds: float = 3.0) -> np.ndarray | None:
        """Record a short audio chunk passively (no key press required)."""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self._record_chunk, chunk_seconds)

    def _record_chunk(self, duration: float) -> np.ndarray | None:
        """Record a fixed-length audio chunk from the mic using native mic settings."""
        frames: list[np.ndarray] = []

        # Get the default input device's native settings
        try:
            dev_info = sd.query_devices(kind="input")
            native_rate = int(dev_info["default_samplerate"])
            native_channels = max(1, dev_info["max_input_channels"])
        except Exception:
            native_rate = 44100
            native_channels = 2

        def callback(indata: np.ndarray, frame_count: int, time_info: dict, status: sd.CallbackFlags) -> None:
            frames.append(indata.copy())

        try:
            with sd.InputStream(
                samplerate=native_rate,
                channels=native_channels,
                dtype="float32",
                callback=callback,
                blocksize=1024,
            ):
                time.sleep(duration)
        except Exception as e:
            logger.error(f"Passive recording error: {e}")
            return None

        if not frames:
            return None

        audio = np.concatenate(frames, axis=0).flatten()

        # Convert to mono if stereo
        if native_channels > 1:
            audio = audio.reshape(-1, native_channels)[:, 0]

        # Resample to 16kHz for Whisper if needed
        if native_rate != self.sample_rate:
            ratio = self.sample_rate / native_rate
            new_len = int(len(audio) * ratio)
            indices = np.linspace(0, len(audio) - 1, new_len).astype(int)
            audio = audio[indices]

        rms = np.sqrt(np.mean(audio ** 2))
        logger.debug(f"Passive chunk: {len(audio)} samples, RMS={rms:.6f} (native: {native_rate}Hz/{native_channels}ch)")

        if rms < 0.001:
            return None

        return audio.astype(np.float32)

    # â”€â”€ Unified listening (branches on PTT mode, read per-call) â”€â”€â”€â”€

    async def listen(self) -> np.ndarray | None:
        """Record a command utterance.

        Branches in real time on `context.get_ptt_enabled()`:
            PTT on  â†’ wait for hotkey/UI button, then record until release.
            PTT off â†’ continuous passive listening (rolling chunk).
        """
        from nora import context

        if context.get_ptt_enabled():
            logger.info(f"PTT mode â€” press [{self.hotkey}] or UI button to speak...")
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, self._wait_for_key_press)
            return await loop.run_in_executor(None, self._record)

        # Passive mode â€” return a short chunk of mic audio for transcription
        logger.debug("Passive mode â€” sampling mic chunk")
        return await self.listen_passive(chunk_seconds=3.0)

    def _wait_for_key_press(self) -> None:
        """Block until the push-to-talk key OR the UI button is pressed."""
        while True:
            try:
                if keyboard.is_pressed(self.hotkey):
                    return
            except Exception:
                pass  # keyboard module may need admin rights; UI PTT still works
            try:
                from nora import ui_server
                if ui_server.is_ptt_pressed():
                    return
            except Exception:
                pass
            time.sleep(0.02)

    def _record(self) -> np.ndarray | None:
        """Record audio from microphone until key release, silence, or max duration."""
        logger.info("Recording...")
        frames: list[np.ndarray] = []
        silence_start: float | None = None
        start_time = time.time()
        rms_threshold = 0.01

        def callback(indata: np.ndarray, frame_count: int, time_info: dict, status: sd.CallbackFlags) -> None:
            frames.append(indata.copy())

        try:
            with sd.InputStream(
                samplerate=self.sample_rate,
                channels=1,
                dtype="float32",
                callback=callback,
                blocksize=1024,
            ):
                while True:
                    time.sleep(0.05)
                    elapsed = time.time() - start_time

                    if elapsed >= self.max_duration:
                        logger.info("Max recording duration reached.")
                        break

                    last_key = self.hotkey.split("+")[-1]
                    key_held = False
                    try:
                        key_held = keyboard.is_pressed(last_key)
                    except Exception:
                        pass
                    ui_held = False
                    try:
                        from nora import ui_server
                        ui_held = ui_server.is_ptt_pressed()
                    except Exception:
                        pass
                    if not key_held and not ui_held and elapsed > 0.3:
                        logger.info("PTT released, stopping recording.")
                        break

                    if frames:
                        recent = frames[-1]
                        rms = np.sqrt(np.mean(recent ** 2))
                        if rms < rms_threshold:
                            if silence_start is None:
                                silence_start = time.time()
                            elif time.time() - silence_start >= self.silence_timeout:
                                logger.info("Silence detected, stopping recording.")
                                break
                        else:
                            silence_start = None

        except Exception as e:
            logger.error(f"Recording error: {e}")
            return None

        if not frames:
            return None

        audio = np.concatenate(frames, axis=0).flatten()
        duration = len(audio) / self.sample_rate
        logger.info(f"Recorded {duration:.1f}s of audio.")
        return audio
