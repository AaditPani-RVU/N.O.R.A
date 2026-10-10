package com.aaditpani.nora.link

import org.json.JSONObject

/** One thing the user told NORA to remember (Sharp F, the memory screen). */
data class Memory(val id: String, val text: String, val tsMs: Long)

/** The core's `memories` reply: the list as it now stands, and whether a forget took. */
data class MemoryList(val items: List<Memory>, val forgotten: Boolean?) {
    companion object {
        fun parse(body: JSONObject): MemoryList {
            val arr = body.optJSONArray("items")
            val items = (0 until (arr?.length() ?: 0)).mapNotNull { i ->
                val o = arr!!.optJSONObject(i) ?: return@mapNotNull null
                val id = o.optString("id")
                val text = o.optString("text")
                if (id.isEmpty() || text.isEmpty()) null
                else Memory(id, text, (o.optDouble("ts", 0.0) * 1000).toLong())
            }
            return MemoryList(items, if (body.has("forgotten")) body.optBoolean("forgotten") else null)
        }
    }
}
