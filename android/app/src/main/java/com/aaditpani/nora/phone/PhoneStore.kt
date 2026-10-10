package com.aaditpani.nora.phone

import android.content.ContentValues
import android.content.Context
import android.database.sqlite.SQLiteDatabase
import android.database.sqlite.SQLiteOpenHelper
import com.aaditpani.nora.link.AuditEntry
import com.aaditpani.nora.link.AuditSink
import com.aaditpani.nora.link.ChatMessage
import com.aaditpani.nora.link.ChatStore
import com.aaditpani.nora.link.Delivery
import com.aaditpani.nora.link.Outbox
import com.aaditpani.nora.link.Sender

/**
 * The phone's own records: its audit log of every invocation, so it can show
 * what happened without trusting the core (plan §7.7), and the event outbox,
 * capped at 500 events and 24 hours (plan §5 "Events"), and the chat with
 * NORA (the last [CHAT_KEEP] lines).
 */
class PhoneDb(context: Context) : SQLiteOpenHelper(context, "nora_phone.db", null, 2),
    Outbox, AuditSink, ChatStore {

    override fun onCreate(db: SQLiteDatabase) {
        db.execSQL("""CREATE TABLE audit (
            row INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, invocation_id TEXT NOT NULL,
            capability TEXT NOT NULL, params TEXT NOT NULL, origin TEXT NOT NULL, tier INTEGER NOT NULL,
            outcome TEXT NOT NULL, message TEXT NOT NULL, duration_ms INTEGER NOT NULL)""")
        db.execSQL("CREATE TABLE outbox (id TEXT PRIMARY KEY, created_at INTEGER NOT NULL, frame TEXT NOT NULL)")
        createChat(db)
    }

    override fun onUpgrade(db: SQLiteDatabase, oldVersion: Int, newVersion: Int) {
        if (oldVersion < 2) createChat(db)
    }

    private fun createChat(db: SQLiteDatabase) {
        db.execSQL("""CREATE TABLE chat (
            row INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT NOT NULL UNIQUE, ts INTEGER NOT NULL,
            sender TEXT NOT NULL, text TEXT NOT NULL, ref TEXT NOT NULL, delivery TEXT NOT NULL)""")
    }

    /** Called after each new audit row, so the audit screen can refresh. */
    var onAudit: () -> Unit = {}

    override fun record(entry: AuditEntry) {
        writableDatabase.run {
            insert("audit", null, ContentValues().apply {
                put("ts", entry.ts); put("invocation_id", entry.invocationId)
                put("capability", entry.capability); put("params", entry.params.take(2000))
                put("origin", entry.origin); put("tier", entry.tier); put("outcome", entry.outcome)
                put("message", entry.message.take(500)); put("duration_ms", entry.durationMs)
            })
            execSQL("DELETE FROM audit WHERE row <= (SELECT MAX(row) FROM audit) - $AUDIT_KEEP")
        }
        onAudit()
    }

    fun recentAudit(limit: Int = 200): List<AuditEntry> =
        readableDatabase.rawQuery(
            "SELECT ts, invocation_id, capability, params, origin, tier, outcome, message, duration_ms" +
                " FROM audit ORDER BY row DESC LIMIT ?", arrayOf(limit.toString())).use { c ->
            buildList {
                while (c.moveToNext()) add(AuditEntry(c.getLong(0), c.getString(1), c.getString(2),
                    c.getString(3), c.getString(4), c.getInt(5), c.getString(6), c.getString(7), c.getLong(8)))
            }
        }

    override fun add(id: String, frame: String) {
        val now = System.currentTimeMillis()
        writableDatabase.run {
            insertWithOnConflict("outbox", null, ContentValues().apply {
                put("id", id); put("created_at", now); put("frame", frame)
            }, SQLiteDatabase.CONFLICT_IGNORE)
            delete("outbox", "created_at < ?", arrayOf((now - OUTBOX_MAX_AGE_MS).toString()))
            execSQL("DELETE FROM outbox WHERE id NOT IN" +
                " (SELECT id FROM outbox ORDER BY created_at DESC LIMIT $OUTBOX_MAX)")
        }
    }

    override fun remove(id: String) {
        writableDatabase.delete("outbox", "id = ?", arrayOf(id))
    }

    override fun pending(): List<Pair<String, String>> =
        readableDatabase.rawQuery("SELECT id, frame FROM outbox ORDER BY created_at", null).use { c ->
            buildList { while (c.moveToNext()) add(c.getString(0) to c.getString(1)) }
        }

    override fun loadChat(limit: Int): List<ChatMessage> =
        readableDatabase.rawQuery(
            "SELECT id, ts, sender, text, ref, delivery FROM chat ORDER BY row DESC LIMIT ?",
            arrayOf(limit.toString())).use { c ->
            buildList {
                while (c.moveToNext()) {
                    val sender = runCatching { Sender.valueOf(c.getString(2)) }.getOrNull() ?: continue
                    val delivery = runCatching { Delivery.valueOf(c.getString(5)) }.getOrDefault(Delivery.ANSWERED)
                    add(ChatMessage(c.getString(0), c.getLong(1), sender, c.getString(3), c.getString(4), delivery))
                }
            }.reversed()
        }

    /** Insert, or update in place (keeping its position) when the id exists. */
    override fun saveChat(message: ChatMessage) {
        val values = ContentValues().apply {
            put("ts", message.ts); put("sender", message.sender.name)
            put("text", message.text.take(4000)); put("ref", message.ref); put("delivery", message.delivery.name)
        }
        writableDatabase.run {
            if (update("chat", values, "id = ?", arrayOf(message.id)) == 0) {
                values.put("id", message.id)
                insert("chat", null, values)
                execSQL("DELETE FROM chat WHERE row <= (SELECT MAX(row) FROM chat) - $CHAT_KEEP")
            }
        }
    }

    override fun removeChat(id: String) {
        writableDatabase.delete("chat", "id = ?", arrayOf(id))
    }

    override fun clearChat() {
        writableDatabase.delete("chat", null, null)
    }

    fun clear() {
        writableDatabase.run {
            delete("audit", null, null)
            delete("outbox", null, null)
            delete("chat", null, null)
        }
    }

    companion object {
        const val CHAT_KEEP = 300
        private const val AUDIT_KEEP = 2000
        private const val OUTBOX_MAX = 500
        private const val OUTBOX_MAX_AGE_MS = 24 * 60 * 60 * 1000L
    }
}

