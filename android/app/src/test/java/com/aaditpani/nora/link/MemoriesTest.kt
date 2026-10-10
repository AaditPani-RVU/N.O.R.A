package com.aaditpani.nora.link

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class MemoriesTest {
    @Test
    fun parsesTheCoresList() {
        val body = JSONObject("""{"items":[
            {"id":"kn_1","text":"my locker code is 4417","ts":1760100000.5},
            {"id":"","text":"no id"},
            {"id":"kn_2","text":""},
            {"id":"kn_3","text":"my bike is the blue one","ts":1760000000}]}""")
        val list = MemoryList.parse(body)
        assertEquals(listOf("kn_1", "kn_3"), list.items.map { it.id })
        assertEquals(1760100000500L, list.items[0].tsMs)
        assertNull(list.forgotten)
    }

    @Test
    fun saysWhetherAForgetTook() {
        val list = MemoryList.parse(JSONObject("""{"items":[],"forgotten":true}"""))
        assertTrue(list.forgotten == true)
        assertTrue(list.items.isEmpty())
    }
}
