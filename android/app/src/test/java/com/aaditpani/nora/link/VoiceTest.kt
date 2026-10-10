package com.aaditpani.nora.link

import com.aaditpani.nora.voice.BargeIn
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/** Phase 6's phone-side logic, apart from the microphone and the speaker. */
class VoiceTest {
    /** Records what it was told to play, and plays nothing. */
    private class FakeOutput : VoiceOutput {
        val log = mutableListOf<String>()
        override fun startPcm(rate: Int) { log += "start $rate" }
        override fun writePcm(pcm: ByteArray) { log += "pcm ${String(pcm)}" }
        override fun finishPcm() { log += "finish" }
        override fun speak(text: String) { log += "speak $text" }
        override fun stop() { log += "stop" }
    }

    private fun queue(): Pair<SpeechQueue, FakeOutput> {
        val out = FakeOutput()
        return SpeechQueue(out, clock = { 1000L }) to out
    }

    @Test
    fun audioSpecIsReadStrictly() {
        assertEquals(AudioSpec(3, 24000), AudioSpec.parse(JSONObject("""{"stream":3,"format":"pcm_s16le","rate":24000}""")))
        assertNull(AudioSpec.parse(null))
        assertNull(AudioSpec.parse(JSONObject("""{"stream":3,"format":"mp3","rate":24000}""")))
        assertNull(AudioSpec.parse(JSONObject("""{"stream":3,"format":"pcm_s16le","rate":1}""")))
        assertNull(AudioSpec.parse(JSONObject("""{"format":"pcm_s16le","rate":24000}""")))
    }

    @Test
    fun pcmFrameParses() {
        val frame = byteArrayOf(1, 0, 0, 1, 2, 9, 8)
        val (stream, pcm) = PcmFrame.parse(frame)!!
        assertEquals(258, stream)
        assertEquals(listOf<Byte>(9, 8), pcm.toList())
        assertNull(PcmFrame.parse(byteArrayOf(1, 0)))
        assertNull(PcmFrame.parse(byteArrayOf(2, 0, 0, 0, 1)))
        assertNull(PcmFrame.parse(byteArrayOf(1, -1, 0, 0, 0)))   // negative stream
    }

    @Test
    fun linesPlayInOrderAndEarlyAudioIsHeld() {
        val (q, out) = queue()
        q.line("One.", AudioSpec(1, 24000))
        q.line("Two.", AudioSpec(2, 24000))
        q.audio(2, "b1".toByteArray())      // stream 2 arrives while 1 plays: held
        q.audio(1, "a1".toByteArray())
        q.audioEnd(1, ok = true, sent = true)
        q.audioEnd(2, ok = true, sent = true)
        assertEquals(listOf("start 24000", "pcm a1", "finish"), out.log)
        q.segmentDone()
        assertEquals(listOf("start 24000", "pcm a1", "finish", "start 24000", "pcm b1", "finish"), out.log)
    }

    @Test
    fun aLineWithoutAudioIsSpokenByThePhone() {
        val (q, out) = queue()
        q.line("Typed-style line.", null)
        assertEquals(listOf("speak Typed-style line."), out.log)
    }

    @Test
    fun audioThatNeverCameFallsBackToThePhoneVoice() {
        val (q, out) = queue()
        q.line("Playing now.", AudioSpec(1, 24000))
        q.line("Waiting.", AudioSpec(2, 24000))
        q.audioEnd(2, ok = false, sent = false)          // queued line: becomes phone TTS
        q.audioEnd(1, ok = false, sent = false)          // playing line: finishes empty, then speaks
        assertEquals(listOf("start 24000", "finish"), out.log)
        q.segmentDone()
        assertEquals("speak Playing now.", out.log.last())
        q.segmentDone()
        assertEquals("speak Waiting.", out.log.last())
    }

    @Test
    fun idleComesAfterTheLastSegmentAndTheTurn() {
        val (q, _) = queue()
        var idle = 0
        q.onIdle = { idle++ }
        q.line("Only line.", AudioSpec(1, 24000))
        q.audioEnd(1, ok = true, sent = true)
        q.turnDone()
        assertEquals(0, idle)                // still playing
        q.segmentDone()
        assertEquals(1, idle)
        q.turnDone()
        assertEquals(1, idle)                // once
    }

    @Test
    fun aTurnThatSaidNothingIsIdleAtOnce() {
        val (q, _) = queue()
        var idle = false
        q.onIdle = { idle = true }
        q.turnDone()
        assertTrue(idle)
    }

    @Test
    fun stopDropsEverythingAfter() {
        val (q, out) = queue()
        q.line("One.", AudioSpec(1, 24000))
        q.line("Two.", null)
        assertTrue(q.stop())
        q.audio(1, "late".toByteArray())
        q.line("Three.", null)
        q.segmentDone()
        assertEquals(listOf("start 24000", "stop"), out.log)
        assertFalse(q.busy)
    }

    @Test
    fun firstSoundIsRecordedOnce() {
        val (q, _) = queue()
        assertNull(q.firstSoundAt)
        q.soundStarted("core")
        q.soundStarted("phone")
        assertEquals(1000L, q.firstSoundAt)
        assertEquals("core", q.firstVoice)
    }

