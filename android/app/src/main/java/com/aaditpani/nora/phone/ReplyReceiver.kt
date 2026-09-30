package com.aaditpani.nora.phone

import android.app.RemoteInput
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent

/** A message typed into a NORA notification's Reply box. Not exported. */
class ReplyReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        val text = RemoteInput.getResultsFromIntent(intent)
            ?.getCharSequence(Notifications.KEY_REPLY)?.toString()?.trim()
        if (text.isNullOrEmpty()) return
        val sent = context.controller.send(text)
        Notifications.replySent(context, text, sent)
    }
}
