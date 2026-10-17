package com.aaditpani.nora.phone

import android.Manifest
import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Person
import android.app.RemoteInput
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.graphics.drawable.Icon
import android.media.AudioAttributes
import android.media.RingtoneManager
import com.aaditpani.nora.R
import com.aaditpani.nora.link.ConfirmPrompt
import com.aaditpani.nora.link.LinkState
import com.aaditpani.nora.ui.CallActivity
import com.aaditpani.nora.ui.MainActivity
import java.util.concurrent.atomic.AtomicInteger

object Notifications {
    private const val LINK_CHANNEL = "link"
    private const val MESSAGE_CHANNEL = "messages"
    private const val CONFIRM_CHANNEL = "confirm"
    private const val CALL_CHANNEL = "calls"
    const val LINK_ID = 1
    /** NORA's latest answer to something typed. One at a time: a new answer replaces it. */
    private const val CHAT_ID = 2
    /** NORA ringing. One at a time. */
    private const val CALL_ID = 4
    const val KEY_REPLY = "reply"
    private val nextId = AtomicInteger(100)

    fun createChannels(context: Context) {
        context.getSystemService(NotificationManager::class.java).createNotificationChannels(listOf(
            NotificationChannel(LINK_CHANNEL, "Connection to NORA", NotificationManager.IMPORTANCE_LOW).apply {
                description = "The always-on notification that keeps NORA able to reach this phone"
                setShowBadge(false)
            },
            NotificationChannel(MESSAGE_CHANNEL, "Messages from NORA", NotificationManager.IMPORTANCE_HIGH).apply {
                description = "Reminders, job results, answers, and anything NORA sends to the phone"
            },
            NotificationChannel(CONFIRM_CHANNEL, "NORA asking before she acts", NotificationManager.IMPORTANCE_HIGH).apply {
                description = "NORA wants a yes before doing something. Tap to see what and answer"
            },
            NotificationChannel(CALL_CHANNEL, "Calls from NORA", NotificationManager.IMPORTANCE_HIGH).apply {
                description = "NORA ringing the phone for something that needs you now"
                setSound(RingtoneManager.getDefaultUri(RingtoneManager.TYPE_RINGTONE),
                    AudioAttributes.Builder().setUsage(AudioAttributes.USAGE_NOTIFICATION_RINGTONE)
                        .setContentType(AudioAttributes.CONTENT_TYPE_SONIFICATION).build())
                enableVibration(true)
                vibrationPattern = longArrayOf(0, 1000, 1000)
            },
        ))
    }