/** Pairing and switches. Nothing secret: the key lives in Keystore. */
class LinkPrefs(context: Context) {
    private val prefs = context.getSharedPreferences("nora_link", Context.MODE_PRIVATE)

    var url: String?
        get() = prefs.getString("url", null)
        set(v) = prefs.edit().putString("url", v).apply()
    var deviceId: String?
        get() = prefs.getString("device_id", null)
        set(v) = prefs.edit().putString("device_id", v).apply()
    var killed: Boolean
        get() = prefs.getBoolean("killed", false)
        set(v) = prefs.edit().putBoolean("killed", v).apply()
    /** Capabilities the user switched off. Off-list rather than on-list, so a new one starts on. */
    var disabled: Set<String>
        get() = prefs.getStringSet("disabled", emptySet())!!.toSet()
        set(v) = prefs.edit().putStringSet("disabled", v).apply()
    /** Apps whose notifications NORA may not read. Off-list: the phone's own choice, per app. */
    var hiddenNoteApps: Set<String>
        get() = prefs.getStringSet("hidden_note_apps", emptySet())!!.toSet()
        set(v) = prefs.edit().putStringSet("hidden_note_apps", v).apply()

    /**
     * Whose voice answers a spoken turn: "phone" (the default: starts in
     * ~0.2 s) or "core" (NORA's own Kokoro voice, ~1 s later on the core's CPU).
     */
    var voiceTts: String
        get() = prefs.getString("voice_tts", "phone") ?: "phone"
        set(v) = prefs.edit().putString("voice_tts", v).apply()
    var bargeIn: Boolean
        get() = prefs.getBoolean("barge_in", true)
        set(v) = prefs.edit().putBoolean("barge_in", v).apply()
    /** Listen again after NORA answers, until the user goes quiet. */
    var followUp: Boolean
        get() = prefs.getBoolean("follow_up", true)
        set(v) = prefs.edit().putBoolean("follow_up", v).apply()
    /** Say "One sec" in the phone's voice when the core is slow to answer. */
    var ack: Boolean
        get() = prefs.getBoolean("voice_ack", true)
        set(v) = prefs.edit().putBoolean("voice_ack", v).apply()
    var voiceStats: String?
        get() = prefs.getString("voice_stats", null)
        set(v) = prefs.edit().putString("voice_stats", v).apply()

    fun clear() = prefs.edit().clear().apply()
}
