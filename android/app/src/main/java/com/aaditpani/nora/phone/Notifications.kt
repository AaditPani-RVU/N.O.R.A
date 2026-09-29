package com.aaditpani.nora.phone

import android.Manifest
import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import com.aaditpani.nora.R
import com.aaditpani.nora.link.LinkState
import com.aaditpani.nora.ui.MainActivity
import java.util.concurrent.atomic.AtomicInteger

object Notifications {
    private const val LINK_CHANNEL = "link"
    private const val MESSAGE_CHANNEL = "messages"
    const val LINK_ID = 1
    private val nextId = AtomicInteger(100)

    fun createChannels(context: Context) {
        context.getSystemService(NotificationManager::class.java).createNotificationChannels(listOf(
            NotificationChannel(LINK_CHANNEL, "Connection to NORA", NotificationManager.IMPORTANCE_LOW).apply {
                description = "The always-on notification that keeps NORA able to reach this phone"
                setShowBadge(false)
            },
            NotificationChannel(MESSAGE_CHANNEL, "Messages from NORA", NotificationManager.IMPORTANCE_HIGH).apply {
                description = "Reminders, job results and anything NORA sends to the phone"
            },
        ))
    }

    private fun openApp(context: Context): PendingIntent = PendingIntent.getActivity(context, 0,
        Intent(context, MainActivity::class.java).addFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP),
        PendingIntent.FLAG_IMMUTABLE)

    /** The foreground-service notification, with the kill switch as its action. */
    fun link(context: Context, state: LinkState, killed: Boolean): Notification {
        val text = when {
            killed -> "Remote control is off. NORA can't act on this phone."
            else -> describe(state)
        }
        val toggle = PendingIntent.getService(context, 1,
            Intent(context, LinkService::class.java).setAction(LinkService.ACTION_TOGGLE_KILL),
            PendingIntent.FLAG_IMMUTABLE)
        return Notification.Builder(context, LINK_CHANNEL)
            .setSmallIcon(R.drawable.ic_nora)
            .setContentTitle(if (killed) "NORA — remote control off" else "NORA")
            .setContentText(text)
            .setOngoing(true)
            .setContentIntent(openApp(context))
            .addAction(Notification.Action.Builder(null,
                if (killed) "Turn remote control on" else "Turn remote control off", toggle).build())
            .build()
    }

    fun message(context: Context, title: String, body: String) {
        if (context.checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED) return
        val n = Notification.Builder(context, MESSAGE_CHANNEL)
            .setSmallIcon(R.drawable.ic_nora)
            .setContentTitle(title)
            .setContentText(body)
            .setStyle(Notification.BigTextStyle().bigText(body))
            .setAutoCancel(true)
            .setContentIntent(openApp(context))
            .build()
        context.getSystemService(NotificationManager::class.java).notify(nextId.getAndIncrement(), n)
    }

    /**
     * Something NORA was asked to open but Android wouldn't let it start from
     * the background: one tap opens it (a tap is the user starting it).
     */
    fun tapToOpen(context: Context, title: String, body: String, target: Intent) {
        if (context.checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED) return
        val id = nextId.getAndIncrement()
        val pending = PendingIntent.getActivity(context, id, target,
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT)
        val n = Notification.Builder(context, MESSAGE_CHANNEL)
            .setSmallIcon(R.drawable.ic_nora)
            .setContentTitle(title)
            .setContentText(body)
            .setAutoCancel(true)
            .setTimeoutAfter(10 * 60 * 1000L)
            .setContentIntent(pending)
            .build()
        context.getSystemService(NotificationManager::class.java).notify(id, n)
    }

    fun describe(state: LinkState): String = when (state) {
        is LinkState.Connected -> "Connected to the core"
        LinkState.Connecting -> "Connecting…"
        LinkState.Stopped -> "Not running"
        is LinkState.NotAuthorised -> "The core hasn't approved this phone (${state.message})"
        is LinkState.Waiting -> "Reconnecting — ${state.reason}"
    }
}
