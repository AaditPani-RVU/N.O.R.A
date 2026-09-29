package com.aaditpani.nora.phone

import android.content.ContentValues
import android.content.Context
import android.database.sqlite.SQLiteDatabase
import android.database.sqlite.SQLiteOpenHelper
import com.aaditpani.nora.link.AuditEntry
import com.aaditpani.nora.link.AuditSink
import com.aaditpani.nora.link.Outbox

/**
 * The phone's own records: its audit log of every invocation, so it can show
 * what happened without trusting the core (plan §7.7), and the event outbox,
 * capped at 500 events and 24 hours (plan §5 "Events").
 */
class PhoneDb(context: Context) : SQLiteOpenHelper(context, "nora_phone.db", null, 1), Outbox, AuditSink {

    override fun onCreate(db: SQLiteDatabase) {
        db.execSQL("""CREATE TABLE audit (
            row INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, invocation_id TEXT NOT NULL,
            capability TEXT NOT NULL, params TEXT NOT NULL, origin TEXT NOT NULL, tier INTEGER NOT NULL,
            outcome TEXT NOT NULL, message TEXT NOT NULL, duration_ms INTEGER NOT NULL)""")
        db.execSQL("CREATE TABLE outbox (id TEXT PRIMARY KEY, created_at INTEGER NOT NULL, frame TEXT NOT NULL)")
    }

    override fun onUpgrade(db: SQLiteDatabase, oldVersion: Int, newVersion: Int) = Unit

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

    fun clear() {
        writableDatabase.run {
            delete("audit", null, null)
            delete("outbox", null, null)
        }
    }

    private companion object {
        const val AUDIT_KEEP = 2000
        const val OUTBOX_MAX = 500
        const val OUTBOX_MAX_AGE_MS = 24 * 60 * 60 * 1000L
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

    fun clear() = prefs.edit().clear().apply()
}
