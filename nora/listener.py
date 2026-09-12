from __future__ import annotations

import asyncio
import logging
import time

import keyboard
import numpy as np
import sounddevice as sd

from nora.config import get_config
from nora.dsp import remove_dc, speech_rms

logger = logging.getLogger("nora.listener")


class Listener:
    def __init__(self) -> None:
        cfg = get_config().get("listener", {})
        self.hotkey = cfg.get("push_to_talk_key", "ctrl+`")
        self.sample_rate = cfg.get("sample_rate", 16000)
        self.max_duration = cfg.get("max_record_sec", 15)
        # Retired: `_record` does not read this. Silence no longer ends a
        # push-to-talk turn at all -- the key release does, and max_duration
        # bounds it. Kept as an attribute only so an old config still loads.
        self.silence_timeout = cfg.get("silence_timeout_sec", 1.5)

        # After a wake word there is no key, so silence is the only end-of-turn
        # signal there is. At the 0.7 s this inherited from PTT it cut you off
        # on the pause between "remind me to" and whatever you were about to
        # remember. A gap has to be longer than a thinking pause and shorter
        # than patience.
        self.wakeword_silence_timeout = cfg.get("wakeword_silence_timeout_sec", 3.0)
        # How long you get to start talking after a wake word before the turn is
        # dropped. This used to have to cover NORA's spoken cue as well as your
        # reaction to it; there is no cue now, so it is purely reaction time,
        # and it is what releases the microphone after a false trigger.
        self.wakeword_speech_start = cfg.get("wakeword_speech_start_sec", 4.0)

        # What counts as speech. A fixed number cannot serve two microphones:
        # the working capture on this machine idles at ~0.03 RMS once its DC
        # offset is removed, so a fixed 0.01 called silence speech and no turn
        # ever ended; the dead input idles at 0.00003, so nothing ever reached
        # 0.01 and every turn was discarded as empty. Leave the threshold unset
        # and it is derived from the noise floor the microphone is actually
        # delivering -- see _record. Set a number to pin it.
        thr = cfg.get("speech_rms_threshold", None)
        self.speech_rms_threshold = (
            None if thr in (None, "", "auto") else float(thr)
        )
        # How far above the floor a block has to sit to count as speech.
        self.speech_over_floor = float(cfg.get("speech_over_floor", 2.5))
        # A derived threshold never goes below this, so an input delivering
        # digital silence cannot have its own noise scaled up into "speech".
        self.speech_floor_min = float(cfg.get("speech_floor_min", 0.02))

        self._text_interrupted = False
        # True while a PTT press we have already recorded is still held down.
        # See listen(): a turn belongs to the press, not to every poll of it.
        self._ptt_latched = False

        # Clap detection settings
        clap_cfg = cfg.get("clap_detection", {})
        self.clap_threshold = clap_cfg.get("threshold", 0.08)
        self.clap_min_gap = clap_cfg.get("min_gap_sec", 0.1)
        self.clap_max_gap = clap_cfg.get("max_gap_sec", 1.0)

    # â"€â"€ Clap detection (for wake sequence) â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€

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

    # â"€â"€ Passive voice listening (for wake phrase after clap) â"€â"€â"€â"€â"€â"€â"€

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

        audio = remove_dc(np.concatenate(frames, axis=0).flatten())
        rms = np.sqrt(np.mean(audio ** 2))
        if rms < 0.003:
            return None

        return audio

    # â"€â"€ Passive listening (continuous) â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€

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

        audio = remove_dc(audio)
        rms = np.sqrt(np.mean(audio ** 2))
        logger.debug(f"Passive chunk: {len(audio)} samples, RMS={rms:.6f} (native: {native_rate}Hz/{native_channels}ch)")

        if rms < 0.001:
            return None

        return audio.astype(np.float32)

    # â"€â"€ Wakeword mode â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€

    async def listen_wakeword(self) -> np.ndarray | None:
        """Check for wakeword trigger; if detected, record the command."""
        from nora import wakeword as _ww

        if not _ww.wait_for_trigger(timeout=0.1):
            return None

        logger.info("Wakeword triggered -- recording command")
        return await self._wake_and_record()

    async def _wake_and_record(self) -> np.ndarray | None:
        """Open the microphone on a wake word. Both ways in come through here.

        NORA used to answer a wake word out loud -- "Sure.", "Okay.", "One
        sec." -- before recording. Nothing NORA plays is spoken here any more,
        because on a laptop her speakers and her microphone are six inches
        apart and the recorder cannot tell her voice from yours. The cue was
        scored as the start of your turn, and the end-of-turn timer then ran
        against it; see `_record` for the half of that which bit hardest.

        Silence at the start of a wake turn is not a hang. `_record` gives you
        `wakeword_speech_start_sec` to begin talking and drops the turn if you
        never do, so a false trigger releases the microphone on its own.
        """
        from nora import wakeword as _ww

        loop = asyncio.get_event_loop()
        try:
            return await loop.run_in_executor(None, self._record, False)
        finally:
            # The detector keeps scoring its own stream throughout the
            # recording. Anything it matched in there is not a new request --
            # usually the user saying the name again at the head of the sentence
            # already being recorded. Left in the event it wakes the very next
            # poll with nobody having asked for anything.
            _ww.drain_trigger()

    # â"€â"€ Wakeword + PTT secondary â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€

    def _check_ptt_now(self) -> bool:
        """Non-blocking: is PTT key or UI button held right now?"""
        try:
            if keyboard.is_pressed(self.hotkey):
                return True
        except Exception:
            pass
        try:
            from nora import ui_server
            if ui_server.is_ptt_pressed():
                return True
        except Exception:
            pass
        return False

    # â"€â"€ Unified listening (branches on PTT / wakeword / passive) â"€â"€â"€â"€â"€

    async def listen(self) -> np.ndarray | None:
        """Record a command utterance.

        Priority:
          1. Wakeword (primary when enabled) — polls the trigger event.
          2. PTT key/button — works as secondary even when wakeword is enabled.
          3. PTT primary mode — blocks on key when wakeword is off.
          4. Passive — continuous rolling chunk (always-on transcription).
        """
        from nora import context, wakeword as _ww

        if _ww.is_enabled():
            # Short-poll wakeword trigger (non-blocking at 50 ms granularity).
            # Do NOT call listen_wakeword() here — it polls the event again and
            # it will already be cleared. Go straight to the shared wake path.
            if _ww.wait_for_trigger(timeout=0.05):
                logger.info("Wakeword confirmed -- recording")
                return await self._wake_and_record()

            # PTT secondary: fire on the press, not on the hold. This branch is
            # polled several times a second, so without the latch one held key
            # opens a new recording on every poll. The key has to come back up
            # before it counts as a new press.
            loop = asyncio.get_event_loop()
            held = await loop.run_in_executor(None, self._check_ptt_now)
            if not held:
                self._ptt_latched = False
                return None  # neither triggered; caller loops
            if self._ptt_latched:
                return None  # the same press, already answered
            self._ptt_latched = True
            return await loop.run_in_executor(None, self._record, True)

        if context.get_ptt_enabled():
            logger.info(f"PTT mode -- press [{self.hotkey}] or UI button to speak...")
            loop = asyncio.get_event_loop()
            self._text_interrupted = False
            await loop.run_in_executor(None, self._wait_for_key_press)
            if self._text_interrupted:
                return None  # pipeline picks up queued text on next iteration
            return await loop.run_in_executor(None, self._record)

        # Passive mode -- return a short chunk of mic audio for transcription
        logger.debug("Passive mode -- sampling mic chunk")
        return await self.listen_passive(chunk_seconds=3.0)

    def _wait_for_key_press(self) -> None:
        """Block until the push-to-talk key, UI button, or queued text input."""
        from nora import text_input as _ti
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
            if not _ti._queue.empty():
                print(f"[NORA] Text queue detected in listener ({_ti._queue.qsize()} item(s)) — interrupting PTT wait", flush=True)
                self._text_interrupted = True
                return
            time.sleep(0.02)

    def _speech_threshold(self, floor: float | None) -> float:
        """The level a block must reach to count as speech.

        `speech_floor_min` is the whole threshold until some quiet has been
        observed, and the floor is what adapts it upward in a room noisier than
        that. Before any floor is known, guessing low is the safe direction: a
        turn wrongly kept is transcribed and discarded downstream, a turn
        wrongly dropped is silence where an answer should be.
        """
        if self.speech_rms_threshold is not None:
            return self.speech_rms_threshold
        if floor is None:
            return self.speech_floor_min
        return max(floor * self.speech_over_floor, self.speech_floor_min)

    def _record(self, ptt_mode: bool = True) -> np.ndarray | None:
        """Record audio until key release (PTT) or VAD silence (wakeword).

        ptt_mode=False: skip key-release check; stop on silence after speech,
        or bail if no speech detected within speech_start_timeout seconds.

        ptt_mode=True: only the key release and `max_duration` end the turn.
        Silence deliberately does not -- see the timeout block below.
        """
        logger.info("Recording...")
        label = "(release key to stop)" if ptt_mode else "(wakeword mode)"
        print(f"[NORA] Recording... {label}", flush=True)
        frames: list[np.ndarray] = []
        silence_start: float | None = None
        speech_detected = False
        speech_start_timeout = self.wakeword_speech_start
        # After a wake word there is no key, so silence is the only end-of-turn
        # signal there is. Under PTT there is a key, and it is a better signal
        # than silence can ever be -- so silence gets no vote at all.
        #
        # It used to get one, as a "backstop", and that backstop is what broke
        # push-to-talk. The recorder cannot tell whose voice it is hearing. NORA
        # answered the press out loud, her own cue came back through the
        # microphone six inches away, `speech_detected` went True on it, and the
        # 0.7 s gap then expired while the user was still drawing breath -- so
        # the turn ended at 1.6 s holding nothing but a recording of NORA saying
        # "Okay.", which Whisper duly transcribed and NORA duly answered. The
        # cue is gone now, but any sharp sound in the room -- the click of the
        # very key being pressed -- could seed the same failure. Push-to-talk
        # means the talking ends when you let go.
        silence_timeout = None if ptt_mode else self.wakeword_silence_timeout
        start_time = time.time()
        # Quietest block seen so far -- the noise floor this microphone is
        # delivering right now, which is what the speech test is measured
        # against. Tracked rather than configured because it differs by an order
        # of magnitude between the inputs on one machine, and moves with the
        # room.
        floor: float | None = None
        loudest = 0.0

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

                    if ptt_mode:
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
                        # Measured in the speech band, with the DC offset
                        # removed. Both matter on this hardware: the bias means
                        # a raw RMS never drops below +0.16 so silence and
                        # speech read alike, and the noise below that band is
                        # loud enough that full-band RMS leaves no room to set
                        # a threshold. See nora/dsp.py.
                        rms = speech_rms(frames[-1], self.sample_rate)
                        loudest = max(loudest, rms)
                        rms_threshold = self._speech_threshold(floor)
                        if rms >= rms_threshold:
                            speech_detected = True
                            silence_start = None
                        else:
                            # Only a block quiet enough to be judged non-speech
                            # may define the noise floor. Letting every block
                            # seed it means a turn that opens with a word sets
                            # the floor to that word and the bar to 2.5x it, so
                            # nothing after ever registers -- the same "no
                            # speech in recording" this whole change exists to
                            # end, just arrived at from the other side.
                            floor = rms if floor is None else min(floor, rms)
                            if not speech_detected:
                                # Nothing has been said yet, so there is no end
                                # of turn to detect. After a wake word the turn
                                # is abandoned once the opening window passes;
                                # under PTT the key is still down and starting
                                # is the user's to do, so we wait.
                                if not ptt_mode and elapsed >= speech_start_timeout:
                                    logger.info("No speech detected after wakeword -- discarding.")
                                    return None
                            elif silence_timeout is not None:
                                if silence_start is None:
                                    silence_start = time.time()
                                elif time.time() - silence_start >= silence_timeout:
                                    logger.info("Silence detected, stopping recording.")
                                    break

        except Exception as e:
            logger.error(f"Recording error: {e}")
            return None

        if not frames:
            return None

        if not speech_detected:
            # Nothing above the speech threshold for the whole recording. Under
            # PTT this used to be returned anyway, and a clip of room tone sent
            # to Whisper comes back as "Thank you." — which NORA then answers,
            # which starts another turn, which records more silence. Wakeword
            # mode already bailed here; PTT never did.
            #
            # The numbers go in the log because this line on its own is the same
            # message whether the microphone is dead, muted, pointed at the
            # wrong input, or simply quieter than the margin allows for -- and
            # those want opposite fixes. floor≈loudest means nothing changed
            # while you spoke; a healthy gap means the margin is too wide.
            logger.info(
                "No speech in recording -- discarding. "
                "(noise floor %.5f, loudest block %.5f, needed %.5f)",
                floor if floor is not None else float("nan"),
                loudest,
                self._speech_threshold(floor),
            )
            return None

        audio = remove_dc(np.concatenate(frames, axis=0).flatten())
        duration = len(audio) / self.sample_rate
        logger.info(f"Recorded {duration:.1f}s of audio.")
        print(f"[NORA] Recorded {duration:.1f}s -- transcribing...", flush=True)
        return audio
