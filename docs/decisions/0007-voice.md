# ADR-0007: Voice on the phone (Phase 6)

- **Date:** 2026-09-30
- **Status:** implemented; **not yet verified on the Pixel** (see the on-phone checklist below)
- **Author:** Aadit Pani (with Claude Code)
- **Scope:** Android `voice/` (new: `VoiceController`, `PhoneVoiceOutput`, `BargeIn`, `AudioRoute`, `VoiceService`, `Triggers`), `link/Voice.kt` (new), `link/DeviceLink.kt`, `link/Chat.kt`, `phone/{NoraApp,PhoneStore}.kt`, `ui/{MainActivity,Orb}.kt`, manifest, `res/xml/`; core `nora/hub/voice.py` (new), `nora/hub/server.py`, `nora/hub/protocol.py`, `nora/hub/fake_device.py`, `nora/tts_local.py`; `android/tools/{hub_harness,voice_latency}.py`; tests

## Context

Phase 6 of `NORA_DISTRIBUTED_PLAN.md` covers a voice session service, on-device
speech recognition sending text, sentence-streamed speech, barge-in, the
assistant gesture (if it proves feasible) and Bluetooth routing. The exit
criterion is a **median first audio of ≤ 1.5 s over 20 measured turns**, timed
from when you stop talking to when NORA's voice starts.

Typed chat (ADR-0005) already carries a turn from the phone and its answer
lines back. Voice adds three things on top of it: ears on the phone, a mouth
on the phone, and the timing.

## Decision

### A spoken turn is a typed turn with a flag

- The phone recognises speech itself, with
  `SpeechRecognizer.createOnDeviceSpeechRecognizer` (plan §9.3). Only the text
  leaves the phone.
- The text goes up as an ordinary `utterance`, with
  `voice: {tts: "phone" | "core"}` added. It gets the same pipeline, the same
  policy and the same confirmations as a typed message.
- It appears in the chat like a typed message. The voice panel shows the
  words as they are recognised.

### Who speaks the answer: the phone, by default

Two voices are built, and a setting picks one ("NORA's own voice" on the
System tab):

| | Phone voice (**default**) | NORA's own voice |
|---|---|---|
| Engine | Android `TextToSpeech`, en-GB, on-device voice preferred | Kokoro `bf_emma` on the core, the laptop's own voice |
| Carried as | The `say` text, which the phone speaks | The `say` text, then its PCM as binary frames while it is made |
| First sound after `say` | ~0.2 s | Kokoro synthesis of the first chunk: 0.3–1.9 s |

**Why the phone's voice is the default:** Kokoro on this laptop runs at about
0.4× real time. Measured on 2026-09-30 with the live NORA running:

- "Done." takes 0.28 s;
- "Your phone is at 64 percent." takes 0.86 s;
- "It is 11:07 PM on Wednesday, September 30, 2026." takes 1.9 s.

End to end through the real hub and pipeline, fast-path turns (so no model
call) had their **first PCM 0.7–3.0 s after the utterance arrived** (median
1.3 s). Add recognition and the network, and NORA's own voice can't meet the
1.5 s budget even when no model is called. edge-tts isn't faster either:
1.3–1.7 s to its first byte.

The core's voice is still fully built, for when sounding like NORA matters
more than the second:

- **Streaming.** Every `say` carries `audio: {stream, format: "pcm_s16le",
  rate: 24000}`. It is followed by binary frames `0x01 · stream (u32 BE) ·
  PCM` (≤ 32 KB each) and then `audio.end {stream, ok, sent, cancelled}`.
  Raw PCM needs no decoder: the phone writes it to an `AudioTrack` as it
  arrives, and can cut it off mid-word.
- **The first chunk is one clause.** It is cut at the first `,;:—` between
  12 and 40 characters, because Kokoro costs ~250 ms plus ~20 ms per
  character. After that, each sentence is its own chunk.
- **No audio for a line** (Kokoro off, or synthesis failed): the phone speaks
  that line with its own voice. An answer is never silently lost.
- Kokoro is loaded when a device connects, not on its first spoken turn.

### The session on the phone

`VoiceController` runs listen → send → speak → listen again.

- **Starting** is always from the app's screen: the mic button, the **Talk to
  NORA** quick-settings tile, the **assistant gesture**, or a headset's voice
  button (`VOICE_COMMAND`). Each one opens the app, which starts the session.
  Android 14+ only lets a microphone service start while the app is visible,
  and starting this way also guarantees there's never a hidden open mic.
- **`VoiceService`** is a foreground service (microphone + media playback). It
  keeps the session going with the screen off. Its notification says the
  microphone is on and has a Stop button.
