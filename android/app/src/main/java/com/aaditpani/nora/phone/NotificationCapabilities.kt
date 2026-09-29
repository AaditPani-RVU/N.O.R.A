package com.aaditpani.nora.phone

import android.app.Notification
import android.app.NotificationManager
import android.content.ComponentName
import android.content.Context
import android.service.notification.NotificationListenerService
import android.service.notification.StatusBarNotification
import com.aaditpani.nora.link.Capability
import com.aaditpani.nora.link.CapabilityResult
import com.aaditpani.nora.link.ErrorCode
import com.aaditpani.nora.link.Note
import com.aaditpani.nora.link.NoteDigest
import org.json.JSONArray
import org.json.JSONObject

/**
 * The phone's notifications as NORA may read them. Held in memory only — a
 * ring of what was posted in the last 24 hours plus what is on screen now —
 * and gone when the process is (plan §7.9). Nothing here is written to disk
 * or sent anywhere until the core asks, and then only what it asked for.
 *
 * Left out: NORA's own, ongoing ones (media players, downloads, the link),
 * group summaries, anything an app marked secret, and apps the user switched
 * off in NORA. OTP notifications arrive from Android already redacted.
 */
class NoraNotificationListener : NotificationListenerService() {
    override fun onListenerConnected() {
        connected = true
        runCatching { activeNotifications }.getOrNull()?.forEach { record(it, active = true) }
    }

    override fun onListenerDisconnected() {
        connected = false
    }

    override fun onNotificationPosted(sbn: StatusBarNotification) = record(sbn, active = true)

    override fun onNotificationRemoved(sbn: StatusBarNotification) {
        synchronized(ring) { ring[sbn.key]?.let { ring[sbn.key] = it.copy(second = false) } }
    }

    private fun record(sbn: StatusBarNotification, active: Boolean) {
        val note = toNote(sbn) ?: return
        synchronized(ring) {
            ring.remove(sbn.key)
            ring[sbn.key] = note to active
            val cutoff = System.currentTimeMillis() - KEEP_MS
            ring.entries.removeAll { it.value.first.postedAt < cutoff }
            while (ring.size > MAX) ring.remove(ring.keys.first())
        }
    }

    private fun toNote(sbn: StatusBarNotification): Note? {
        val n = sbn.notification
        if (sbn.packageName == packageName) return null
        if (sbn.isOngoing || n.flags and Notification.FLAG_GROUP_SUMMARY != 0) return null
        if (n.visibility == Notification.VISIBILITY_SECRET) return null
        val extras = n.extras
        val title = (extras.getCharSequence(Notification.EXTRA_CONVERSATION_TITLE)
            ?: extras.getCharSequence(Notification.EXTRA_TITLE))?.toString().orEmpty()
        val text = (extras.getCharSequence(Notification.EXTRA_BIG_TEXT)
            ?: extras.getCharSequence(Notification.EXTRA_TEXT))?.toString().orEmpty()
        if (title.isBlank() && text.isBlank()) return null
        return Note(sbn.key, sbn.packageName, Media.appLabel(this, sbn.packageName),
            NoteDigest.clip(title, NoteDigest.MAX_TITLE), NoteDigest.clip(text, NoteDigest.MAX_TEXT),
            sbn.postTime)
    }

    companion object {
        private const val KEEP_MS = 24 * 60 * 60 * 1000L
        private const val MAX = 300

        /** key → (note, still on screen). Insertion-ordered: oldest first. */
        private val ring = LinkedHashMap<String, Pair<Note, Boolean>>()

        @Volatile
        var connected = false
            private set

        fun hasAccess(context: Context): Boolean =
            context.getSystemService(NotificationManager::class.java)
                .isNotificationListenerAccessGranted(component(context))

        fun component(context: Context) = ComponentName(context, NoraNotificationListener::class.java)

        fun snapshot(includeDismissed: Boolean): List<Note> = synchronized(ring) {
            ring.values.filter { includeDismissed || it.second }.map { it.first }
        }

        /** Apps seen recently, for the privacy switches in the app. package → label. */
        fun seenApps(): Map<String, String> = synchronized(ring) {
            ring.values.associate { it.first.packageName to it.first.app }
        }

        fun clear() = synchronized(ring) { ring.clear() }
    }
}

/** Spec example 2: "what's on my notifications?" Tier 0, and its output is untrusted. */
class ReadNotifications(private val context: Context, private val blocked: () -> Set<String>) : Capability {
    override val name = "phone.read_notifications"
    override val description = "The phone's notifications, newest first (\"anything important?\")"
    override val tier = 0
    override val untrustedOutput = true
    override val paramsSchema: JSONObject = JSONObject("""
        {"type":"object",
         "properties":{"app":{"type":"string","maxLength":60},
                       "limit":{"type":"integer","minimum":1,"maximum":30},
                       "since_minutes":{"type":"integer","minimum":1,"maximum":1440},
                       "include_dismissed":{"type":"boolean"}}}""")

    override suspend fun execute(params: JSONObject): CapabilityResult {
        if (!NoraNotificationListener.hasAccess(context)) {
            return CapabilityResult.Failed(ErrorCode.PERMISSION_DENIED,
                "NORA doesn't have notification access on the phone yet. Switch it on from the NORA app.",
                userAction = "grant_notification_access")
        }
        if (!NoraNotificationListener.connected) {
            NotificationListenerService.requestRebind(NoraNotificationListener.component(context))
            return CapabilityResult.Failed(ErrorCode.EXECUTION_FAILED,
                "The phone's notification reader is still starting. Ask again in a moment.", retryable = true)
        }
        val now = System.currentTimeMillis()
        val app = params.optString("app").ifBlank { null }
        val off = blocked()
        val dismissed = params.optBoolean("include_dismissed", false)
        val notes = NoteDigest.select(
            NoraNotificationListener.snapshot(dismissed).filter { it.packageName !in off },
            now, params.optInt("since_minutes", 24 * 60) * 60_000L, app, params.optInt("limit", 15))
        val items = JSONArray()
        notes.forEach {
            items.put(JSONObject().put("app", it.app).put("title", it.title).put("text", it.text)
                .put("minutes_ago", (now - it.postedAt) / 60_000))
        }
        return CapabilityResult.Ok(JSONObject()
            .put("items", items)
            .put("untrusted", true)
            .put("message", NoteDigest.message(notes, now, app)))
    }
}
