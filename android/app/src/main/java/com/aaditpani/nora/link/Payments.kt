package com.aaditpani.nora.link

/**
 * Which new notifications are payments, for "what did I spend this week"
 * (NORA_ASSIST_PLAN E). Only these are forwarded to the core, as they arrive,
 * as a `money.notification` event; every other notification stays on the phone
 * until asked for, as before. The core keeps the parsed amount and payee only,
 * never the text.
 *
 * A notification counts when it names an amount in rupees *and* says money
 * moved. From a payments or banking app that's enough; from anywhere else (the
 * SMS app, where bank alerts land) it also needs a bank's phrasing — "debited",
 * "credited", "a/c", "UPI" — so a friend's "I paid ₹500 for dinner" isn't one.
 */
object Payments {
    val PAYMENT_APPS = setOf(
        "com.google.android.apps.nbu.paisa.user",   // Google Pay
        "com.phonepe.app",
        "net.one97.paytm",
        "in.org.npci.upiapp",                       // BHIM
        "com.dreamplug.androidapp",                 // CRED
        "in.amazon.mShop.android.shopping",
        "com.sbi.lotusintouch", "com.sbi.SBIFreedomPlus",
        "com.csam.icici.bank.imobile",
        "com.snapwork.hdfc", "com.hdfcbank.payzapp",
        "com.axis.mobile",
        "com.msf.kbank.mobile",                     // Kotak
        "com.fss.indus", "com.bankofbaroda.mconnect",
        "com.freecharge.android", "com.mobikwik_new",
    )

    private val AMOUNT = Regex("""(?:₹|\bRs\.?|\bINR)\s?[0-9][0-9,]*(?:\.[0-9]{1,2})?""", RegexOption.IGNORE_CASE)
    private val MOVED = Regex("""\b(?:paid|debited|credited|sent|received|spent|withdrawn|transferred|payment|txn|transaction|refund(?:ed)?)\b""",
        RegexOption.IGNORE_CASE)
    private val BANKISH = Regex("""\b(?:debited|credited|a/c|acct|account|upi|vpa|neft|imps|avl\.?\s*bal|card\s+ending|xx\d{3,4})\b""",
        RegexOption.IGNORE_CASE)

    fun isPayment(packageName: String, title: String, text: String): Boolean {
        val all = "$title $text"
        if (!AMOUNT.containsMatchIn(all) || !MOVED.containsMatchIn(all)) return false
        return packageName in PAYMENT_APPS || BANKISH.containsMatchIn(all)
    }
}