- **Follow-ups.** After NORA finishes speaking, the phone listens again. The
  session ends on silence. This is on by default and can be switched off.
- **The answer plays in order.** Each `say` line is a segment in a
  `SpeechQueue`: PCM when the core sends it, the phone's voice otherwise. PCM
  that arrives early is held until its line is due.
- **Audio focus** is held for the whole session (`GAIN_TRANSIENT`), so music
  pauses while you talk and NORA answers.
- **Nothing is spoken twice.** A spoken turn's answer doesn't also post a
  notification.

### Barge-in

While NORA speaks, the microphone listens for the user talking over her.

- It opens as a call would (`VOICE_COMMUNICATION`, plus
  `AcousticEchoCanceler`).
- The level of her own voice leaking back is learnt as a floor. Talking
  counts once the level stays above that floor for 160 ms: 12 dB above it on a
  headset, 20 dB on the phone's speaker, where the echo is worse.
- On barge-in, or a tap on the voice panel:
  - playback stops at once;
  - the phone sends `voice.barge_in` (corr = the utterance), and the core
    stops synthesising for that turn;
  - the phone listens.
- The turn itself runs on, because what was asked may already be happening.
  Its remaining lines still arrive as text.
- Barge-in can be switched off ("Talk over her").

### Bluetooth

- **Output.** Speech is played as the assistant (`USAGE_ASSISTANT`), and the
  system sends that to a connected headset on its own.
- **Input.** While listening, a Bluetooth headset's microphone (SCO or LE
  Audio) is made the communication device. It is released before NORA
  answers, so the answer plays over the headset's music-quality link, not the
  narrow call channel.

### Assistant gesture

NORA can be chosen as the phone's **digital assistant app**
(`NoraAssistantService`, a `VoiceInteractionService`).

- Holding power opens a session that shows nothing of its own. It calls
  `startAssistantActivity` to open the app's voice screen.
- **On a locked phone you unlock first:**
  `supportsLaunchVoiceAssistFromKeyguard="false"`, and the app isn't shown
  over the lock screen. The ADR-0005 rule stands: a locked phone can't give
  NORA orders.
- The system requires an assistant to name a recognition service.
  `NoraRecognitionService` declines every request.
  - Consequence: while NORA is the assistant, an app that uses the system's
    *default* recogniser gets an error. Gboard and Google's apps use their own.
  - NORA's own fallback recogniser is picked explicitly, never the default.
- The plan said to verify feasibility first. What's built is the standard,
  documented route. It is on the checklist below because it can only be
  proven on the phone.

### Measuring the exit criterion

