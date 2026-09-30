package com.aaditpani.nora.link

import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import org.json.JSONArray
import org.json.JSONObject

/** Who a line in the chat is from. NOTICE is NORA speaking unprompted: a reminder, a job's answer. */
enum class Sender { ME, NORA, NOTICE }

/** Where one of my messages is. Only ME lines move through these. */
enum class Delivery { SENDING, SENT, ANSWERED, FAILED }

data class ChatMessage(
    val id: String,
    val ts: Long,
    val sender: Sender,
    val text: String,
    /** NORA: the id of the message this answers. NOTICE: its kind ("reminder", "answer"). */
    val ref: String = "",
    val delivery: Delivery = Delivery.ANSWERED,
)

/**
 * The core asking this phone's user before it acts (plan §5 "Confirmation").
 * The phone shows [steps], its own rendering of what will run, rather than
 * trusting [rendered]; [rendered] is only the core's summary line.
 */
data class ConfirmPrompt(
    /** The `confirm_request` frame's id; the answer is correlated to it. */
    val requestId: String,
    /** The turn or invocation id the approval is recorded under. */
    val ref: String,
    val steps: List<Pair<String, JSONObject>>,
    val rendered: String,
    val tier: Int,
    val expiresAtMs: Long,
)

/** What the core says to this phone outside an invocation. */
interface ChatListener {
    /** One line of the answer to a typed message; [replyTo] is that message's id. */
    fun onSay(replyTo: String?, text: String)
    /** The turn [replyTo] started has finished; [outcome] is the core's `TurnOutcome.kind`. */
    fun onTurnDone(replyTo: String?, outcome: String)
    /** Something said unprompted: a reminder or a finished job. */
    fun onNotify(title: String, body: String, kind: String)
    /** Ask the user. Return true only on an explicit yes; the link declines on timeout. */
    suspend fun onConfirm(prompt: ConfirmPrompt): Boolean
}

interface ChatStore {
    /** Oldest first. */
    fun loadChat(limit: Int): List<ChatMessage>
    fun saveChat(message: ChatMessage)
    fun removeChat(id: String)
    fun clearChat()
}

/**
 * The chat as the phone keeps it: my messages, NORA's answers, and what NORA
 * sent on her own. Every change is written through to [store], so it survives
 * the app being killed.
 *
 * A message still waiting when the app starts, or when the link drops, will
 * never get its answer: the core answers down the socket the question came
 * up, and that socket is gone. Those are marked FAILED so they can be resent.
 */
class ChatLog(
    private val store: ChatStore,
    private val clock: () -> Long = System::currentTimeMillis,
    private val keep: Int = 300,
) {
    private val _messages = MutableStateFlow(store.loadChat(keep).map {
        if (it.sender == Sender.ME && it.delivery in IN_FLIGHT) it.copy(delivery = Delivery.FAILED).also(store::saveChat)
        else it
    })
    val messages: StateFlow<List<ChatMessage>> = _messages

    @Synchronized
    fun mine(id: String, text: String, delivery: Delivery) {
        add(ChatMessage(id, clock(), Sender.ME, text, delivery = delivery))
    }

    @Synchronized
    fun setDelivery(id: String, delivery: Delivery) {
        update(id) { it.copy(delivery = delivery) }
    }

    @Synchronized
    fun nora(replyTo: String?, text: String) {
        add(ChatMessage(Protocol.newId(clock()), clock(), Sender.NORA, text, ref = replyTo.orEmpty()))
        if (replyTo != null) update(replyTo) { it.copy(delivery = Delivery.ANSWERED) }
    }

    /** The turn is over. A turn that said nothing still stops showing as waiting. */
    @Synchronized
    fun done(replyTo: String?) {
        if (replyTo != null) update(replyTo) {
            if (it.delivery in IN_FLIGHT) it.copy(delivery = Delivery.ANSWERED) else it
        }
    }

    @Synchronized
    fun notice(body: String, kind: String) {
        add(ChatMessage(Protocol.newId(clock()), clock(), Sender.NOTICE, body, ref = kind))
    }

    /** The link dropped: nothing still waiting will be answered. */
    @Synchronized
    fun dropInFlight() {
        _messages.value.filter { it.sender == Sender.ME && it.delivery in IN_FLIGHT }
            .forEach { m -> update(m.id) { it.copy(delivery = Delivery.FAILED) } }
    }

    @Synchronized
    fun remove(id: String) {
        store.removeChat(id)
        _messages.value = _messages.value.filterNot { it.id == id }
    }

    /** NORA's lines answering [id], in order. */
    fun answersTo(id: String): List<String> =
        _messages.value.filter { it.sender == Sender.NORA && it.ref == id }.map { it.text }

    fun get(id: String): ChatMessage? = _messages.value.firstOrNull { it.id == id }

    @Synchronized
    fun clear() {
        store.clearChat()
        _messages.value = emptyList()
    }

    private fun add(m: ChatMessage) {
        store.saveChat(m)
        _messages.value = (_messages.value + m).takeLast(keep)
    }

    private fun update(id: String, change: (ChatMessage) -> ChatMessage) {
        _messages.value = _messages.value.map { m ->
            if (m.id != id) m else change(m).also { if (it != m) store.saveChat(it) }
        }
    }

    companion object {
        private val IN_FLIGHT = setOf(Delivery.SENDING, Delivery.SENT)
        /** What one typed message may be. The core truncates at 2000 as well. */
        const val MAX_TEXT = 2000
    }
}

/** The steps of a `confirm_request`, read strictly, and said in words for the prompt. */
object ConfirmSteps {
    private const val MAX_STEPS = 20

    /** `[{action, params}]`, or null if anything about it is off. */
    fun parse(arr: JSONArray?): List<Pair<String, JSONObject>>? {
        if (arr == null || arr.length() == 0 || arr.length() > MAX_STEPS) return null
        return (0 until arr.length()).map { i ->
            val step = arr.optJSONObject(i) ?: return null
            val action = step.opt("action") as? String ?: return null
            if (action.isBlank() || action.length > 100) return null
            val params = when (val p = step.opt("params")) {
                null, JSONObject.NULL -> JSONObject()
                is JSONObject -> p
                else -> return null
            }
            action to params
        }
    }

    /** "phone.set_alarm" {hour: 6} → "Set alarm on the phone — hour: 6". */
    fun describe(action: String, params: JSONObject): String {
        val onPhone = action.startsWith("phone.")
        val verb = action.removePrefix("phone.").replace('_', ' ').replace('.', ' ')
            .replaceFirstChar { it.uppercase() } + if (onPhone) " on the phone" else ""
        val args = params.keys().asSequence().sorted().joinToString(", ") { k ->
            "$k: ${params.opt(k).toString().take(120)}"
        }
        return if (args.isEmpty()) verb else "$verb — $args"
    }
}
