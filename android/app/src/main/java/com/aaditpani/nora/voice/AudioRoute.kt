package com.aaditpani.nora.voice

import android.content.Context
import android.media.AudioDeviceInfo
import android.media.AudioManager
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.withTimeoutOrNull

/**
 * Where a voice session hears and speaks.
 *
 * Output needs nothing: NORA's speech is played as the assistant, which the
 * system sends to a connected headset by itself. Input does: the recogniser
 * takes the phone's own microphone unless a headset's is made the
 * communication device. So while listening, a Bluetooth headset's microphone
 * (classic SCO or LE Audio) is selected, and released again before NORA
 * answers, so the answer plays over the headset's music-quality link rather
 * than the narrow call channel.
 */
class AudioRoute(context: Context) {
    private val am = context.getSystemService(AudioManager::class.java)
    private var holding = false
    private var previousMode = AudioManager.MODE_NORMAL

    /** "bluetooth", "wired", or "phone": what the user is talking into and hearing. */
    fun describe(): String {
        val outs = am.getDevices(AudioManager.GET_DEVICES_OUTPUTS).map { it.type }.toSet()
        return when {
            outs.any { it in BT_OUT } -> "bluetooth"
            outs.any { it in WIRED } -> "wired"
            else -> "phone"
        }
    }

    /** True when NORA's voice comes out of the phone's own speaker (echo into the mic). */
    fun onSpeaker(): Boolean = describe() == "phone"

    /**
     * Use a Bluetooth headset's microphone for listening, if there is one.
     * Waits up to [waitMs] for the link to switch (SCO takes a moment).
     */
    suspend fun holdHeadsetMic(waitMs: Long = 1500): Boolean {
        val mic = am.availableCommunicationDevices.firstOrNull { it.type in BT_MIC } ?: return false
        if (am.communicationDevice?.id == mic.id) {
            holding = true
            return true
        }
        val switched = CompletableDeferred<Unit>()
        val listener = AudioManager.OnCommunicationDeviceChangedListener { d ->
            if (d?.id == mic.id) switched.complete(Unit)
        }
        am.addOnCommunicationDeviceChangedListener(Runnable::run, listener)
        return try {
            previousMode = am.mode
            am.mode = AudioManager.MODE_IN_COMMUNICATION
            if (!am.setCommunicationDevice(mic)) {
                am.mode = previousMode
                return false
            }
            holding = true
            withTimeoutOrNull(waitMs) { switched.await() }
            true
        } finally {
            am.removeOnCommunicationDeviceChangedListener(listener)
        }
    }

    fun release() {
        if (!holding) return
        holding = false
        am.clearCommunicationDevice()
        am.mode = previousMode
    }

    companion object {
        private val BT_MIC = setOf(AudioDeviceInfo.TYPE_BLUETOOTH_SCO, AudioDeviceInfo.TYPE_BLE_HEADSET)
        private val BT_OUT = setOf(AudioDeviceInfo.TYPE_BLUETOOTH_A2DP, AudioDeviceInfo.TYPE_BLUETOOTH_SCO,
            AudioDeviceInfo.TYPE_BLE_HEADSET, AudioDeviceInfo.TYPE_BLE_SPEAKER)
        private val WIRED = setOf(AudioDeviceInfo.TYPE_WIRED_HEADSET, AudioDeviceInfo.TYPE_WIRED_HEADPHONES,
            AudioDeviceInfo.TYPE_USB_HEADSET)
    }
}