- Each spoken turn produces a `VoiceTiming`:
  - **speech end:** the last moment the recogniser's level was loud, falling
    back to the last new partial words, then to the recogniser's own
    end-of-speech;
  - when recognition was final;
  - when NORA's first line arrived;
  - when the first sound actually started (the first PCM written to a playing
    `AudioTrack`, or the TTS engine's `onStart`).
- Speech end is deliberately *not* the recogniser's end-of-speech, which
  fires after its silence timeout. The user waits through that timeout too,
  so it belongs inside the measurement.
- The phone shows the median over the last 20 turns on the System tab.
- It also sends each turn as a `voice.turn` event. The core logs it and
  appends it to `nora_voice_latency.jsonl` (gitignored), which
  `android/tools/voice_latency.py` summarises.

## Deviations from the plan

| Plan | Here | Why |
|---|---|---|
| Sentence-streamed TTS **via Media3** | Raw PCM into an `AudioTrack`, or the phone's `TextToSpeech` | PCM needs no container or decoder, starts sooner, and stops instantly on barge-in. No new dependency |
| edge-tts audio from the core | Kokoro PCM (the laptop's configured backend), and the phone's voice by default | Neither core voice fits the budget on this CPU (see above) |
| `voice.start` / `voice.text` / `voice.end` messages | `utterance` with a `voice` field | A spoken turn is a typed turn; one path, one set of tests |
| Opus streaming → Whisper fallback mode | Not built | On-device recognition is the primary mode (§9.3). Streaming stays possible as a later mode |
| Voice confirmations | Confirmations still need a tap on the card | Saying "yes" to an unseen step contradicts ADR-0005's rendered-from-steps rule |

## Blast radius & reversibility

- **Core:**
  - Voice only happens when a device sends `voice`. Typed turns are
    unchanged (tested).
  - Binary frames go only to devices that asked for NORA's own voice.
  - The Kokoro pre-load costs ~1.5 s of CPU once per process.
- **App:** 0.6.0 (versionCode 6) installs over 0.5.x and keeps the pairing.
  - New permissions: `RECORD_AUDIO` (asked at the first mic tap), the
    microphone and media-playback FGS types, and `MODIFY_AUDIO_SETTINGS`.
  - Nothing listens until a session is started.
- **Revert:** the core change is additive. Remove the voice field and nothing
  else notices.

## Security & data impact

- **Microphone:**
  - on only during a session the user started from a visible screen, with a
    notification showing and a Stop button;
  - never started by the core, a job or an event;
  - barge-in's mic is open only while NORA is speaking in such a session.
- **Audio never leaves the phone.** Recognition is on-device. Only the text
  goes to the core, as a typed message would.
- **Locked phone:** the assistant gesture needs an unlock first.
- **Stored:** the last 20 turns' timings, on the phone and in
  `nora_voice_latency.jsonl` on the core. These are durations, device id and
  route only; no words.
- **Policy:** a spoken turn is a live-user turn on an authenticated device
  channel, identical to a typed one. Every tier, taint and confirmation rule
  applies unchanged.

## Verification

- **Python: 855 passed, 4 skipped** (837 on `main`). There are 18 new tests
  in `tests/test_voice.py`:
  - chunking;
  - frames;
  - a voice turn over the real hub, each line announced then streamed, with
    the bytes checked;
  - frames under the size limit;
  - typed and phone-voice turns get no audio;
  - no Kokoro means no audio block;
  - failed synthesis says so;
  - barge-in stops synthesis but not the turn;
  - a disconnect cancels synthesis;
  - `voice.turn` is kept, with unknown fields dropped;
  - Kokoro's PCM is little-endian 16-bit.
- **Android JVM: 54 passed** (was 37). `VoiceTest` has 16 tests: speech-queue
  ordering, held audio, phone-voice fallback, idle, stop, timing, the
  20-turn median, and the barge-in detector (echo, voice, click, warm-up).
  `HubIntegrationTest.voiceTurnStreamsPcm` runs a spoken turn against the
  real Python hub and checks every PCM sample.
- `assembleDebug` and `lintDebug` are clean (0 errors; the 21 warnings are
  the existing KTX-style ones).
- **Not verified yet: anything that needs the phone's microphone, speaker or
  settings.** In particular:
  - the real latency number;
  - whether the echo canceller keeps barge-in from firing on NORA's own voice
    through the speaker;
  - whether the on-device recogniser follows a Bluetooth headset's mic;
  - whether the assistant gesture opens the session.

## On-phone checklist (to close Phase 6)

1. Merge into the live tree and restart NORA (`systemctl --user restart
   nora`).
2. Install the new build:
   `adb install -r android/app/build/outputs/apk/debug/app-debug.apk`.
3. Tap the mic in Chat, then allow the microphone. Say "what time is it".
   Expect words appearing live, then the answer spoken, then listening again,
   then the session ending on silence.
4. Ask something long ("tell me about the Pixel 10") and talk over the
   answer. Expect NORA to stop within about a quarter second and listen. Try
   it on the speaker and on earbuds. Also check she does **not** stop herself
   when nobody talks.
5. System tab → switch on NORA's own voice. Ask again and expect the
   laptop's voice. Switch it back off.
6. With Bluetooth earbuds connected, expect the answer in the earbuds and
   the question heard through the earbuds' mic. The route in the last
   measurement should read `bluetooth`.
7. System tab → Digital assistant → choose NORA. Hold power and expect the
   voice screen, listening. Lock the phone, hold power, and expect an unlock
   first.
8. Add the **Talk to NORA** quick-settings tile and check it starts a
   session.
9. Do 20 turns in a normal mix of questions, then on the laptop run
   `.venv/bin/python android/tools/voice_latency.py`. The exit criterion is
   a **median first audio ≤ 1.5 s**.

## Consequences & follow-ups

1. Turns that need the model (0.9–2.1 s on Groq, ADR-0006) will be over the
   budget by themselves. Whether the median passes depends on the mix, and
   the fast path answers most everyday questions. If it doesn't pass:
   - stream the model's reply and speak its first sentence early; or
   - have the phone say a short local acknowledgement once the core has been
     silent ~600 ms. That one is cheap, but it moves the goalposts, so it
     should be reported separately from "first word of the answer".
2. Kokoro would fit the budget on a faster CPU or GPU. Nothing on the phone
   would change.
3. The on-device recogniser may ignore the requested 700 ms end-of-speech
   silence. The measurement starts at the last loud frame, so any extra wait
   shows up in the number instead of hiding.
