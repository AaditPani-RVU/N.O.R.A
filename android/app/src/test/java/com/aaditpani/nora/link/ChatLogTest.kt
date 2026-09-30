package com.aaditpani.nora.link

import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class ChatLogTest {
    private class MemStore : ChatStore {
        val rows = LinkedHashMap<String, ChatMessage>()
        override fun loadChat(limit: Int) = rows.values.toList().takeLast(limit)
        override fun saveChat(message: ChatMessage) { rows[message.id] = message }
        override fun removeChat(id: String) { rows.remove(id) }
        override fun clearChat() = rows.clear()
    }

    @Test
    fun answersAttachToTheMessageTheyAnswer() {
        val store = MemStore()
        val log = ChatLog(store)
        log.mine("m1", "remember I have to submit the assignment tomorrow", Delivery.SENT)
        log.nora("m1", "Got it: Submit the assignment, due tomorrow.")
        log.done("m1")

        val msgs = log.messages.value
        assertEquals(listOf(Sender.ME, Sender.NORA), msgs.map { it.sender })
        assertEquals(Delivery.ANSWERED, msgs[0].delivery)
        assertEquals(listOf("Got it: Submit the assignment, due tomorrow."), log.answersTo("m1"))
        // Written through: a restart shows the same thing.
        assertEquals(msgs, ChatLog(store).messages.value)
    }

    @Test
    fun aTurnThatSaidNothingStopsWaiting() {
        val log = ChatLog(MemStore())
        log.mine("m1", "pause", Delivery.SENT)
        log.done("m1")
        assertEquals(Delivery.ANSWERED, log.get("m1")!!.delivery)
    }

    @Test
    fun whatWasWaitingWhenTheLinkDroppedIsMarkedFailed() {
        val log = ChatLog(MemStore())
        log.mine("m1", "hello", Delivery.SENT)
        log.mine("m2", "done already", Delivery.SENT)
        log.done("m2")
        log.dropInFlight()
        assertEquals(Delivery.FAILED, log.get("m1")!!.delivery)
        assertEquals(Delivery.ANSWERED, log.get("m2")!!.delivery)
    }

    @Test
    fun whatWasWaitingWhenTheAppDiedIsFailedOnLoad() {
        val store = MemStore()
        ChatLog(store).mine("m1", "hello", Delivery.SENT)
        val reopened = ChatLog(store)
        assertEquals(Delivery.FAILED, reopened.get("m1")!!.delivery)
        assertEquals(Delivery.FAILED, store.rows["m1"]!!.delivery)
    }

    @Test
    fun noticesKeepTheirKindAndTheLogIsCapped() {
        val log = ChatLog(MemStore(), keep = 3)
        log.notice("Reminder, sir: call mom", "reminder")
        repeat(4) { log.mine("m$it", "x$it", Delivery.SENT) }
        val msgs = log.messages.value
        assertEquals(3, msgs.size)
        assertEquals(listOf("m1", "m2", "m3"), msgs.map { it.id })
    }

    @Test
    fun removeAndClear() {
        val store = MemStore()
        val log = ChatLog(store)
        log.mine("m1", "x", Delivery.FAILED)
        log.remove("m1")
        assertNull(log.get("m1"))
        assertTrue(store.rows.isEmpty())
        log.notice("hi", "answer")
        log.clear()
        assertTrue(log.messages.value.isEmpty() && store.rows.isEmpty())
    }

    @Test
    fun confirmStepsAreReadStrictly() {
        val ok = ConfirmSteps.parse(JSONArray("""[{"action":"phone.set_alarm","params":{"hour":6,"minute":30}},
            {"action":"send_email"}]"""))!!
        assertEquals("phone.set_alarm", ok[0].first)
        assertEquals(0, ok[1].second.length())
        assertNull(ConfirmSteps.parse(null))
        assertNull(ConfirmSteps.parse(JSONArray()))
        assertNull(ConfirmSteps.parse(JSONArray("""[{"params":{}}]""")))
        assertNull(ConfirmSteps.parse(JSONArray("""[{"action":"x","params":"rm -rf"}]""")))
        assertNull(ConfirmSteps.parse(JSONArray((0..20).map { JSONObject().put("action", "a") })))
    }

    @Test
    fun confirmStepsInWords() {
        assertEquals("Set alarm on the phone — hour: 6, minute: 30",
            ConfirmSteps.describe("phone.set_alarm", JSONObject().put("minute", 30).put("hour", 6)))
        assertEquals("Open url on the phone — url: https://example.com",
            ConfirmSteps.describe("phone.open_url", JSONObject().put("url", "https://example.com")))
        assertEquals("Lock screen", ConfirmSteps.describe("lock_screen", JSONObject()))
    }
}
