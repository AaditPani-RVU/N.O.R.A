package com.aaditpani.nora.phone

import android.app.KeyguardManager
import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.media.session.PlaybackState
import com.aaditpani.nora.R
import com.aaditpani.nora.ui.PlayerActivity

/**
 * NORA Player over the lock screen: wake the phone while music plays and the
 * HUD player is what you see, instead of the system's media card.
 *
 * Apps can't open a screen from the background, so the wake posts a silent
 * full-screen-intent notification — the route NORA's call screen already
 * uses — and the player cancels it the moment it opens. Nothing is posted
 * unless the phone is locked and something is actually playing.
 *
 * The screen-on broadcast can only be registered at runtime, so the
 * notification listener holds it: the system keeps that service bound for
 * as long as notification access is on, paired or not.
 */
object LockPlayer {
    private const val CHANNEL = "lock_player"
    private const val ID = 6
    private const val PREFS = "lock_player"
    private const val KEY_ON = "on"

    fun enabled(context: Context): Boolean =
        context.getSharedPreferences(PREFS, Context.MODE_PRIVATE).getBoolean(KEY_ON, true)

    fun setEnabled(context: Context, on: Boolean) {
        context.getSharedPreferences(PREFS, Context.MODE_PRIVATE).edit().putBoolean(KEY_ON, on).apply()
    }

    fun canShow(context: Context): Boolean =
        context.getSystemService(NotificationManager::class.java).canUseFullScreenIntent()

    fun register(context: Context): BroadcastReceiver {
        val r = object : BroadcastReceiver() {
            override fun onReceive(ctx: Context, intent: Intent) {
                if (intent.action == Intent.ACTION_SCREEN_ON) maybeShow(ctx)
            }
        }
        context.registerReceiver(r, IntentFilter(Intent.ACTION_SCREEN_ON))
        return r
    }

    fun maybeShow(context: Context) {
        if (!enabled(context)) return
        if (!context.getSystemService(KeyguardManager::class.java).isKeyguardLocked) return
        val playing = Media.sessions(context).any { it.playbackState?.state == PlaybackState.STATE_PLAYING }
        if (!playing) return
        val nm = context.getSystemService(NotificationManager::class.java)
        if (!nm.canUseFullScreenIntent()) return
        // High importance is what lets a full-screen intent fire; no sound,
        // no vibration, no badge, so all it does is open the player.
        nm.createNotificationChannel(NotificationChannel(CHANNEL, "Player on the lock screen",
            NotificationManager.IMPORTANCE_HIGH).apply {
            description = "Opens NORA Player when you wake the phone while music plays"
            setSound(null, null)
            enableVibration(false)
            setShowBadge(false)
        })
        val screen = PendingIntent.getActivity(context, 30, PlayerActivity.lockIntent(context),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT)
        val n = Notification.Builder(context, CHANNEL)
            .setSmallIcon(R.drawable.ic_nora)
            .setContentTitle("NORA Player")
            .setCategory(Notification.CATEGORY_TRANSPORT)
            .setVisibility(Notification.VISIBILITY_PUBLIC)
            .setFullScreenIntent(screen, true)
            .setContentIntent(screen)
            .setOnlyAlertOnce(true)
            .setTimeoutAfter(4_000)
            .build()
        nm.notify(ID, n)
    }

    /** The player is up; the notification that opened it has nothing left to do. */
    fun opened(context: Context) {
        context.getSystemService(NotificationManager::class.java).cancel(ID)
    }
}
