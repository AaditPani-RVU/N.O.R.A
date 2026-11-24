package com.aaditpani.nora.link

import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class PaymentsTest {
    private val gpay = "com.google.android.apps.nbu.paisa.user"
    private val sms = "com.google.android.apps.messaging"
    private val whatsapp = "com.whatsapp"

    @Test
    fun `payment apps need an amount and a verb`() {
        assertTrue(Payments.isPayment(gpay, "Paid to Swiggy", "You paid ₹250 to Swiggy"))
        assertFalse(Payments.isPayment(gpay, "Google Pay", "Get ₹50 cashback on your next recharge"))
    }

    @Test
    fun `bank SMS counts, a friend's message doesn't`() {
        assertTrue(Payments.isPayment(sms, "AX-HDFCBK",
            "Rs.1,200.00 debited from a/c XX1234 on 24-11-26 to VPA swiggy@icici. Avl Bal Rs 5,000"))
        assertTrue(Payments.isPayment(sms, "JM-SBIUPI", "INR 500 credited to your account via UPI"))
        assertFalse(Payments.isPayment(whatsapp, "Rahul", "I paid ₹500 for dinner, send me half"))
    }
}
