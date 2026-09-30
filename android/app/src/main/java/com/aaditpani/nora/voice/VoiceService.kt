package com.aaditpani.nora.voice

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.os.IBinder
import android.util.Log
import com.aaditpani.nora.R
import com.aaditpani.nora.phone.controller
import com.aaditpani.nora.ui.MainActivity

/**
 * Holds a voice session in the foreground (microphone + media playback), so
 * it carries on when the screen goes off or the user leaves the app while
 * NORA answers. Android 14+ only lets a microphone service start while the
 * app is visible, and every voice session starts from the app's screen, so
 * this never starts from the background. Its notification is the way to stop.
 */
class VoiceService : Service() {
    override fun onBind(intent: Intent?): IBinder? = null

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        if (intent?.action == ACTION_STOP) {
            controller.voice.stop()
            stopSelf()
            return START_NOT_STICKY
        }
        try {
            startForeground(ID, notification(this), ServiceInfo.FOREGROUND_SERVICE_TYPE_MICROPHONE or
                ServiceInfo.FOREGROUND_SERVICE_TYPE_MEDIA_PLAYBACK)
        } catch (e: Exception) {
            // Not allowed from where we were started: end the session rather than
            // listen without the user able to see it.
            Log.w("NoraVoice", "voice service refused: ${e.message}")
            controller.voice.stop()
            stopSelf()
        }
        return START_NOT_STICKY
    }

    companion object {
        private const val ID = 3
        private const val CHANNEL = "voice"
        private const val ACTION_STOP = "com.aaditpani.nora.STOP_VOICE"

        fun start(context: Context) {
            context.startForegroundService(Intent(context, VoiceService::class.java))
        }

        fun stop(context: Context) {
            context.stopService(Intent(context, VoiceService::class.java))
        }

        private fun notification(context: Context): Notification {
            val nm = context.getSystemService(NotificationManager::class.java)
            nm.createNotificationChannel(NotificationChannel(CHANNEL, "Talking to NORA",
                NotificationManager.IMPORTANCE_LOW).apply {
                description = "Shown while a voice conversation with NORA is open"
                setShowBadge(false)
            })
            val open = PendingIntent.getActivity(context, 3,
                Intent(context, MainActivity::class.java).addFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP),
                PendingIntent.FLAG_IMMUTABLE)
            val stop = PendingIntent.getService(context, 4,
                Intent(context, VoiceService::class.java).setAction(ACTION_STOP), PendingIntent.FLAG_IMMUTABLE)
            return Notification.Builder(context, CHANNEL)
                .setSmallIcon(R.drawable.ic_nora)
                .setContentTitle("Talking to NORA")
                .setContentText("The microphone is on for this conversation")
                .setOngoing(true)
                .setContentIntent(open)
                .addAction(Notification.Action.Builder(null, "Stop", stop).build())
                .build()
        }
    }
}
