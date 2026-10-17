package com.aaditpani.nora.link

/**
 * What happens to a call from a number that isn't in the phone's contacts
 * (NORA_CALLS_PLAN §3.1). Android asks the call-screening app before the call
 * rings; contacts never reach it, so nothing here can silence someone the
 * user knows.
 *
 *  - A number the user blocked is rejected, whatever the mode.
 *  - [Mode.RING] lets everything else ring as usual; the core only hears of it.
 *  - [Mode.SILENCE] lets it through without ringing: it shows as a missed call,
 *    and NORA leaves a notification saying who it was.
 */
object Screening {
    enum class Mode { RING, SILENCE }

    enum class Action(val wire: String) { RANG("rang"), SILENCED("silenced"), REJECTED("rejected") }

    fun decide(number: String?, mode: Mode, blocked: Set<String>): Action = when {
        number != null && key(number).let { it.isNotEmpty() && it in blocked } -> Action.REJECTED
        mode == Mode.SILENCE -> Action.SILENCED
        else -> Action.RANG
    }

    /**
     * The same number however it's written: "+91 98450 12345", "098450-12345"
     * and "9845012345" are one number. The last ten digits, which is a whole
     * Indian mobile number and enough to tell numbers apart elsewhere.
     * Empty for something with no digits.
     */
    fun key(number: String): String = number.filter { it.isDigit() }.takeLast(10)

    fun modeOf(s: String?): Mode = if (s == "silence") Mode.SILENCE else Mode.RING
    fun wire(mode: Mode): String = if (mode == Mode.SILENCE) "silence" else "ring"
}