    @Test
    fun anAckPlaysFirstAndTheAnswerQueuesBehindIt() {
        val (q, out) = queue()
        assertTrue(q.ack("One sec."))
        q.soundStarted("phone")
        q.line("Paris.", null)
        assertEquals(listOf("speak One sec."), out.log)
        q.segmentDone()
        assertEquals(listOf("speak One sec.", "speak Paris."), out.log)
        q.soundStarted("phone")
        // The acknowledgement is timed apart; first sound is the answer's.
        assertEquals(1000L, q.ackSoundAt)
        assertEquals(1000L, q.firstSoundAt)
    }

    @Test
    fun noAckOnceTheAnswerHasStarted() {
        val (q, out) = queue()
        q.line("Paris.", null)
        assertFalse(q.ack("One sec."))
        assertEquals(listOf("speak Paris."), out.log)
        assertNull(q.ackSoundAt)
    }

    @Test
    fun noAckAfterTheTurnEndedOrWasStopped() {
        val (q, _) = queue()
        q.turnDone()
        assertFalse(q.ack("One sec."))
        val (q2, _) = queue()
        q2.stop()
        assertFalse(q2.ack("One sec."))
    }

    @Test
    fun aTurnThatOnlyAckedGoesIdleWhenTheAckEnds() {
        val (q, _) = queue()
        var idle = false
        q.onIdle = { idle = true }
        q.ack("One sec.")
        q.turnDone()
        assertFalse(idle)
        q.segmentDone()
        assertTrue(idle)
    }

    @Test
    fun theAckIsReportedApartFromFirstAudio() {
        val t = VoiceTiming(speechEnd = 10_000, recognised = 10_900, firstSay = 13_000, firstSound = 13_300,
            tts = "phone", route = "phone", ackSound = 11_600)
        val e = t.toEvent()
        assertEquals(1600, e.getInt("ack_ms"))
        assertEquals(3300, e.getInt("first_audio_ms"))
        assertTrue(VoiceTiming(1, 2, null, null, "phone", "phone").toEvent().isNull("ack_ms"))
    }

    @Test
    fun timingIsMeasuredFromTheEndOfSpeech() {
        val t = VoiceTiming(speechEnd = 10_000, recognised = 10_300, firstSay = 11_000, firstSound = 11_250,
            tts = "core", route = "bluetooth")
        assertEquals(1250L, t.firstAudioMs)
        val e = t.toEvent()
        assertEquals(300, e.getInt("stt_ms"))
        assertEquals(1000, e.getInt("core_first_say_ms"))
        assertEquals(1250, e.getInt("first_audio_ms"))
        assertTrue(VoiceTiming(1, 2, null, null, "phone", "phone").toEvent().isNull("first_audio_ms"))
    }

    @Test
    fun statsKeepTheLastTwentyAndTheirMedian() {
        val s = LatencyStats()
        assertNull(s.medianFirstAudio())
        for (i in 1..25) s.add(VoiceTiming(0, 100, 500, i * 100L, "core", "phone"))
        assertEquals(20, s.count())
        // Kept: 600..2500; median of an even count is the mean of the middle two.
        assertEquals(1550L, s.medianFirstAudio())
        s.add(VoiceTiming(0, 100, null, null, "core", "phone"))   // never heard: not counted
        assertEquals(20, s.count())

        val copy = LatencyStats().apply { load(s.save()) }
        assertEquals(1550L, copy.medianFirstAudio())
        assertEquals(100L, copy.medianOf("stt_ms"))
        copy.load("not json")
        assertEquals(0, copy.count())
    }

    @Test
    fun median() {
        assertEquals(2L, LatencyStats.median(listOf(3, 1, 2)))
        assertEquals(2L, LatencyStats.median(listOf(1, 2, 3, 2)))
        assertNull(LatencyStats.median(emptyList()))
    }

    @Test
    fun bargeInIgnoresSteadyEchoButCatchesAVoice() {
        val d = BargeIn.Detector(margin = 12.0)
        // NORA's own voice leaking back at about -45 dBFS for a second: never a barge-in.
        repeat(50) { assertFalse(d.feed(-45.0 + (it % 3))) }
        // The user speaks at -25 dBFS: caught after the hold (8 frames = 160 ms).
        val hits = (1..10).map { d.feed(-25.0) }
        assertEquals(7, hits.indexOfFirst { it })
    }

    @Test
    fun bargeInIgnoresABriefClick() {
        val d = BargeIn.Detector(margin = 12.0)
        repeat(30) { d.feed(-50.0) }
        repeat(3) { assertFalse(d.feed(-20.0)) }
        repeat(20) { assertFalse(d.feed(-50.0)) }
    }

    @Test
    fun bargeInWaitsOutTheWarmup() {
        val d = BargeIn.Detector(margin = 12.0)
        // Loud from the first frame (NORA starting loud): the floor learns it.
        repeat(40) { assertFalse(d.feed(-20.0)) }
    }

    @Test
    fun dbfsOfSilenceAndFullScale() {
        assertEquals(-120.0, BargeIn.dbfs(ShortArray(320), 320), 0.001)
        assertEquals(0.0, BargeIn.dbfs(ShortArray(320) { Short.MIN_VALUE }, 320), 0.01)
    }
}
