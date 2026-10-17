package com.aaditpani.nora.ui

import android.Manifest
import android.annotation.SuppressLint
import android.app.KeyguardManager
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.SystemBarStyle
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.systemBarsPadding
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.material3.darkColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.lifecycle.lifecycleScope
import com.aaditpani.nora.phone.Calls
import com.aaditpani.nora.phone.controller
import com.aaditpani.nora.voice.VoicePhase
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.drop
import kotlinx.coroutines.launch
import kotlinx.coroutines.suspendCancellableCoroutine
import kotlin.coroutines.resume

/**
 * The screen of a call from NORA (`phone.call`): ringing, then the
 * conversation. It shows over the lock screen and turns the screen on, like
 * any incoming call. Answering speaks why she called; she then listens only
 * once the phone is unlocked (see [com.aaditpani.nora.voice.VoiceController.startCall]).
 */
class CallActivity : ComponentActivity() {
    /** Answered on this screen: from here on it shows the conversation. */
    private val answered = MutableStateFlow(false)

    // Lint assumes a FragmentActivity; this is a plain ComponentActivity with no fragments.
    @SuppressLint("InvalidFragmentVersionForActivityResult")
    private val micPermission = registerForActivityResult(ActivityResultContracts.RequestPermission()) { granted ->
        if (granted) answer() else Calls.decline(this)
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setShowWhenLocked(true)
        setTurnScreenOn(true)
        enableEdgeToEdge(SystemBarStyle.dark(android.graphics.Color.TRANSPARENT),
            SystemBarStyle.dark(android.graphics.Color.TRANSPARENT))
        val voice = controller.voice
        // Done when the ringing stops unanswered, or the conversation ends.
        lifecycleScope.launch {
            Calls.ringing.collect { if (it == null && !answered.value) finish() }
        }
        lifecycleScope.launch {
            voice.ui.drop(1).collect { if (answered.value && it.phase == VoicePhase.OFF) finish() }
        }
        if (savedInstanceState == null) handle(intent)
        setContent {
            MaterialTheme(colorScheme = darkColorScheme(background = Hud.Bg, surface = Hud.Bg)) {
                val ringing by Calls.ringing.collectAsState()
                val inCall by answered.collectAsState()
                val ui by voice.ui.collectAsState()
                CallScreen(
                    reason = ringing?.reason,
                    inCall = inCall,
                    phase = ui.phase,
                    heard = ui.partial,
                    onAnswer = ::pickUp,
                    onDecline = { Calls.decline(this) },
                    onHangUp = { voice.stop(); finish() },
                )
            }
        }
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        handle(intent)
    }

    private fun handle(intent: Intent?) {
        if (intent?.getBooleanExtra(EXTRA_ANSWER, false) == true) pickUp()
    }

    private fun pickUp() {
        if (answered.value) return
        if (checkSelfPermission(Manifest.permission.RECORD_AUDIO) == PackageManager.PERMISSION_GRANTED) answer()
        else micPermission.launch(Manifest.permission.RECORD_AUDIO)
    }

    private fun answer() {
        // Before the ringing stops: its watcher may run inline and close this screen.
        answered.value = true
        val call = Calls.answer(this) ?: run { finish(); return }
        controller.voice.startCall(call.reason, ::unlock)
    }

    /** True once the phone is unlocked; asks for it when it isn't. */
    private suspend fun unlock(): Boolean {
        val km = getSystemService(KeyguardManager::class.java)
        if (!km.isKeyguardLocked) return true
        return suspendCancellableCoroutine { done ->
            km.requestDismissKeyguard(this, object : KeyguardManager.KeyguardDismissCallback() {
                override fun onDismissSucceeded() { if (done.isActive) done.resume(true) }
                override fun onDismissCancelled() { if (done.isActive) done.resume(false) }
                override fun onDismissError() { if (done.isActive) done.resume(false) }
            })
        }
    }

    companion object {
        private const val EXTRA_ANSWER = "answer"

        fun intent(context: Context, answer: Boolean): Intent = Intent(context, CallActivity::class.java)
            .putExtra(EXTRA_ANSWER, answer)
            .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_SINGLE_TOP)
    }
}

@Composable
private fun CallScreen(
    reason: String?,
    inCall: Boolean,
    phase: VoicePhase,
    heard: String,
    onAnswer: () -> Unit,
    onDecline: () -> Unit,
    onHangUp: () -> Unit,
) {
    val orb = when {
        !inCall -> OrbState.IDLE
        phase == VoicePhase.LISTENING -> OrbState.LISTENING
        phase == VoicePhase.THINKING -> OrbState.THINKING
        else -> OrbState.SPEAKING
    }
    HudBackground(orb.color) {
        Column(Modifier.fillMaxSize().systemBarsPadding().padding(horizontal = 24.dp, vertical = 32.dp),
            horizontalAlignment = Alignment.CenterHorizontally) {
            Tracked(if (inCall) "ON A CALL WITH" else "INCOMING CALL", color = Hud.Dim, size = 11.sp, spacing = 4.sp)
            Spacer(Modifier.height(10.dp))
            Text("NORA", fontFamily = Hud.Disp, fontWeight = FontWeight.Bold, fontSize = 44.sp,
                letterSpacing = 10.sp, color = orb.color)
            Spacer(Modifier.weight(1f))
            Orb(orb, 220.dp)
            Spacer(Modifier.height(24.dp))
            Text(when {
                !inCall -> reason.orEmpty()
                heard.isNotBlank() -> heard
                phase == VoicePhase.LISTENING -> "Go ahead…"
                else -> ""
            }, color = if (inCall && heard.isBlank()) Hud.Faint else Hud.Txt, fontFamily = Hud.Mono,
                fontSize = 15.sp, lineHeight = 21.sp, textAlign = TextAlign.Center, maxLines = 4,
                modifier = Modifier.fillMaxWidth())
            Spacer(Modifier.weight(1f))
            if (inCall) {
                HudButton("HANG UP", onHangUp, Modifier.fillMaxWidth(), color = Hud.Red, strong = true, textSize = 13.sp)
            } else {
                Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(16.dp)) {
                    HudButton("DECLINE", onDecline, Modifier.weight(1f), color = Hud.Red, strong = true, textSize = 13.sp)
                    HudButton("ANSWER", onAnswer, Modifier.weight(1f), color = Hud.Green, strong = true, textSize = 13.sp)
                }
            }
        }
    }
}
