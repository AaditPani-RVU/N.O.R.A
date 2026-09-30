package com.aaditpani.nora.voice

import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.os.Bundle
import android.service.quicksettings.TileService
import android.service.voice.VoiceInteractionService
import android.service.voice.VoiceInteractionSession
import android.service.voice.VoiceInteractionSessionService
import android.speech.RecognitionService
import android.speech.SpeechRecognizer
import com.aaditpani.nora.ui.MainActivity

/** Ways to start talking to NORA. Each one opens the app, which starts the session. */
object Talk {
    const val ACTION = "com.aaditpani.nora.TALK"

    fun intent(context: Context): Intent = Intent(context, MainActivity::class.java)
        .setAction(ACTION)
        .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_SINGLE_TOP)

    /** Intents that mean "start listening": ours, the assist gesture's, a headset button's. */
    fun wants(intent: Intent?): Boolean = intent?.action in setOf(ACTION, Intent.ACTION_ASSIST,
        Intent.ACTION_VOICE_COMMAND, "android.intent.action.VOICE_ASSIST")
}

/** Quick-settings tile: "Talk to NORA". */
class TalkTileService : TileService() {
    override fun onClick() {
        super.onClick()
        startActivityAndCollapse(PendingIntent.getActivity(this, 5, Talk.intent(this),
            PendingIntent.FLAG_IMMUTABLE))
    }
}

/**
 * NORA as the phone's digital assistant (Settings → Apps → Default apps →
 * Digital assistant app). Long-press power, or the corner swipe, then opens a
 * voice session. The system binds this while NORA is the chosen assistant.
 */
class NoraAssistantService : VoiceInteractionService()

class NoraAssistantSessionService : VoiceInteractionSessionService() {
    override fun onNewSession(args: Bundle?): VoiceInteractionSession = NoraAssistantSession(this)
}

/**
 * The assistant gesture's session. It draws nothing of its own: it opens the
 * app's voice screen and gets out of the way. On a locked phone the app
 * isn't allowed over the lock screen, so the user unlocks first: nobody
 * holding a locked phone can give NORA spoken orders.
 */
class NoraAssistantSession(context: Context) : VoiceInteractionSession(context) {
    override fun onShow(args: Bundle?, showFlags: Int) {
        super.onShow(args, showFlags)
        startAssistantActivity(Talk.intent(context))
        hide()
    }
}

/**
 * Required of every assistant app by the system; NORA doesn't offer speech
 * recognition to other apps, so it declines every request.
 */
class NoraRecognitionService : RecognitionService() {
    override fun onStartListening(recognizerIntent: Intent?, listener: Callback?) {
        listener?.error(SpeechRecognizer.ERROR_CLIENT)
    }
    override fun onCancel(listener: Callback?) {}
    override fun onStopListening(listener: Callback?) {}
}