    private fun openApp(context: Context): PendingIntent = PendingIntent.getActivity(context, 0,
        Intent(context, MainActivity::class.java).addFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP)
            .putExtra(MainActivity.EXTRA_TAB, MainActivity.TAB_CHAT),
        PendingIntent.FLAG_IMMUTABLE)

    /**
     * Type an answer to NORA from the notification. Needs the phone unlocked:
     * whoever holds a locked phone shouldn't be able to give NORA orders.
     */
    private fun replyAction(context: Context): Notification.Action {
        val input = RemoteInput.Builder(KEY_REPLY).setLabel("Message NORA").build()
        // Mutable because RemoteInput fills in the text; the intent is explicit.
        val pending = PendingIntent.getBroadcast(context, 2,
            Intent(context, ReplyReceiver::class.java),
            PendingIntent.FLAG_MUTABLE or PendingIntent.FLAG_UPDATE_CURRENT)
        return Notification.Action.Builder(null, "Reply", pending)
            .addRemoteInput(input)
            .setAllowGeneratedReplies(false)
            .setAuthenticationRequired(true)
            .build()
    }

    private fun canPost(context: Context) =
        context.checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) == PackageManager.PERMISSION_GRANTED

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

    /** A reminder or a job's answer: each its own notification, with Reply. */
    fun message(context: Context, title: String, body: String) {
        if (!canPost(context)) return
        val n = Notification.Builder(context, MESSAGE_CHANNEL)
            .setSmallIcon(R.drawable.ic_nora)
            .setContentTitle(title)
            .setContentText(body)
            .setStyle(Notification.BigTextStyle().bigText(body))
            .setAutoCancel(true)
            .setContentIntent(openApp(context))
            .addAction(replyAction(context))
            .build()
        context.getSystemService(NotificationManager::class.java).notify(nextId.getAndIncrement(), n)
    }

    /** NORA's answer to something typed while the chat wasn't on screen. */
    fun reply(context: Context, text: String) {
        if (!canPost(context)) return
        val n = Notification.Builder(context, MESSAGE_CHANNEL)
            .setSmallIcon(R.drawable.ic_nora)
            .setContentTitle("NORA")
            .setContentText(text)
            .setStyle(Notification.BigTextStyle().bigText(text))
            .setAutoCancel(true)
            .setContentIntent(openApp(context))
            .addAction(replyAction(context))
            .build()
        context.getSystemService(NotificationManager::class.java).notify(CHAT_ID, n)
    }

    fun clearReply(context: Context) {
        context.getSystemService(NotificationManager::class.java).cancel(CHAT_ID)
    }

    /**
     * After a reply from the notification: Android shows a spinner until the
     * notification is posted again, so post what was sent (quietly).
     */
    fun replySent(context: Context, text: String, sent: Boolean) {
        if (!canPost(context)) return
        val n = Notification.Builder(context, MESSAGE_CHANNEL)
            .setSmallIcon(R.drawable.ic_nora)
            .setContentTitle(if (sent) "Sent to NORA" else "Not sent: NORA isn't connected")
            .setContentText(text)
            .setOnlyAlertOnce(true)
            .setAutoCancel(true)
            .setTimeoutAfter(if (sent) 60_000L else 10 * 60_000L)
            .setContentIntent(openApp(context))
            .build()
        context.getSystemService(NotificationManager::class.java).notify(CHAT_ID, n)
    }

    /**
     * NORA wants a yes. Answering takes opening the app (which needs the phone
     * unlocked) and reading the steps there, so no approve button here.
     */
    fun confirm(context: Context, prompt: ConfirmPrompt) {
        if (!canPost(context)) return
        val n = Notification.Builder(context, CONFIRM_CHANNEL)
            .setSmallIcon(R.drawable.ic_nora)
            .setContentTitle("NORA is asking before she acts")
            .setContentText("Tap to see what, and answer")
            .setCategory(Notification.CATEGORY_REMINDER)
            .setAutoCancel(true)
            .setTimeoutAfter((prompt.expiresAtMs - System.currentTimeMillis()).coerceAtLeast(1000))
            .setContentIntent(openApp(context))
            .build()
        context.getSystemService(NotificationManager::class.java).notify(confirmId(prompt), n)
    }

    fun cancelConfirm(context: Context, prompt: ConfirmPrompt) {
        context.getSystemService(NotificationManager::class.java).cancel(confirmId(prompt))
    }

    private fun confirmId(prompt: ConfirmPrompt) = 10_000 + (prompt.requestId.hashCode() and 0xFFFF)

    /**
     * Something NORA was asked to open but Android wouldn't let it start from
     * the background: one tap opens it (a tap is the user starting it).
     */
    fun tapToOpen(context: Context, title: String, body: String, target: Intent) {
        if (!canPost(context)) return
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

    /**
     * NORA ringing: the system's incoming-call notification, ringing until
     * answered, declined or timed out. With the screen off or locked the
     * full-screen intent opens the call screen; in use, it is a heads-up.
     */
    fun incomingCall(context: Context, call: Calls.Call) {
        if (!canPost(context)) return
        val nora = Person.Builder().setName("NORA").setImportant(true)
            .setIcon(Icon.createWithResource(context, R.drawable.ic_nora)).build()
        val screen = PendingIntent.getActivity(context, 20, CallActivity.intent(context, answer = false),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT)
        val answer = PendingIntent.getActivity(context, 21, CallActivity.intent(context, answer = true),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT)
        val decline = PendingIntent.getBroadcast(context, 22,
            Intent(context, CallReceiver::class.java).setAction(CallReceiver.ACTION_DECLINE),
            PendingIntent.FLAG_IMMUTABLE)
        val n = Notification.Builder(context, CALL_CHANNEL)
            .setSmallIcon(R.drawable.ic_nora)
            .setStyle(Notification.CallStyle.forIncomingCall(nora, decline, answer))
            .setContentText(call.reason)
            .setCategory(Notification.CATEGORY_CALL)
            .setFullScreenIntent(screen, true)
            .setContentIntent(screen)
            .setOngoing(true)
            .setTimeoutAfter(Calls.RING_MS + 5_000)
            .build()
        n.flags = n.flags or Notification.FLAG_INSISTENT   // the ringtone loops, like a call
        context.getSystemService(NotificationManager::class.java).notify(CALL_ID, n)
    }

    fun cancelCall(context: Context) {
        context.getSystemService(NotificationManager::class.java).cancel(CALL_ID)
    }

    fun describe(state: LinkState): String = when (state) {
        is LinkState.Connected -> "Connected to the core"
        LinkState.Connecting -> "Connecting…"
        LinkState.Stopped -> "Not running"
        is LinkState.NotAuthorised -> "The core hasn't approved this phone (${state.message})"
        is LinkState.Waiting -> "Reconnecting — ${state.reason}"
    }
}
