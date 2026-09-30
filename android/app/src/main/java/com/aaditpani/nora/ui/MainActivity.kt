package com.aaditpani.nora.ui

import android.Manifest
import android.annotation.SuppressLint
import android.content.Intent
import android.content.pm.PackageManager
import android.hardware.biometrics.BiometricManager
import android.hardware.biometrics.BiometricPrompt
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.os.CancellationSignal
import android.os.PowerManager
import android.provider.Settings
import androidx.activity.ComponentActivity
import androidx.activity.SystemBarStyle
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.animation.AnimatedContent
import androidx.compose.animation.AnimatedVisibility
import androidx.compose.animation.expandVertically
import androidx.compose.animation.shrinkVertically
import androidx.compose.animation.animateColorAsState
import androidx.compose.animation.core.LinearEasing
import androidx.compose.animation.core.animateFloat
import androidx.compose.animation.core.infiniteRepeatable
import androidx.compose.animation.core.rememberInfiniteTransition
import androidx.compose.animation.core.tween
import androidx.compose.animation.fadeIn
import androidx.compose.animation.fadeOut
import androidx.compose.animation.togetherWith
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.BoxWithConstraints
import androidx.compose.foundation.layout.ExperimentalLayoutApi
import androidx.compose.foundation.layout.WindowInsets
import androidx.compose.foundation.layout.isImeVisible
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.imePadding
import androidx.compose.foundation.layout.navigationBarsPadding
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.statusBarsPadding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.layout.widthIn
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.LazyRow
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.lazy.rememberLazyListState
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.selection.SelectionContainer
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.material3.darkColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableLongStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.drawBehind
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.geometry.Size
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.compose.ui.window.Dialog
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.LifecycleEventObserver
import androidx.lifecycle.compose.LocalLifecycleOwner
import com.aaditpani.nora.link.AuditEntry
import com.aaditpani.nora.link.ChatMessage
import com.aaditpani.nora.link.ConfirmPrompt
import com.aaditpani.nora.link.ConfirmSteps
import com.aaditpani.nora.link.Delivery
import com.aaditpani.nora.link.LinkState
import com.aaditpani.nora.link.PairResult
import com.aaditpani.nora.link.PairingInvite
import com.aaditpani.nora.link.Sender
import com.aaditpani.nora.phone.LinkController
import com.aaditpani.nora.phone.LinkService
import com.aaditpani.nora.phone.NoraNotificationListener
import com.aaditpani.nora.phone.Notifications
import com.aaditpani.nora.phone.controller
import com.google.mlkit.vision.barcode.common.Barcode
import com.google.mlkit.vision.codescanner.GmsBarcodeScannerOptions
import com.google.mlkit.vision.codescanner.GmsBarcodeScanning
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.launch
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

class MainActivity : ComponentActivity() {
    /** A tab a notification asked for; the paired screen consumes it. */
    private val requestedTab = MutableStateFlow<Int?>(null)

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        enableEdgeToEdge(SystemBarStyle.dark(android.graphics.Color.TRANSPARENT),
            SystemBarStyle.dark(android.graphics.Color.TRANSPARENT))
        val c = controller
        if (c.paired.value) LinkService.start(this)
        requestTab(intent)
        setContent {
            MaterialTheme(colorScheme = hudScheme) {
                val paired by c.paired.collectAsState()
                if (paired) PairedScreen(c, requestedTab, ::authenticate)
                else PairScreen(c, onScan = ::scan)
            }
        }
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        requestTab(intent)
    }

    private fun requestTab(intent: Intent?) {
        intent?.getIntExtra(EXTRA_TAB, -1)?.takeIf { it >= 0 }?.let { requestedTab.value = it }
    }

    /** Fingerprint or screen lock, for confirmations of tier 3 and up (plan §6). */
    private fun authenticate(onOk: () -> Unit) {
        BiometricPrompt.Builder(this)
            .setTitle("Confirm it's you")
            .setSubtitle("NORA asks for this one to be unlocked first")
            .setAllowedAuthenticators(BiometricManager.Authenticators.BIOMETRIC_STRONG or
                BiometricManager.Authenticators.DEVICE_CREDENTIAL)
            .build()
            .authenticate(CancellationSignal(), mainExecutor, object : BiometricPrompt.AuthenticationCallback() {
                override fun onAuthenticationSucceeded(result: BiometricPrompt.AuthenticationResult?) = onOk()
            })
    }

    private fun scan(onResult: (String?) -> Unit) {
        val options = GmsBarcodeScannerOptions.Builder().setBarcodeFormats(Barcode.FORMAT_QR_CODE).build()
        GmsBarcodeScanning.getClient(this, options).startScan()
            .addOnSuccessListener { onResult(it.rawValue) }
            .addOnFailureListener { onResult(null) }
            .addOnCanceledListener { onResult(null) }
    }

    companion object {
        const val EXTRA_TAB = "tab"
        const val TAB_CHAT = 0
    }
}

/** For the few Material pieces left (selection handles, dialogs' scrim). */
private val hudScheme = darkColorScheme(
    primary = Hud.Cyan, onPrimary = Hud.Bg, background = Hud.Bg, surface = Hud.Bg,
    onSurface = Hud.Txt, onBackground = Hud.Txt, error = Hud.Red,
)

private val clock = SimpleDateFormat("HH:mm", Locale.US)
private val stamp = SimpleDateFormat("HH:mm:ss", Locale.US)
private val dayStamp = SimpleDateFormat("dd MMM HH:mm:ss", Locale.US)

/** Ticks once a second (or [everyMs]) for clocks and countdowns. */
@Composable
private fun rememberNow(everyMs: Long = 1000): Long {
    var now by remember { mutableLongStateOf(System.currentTimeMillis()) }
    LaunchedEffect(everyMs) { while (true) { delay(everyMs); now = System.currentTimeMillis() } }
    return now
}

// ── frame: header and tabs ───────────────────────────────────────────────────

@Composable
private fun Header(state: LinkState, killed: Boolean, accent: Color) {
    val now = rememberNow()
    Column {
        Row(Modifier.fillMaxWidth().padding(horizontal = 16.dp, vertical = 10.dp),
            verticalAlignment = Alignment.CenterVertically) {
            Sigil(accent)
            Spacer(Modifier.width(10.dp))
            Logotype(19.sp, 7.sp, accent)
            Spacer(Modifier.width(8.dp))
            Box(Modifier.border(1.dp, accent.copy(alpha = 0.15f)).padding(horizontal = 5.dp, vertical = 2.dp)) {
                Row {
                    Tracked("MK III", size = 8.sp, spacing = 1.5.sp)
                    Tracked(" · MOB", color = accent, size = 8.sp, spacing = 1.5.sp)
                }
            }
            Spacer(Modifier.weight(1f))
            Tracked(clock.format(Date(now)), color = accent, size = 13.sp, spacing = 3.sp, glow = true)
            Spacer(Modifier.width(10.dp))
            when {
                killed -> Pill("LOCKED", Hud.Amber)
                state is LinkState.Connected -> Pill("ONLINE", Hud.Green)
                state is LinkState.Connecting -> Pill("LINKING", Hud.Amber)
                else -> Pill("OFFLINE", Hud.Red)
            }
        }
        HeaderSweep(accent)
    }
}

/** The light that slides back and forth along the header's lower edge. */
@Composable
private fun HeaderSweep(accent: Color) {
    val t = rememberInfiniteTransition(label = "sweep")
    val x by t.animateFloat(-0.3f, 1.3f, infiniteRepeatable(tween(4000, easing = Hud.Ease),
        androidx.compose.animation.core.RepeatMode.Reverse), label = "x")
    Box(Modifier.fillMaxWidth().height(1.dp).background(accent.copy(alpha = 0.12f)).drawBehind {
        val w = size.width * 0.55f
        val cx = x * size.width
        drawRect(Brush.horizontalGradient(listOf(Color.Transparent, accent.copy(alpha = 0.85f), Color.Transparent),
            cx - w / 2, cx + w / 2), Offset(cx - w / 2, 0f), Size(w, size.height))
    })
}

@Composable
private fun Tabs(tab: Int, onTab: (Int) -> Unit, accent: Color, waiting: Int) {
    Row(Modifier.fillMaxWidth().padding(horizontal = 16.dp, vertical = 8.dp),
        horizontalArrangement = Arrangement.spacedBy(6.dp)) {
        listOf("CHAT", "SYSTEM", "LOG").forEachIndexed { i, label ->
            val on = tab == i
            Box(Modifier.weight(1f)
                .background(accent.copy(alpha = if (on) 0.10f else 0.02f), RoundedCornerShape(2.dp))
                .border(1.dp, accent.copy(alpha = if (on) 0.55f else 0.08f), RoundedCornerShape(2.dp))
                .clickable { onTab(i) }
                .padding(vertical = 8.dp), contentAlignment = Alignment.Center) {
                Row(verticalAlignment = Alignment.CenterVertically) {
                    Tracked(label, color = if (on) accent else Hud.Dim, size = 10.sp, spacing = 3.sp, glow = on)
                    if (i == 0 && waiting > 0) { Spacer(Modifier.width(6.dp)); Dot(Hud.Amber) }
                }
            }
        }
    }
}

// ── paired ───────────────────────────────────────────────────────────────────

@OptIn(ExperimentalLayoutApi::class)
@Composable
private fun PairedScreen(c: LinkController, requestedTab: MutableStateFlow<Int?>,
                         authenticate: (() -> Unit) -> Unit) {
    var tab by rememberSaveable { mutableIntStateOf(MainActivity.TAB_CHAT) }
    val requested by requestedTab.collectAsState()
    LaunchedEffect(requested) { requested?.let { tab = it; requestedTab.value = null } }

    val state by c.state.collectAsState()
    val killed by c.killed.collectAsState()
    val messages by c.chat.messages.collectAsState()
    val confirms by c.confirms.collectAsState()
    val now = rememberNow(250)
    val orb = orbState(state, messages, confirms, now)
    val accent by animateColorAsState(if (confirms.isNotEmpty()) Hud.Amber
        else if (orb == OrbState.OFFLINE) Hud.Cyan.copy(alpha = 0.7f) else orb.color, tween(900), label = "accent")

    HudBackground(accent) {
        Column(Modifier.fillMaxSize().statusBarsPadding().navigationBarsPadding().imePadding()) {
            // Typing in the chat: the keyboard takes half the screen, so the
            // header and tabs fold away and the chat's own strip stands in.
            val typing = WindowInsets.isImeVisible && tab == 0
            AnimatedVisibility(!typing, enter = expandVertically(tween(250)) + fadeIn(tween(250)),
                exit = shrinkVertically(tween(200)) + fadeOut(tween(150))) {
                Column {
                    Header(state, killed, accent)
                    Tabs(tab, { tab = it }, accent, confirms.size)
                }
            }
            Box(Modifier.weight(1f).padding(horizontal = 16.dp)) {
                when (tab) {
                    0 -> ChatTab(c, orb, state is LinkState.Connected, messages, confirms, authenticate, typing)
                    1 -> SystemTab(c, state, killed)
                    else -> LogTab(c)
                }
            }
        }
    }
}

/** What NORA is doing, read from the link and the chat: the dashboard's orb states. */
private fun orbState(state: LinkState, messages: List<ChatMessage>, confirms: List<ConfirmPrompt>, now: Long): OrbState {
    if (state !is LinkState.Connected) return OrbState.OFFLINE
    val last = messages.lastOrNull()
    if (last != null && last.sender != Sender.ME && now - last.ts < 1800) return OrbState.SPEAKING
    if (confirms.isNotEmpty()) return OrbState.THINKING
    if (messages.any { it.sender == Sender.ME && it.delivery in setOf(Delivery.SENT, Delivery.SENDING) })
        return OrbState.THINKING
    return OrbState.IDLE
}

// ── chat ─────────────────────────────────────────────────────────────────────

private val suggestions = listOf(
    "What do I need to do today?", "What's on my notifications?", "What's my phone battery?",
    "What's playing on my phone?", "Pause the music",
)

@Composable
private fun ChatTab(c: LinkController, orb: OrbState, connected: Boolean, messages: List<ChatMessage>,
                    confirms: List<ConfirmPrompt>, authenticate: (() -> Unit) -> Unit, typing: Boolean) {
    val ctx = LocalContext.current
    var draft by rememberSaveable { mutableStateOf("") }
    val list = rememberLazyListState()
    // Only what arrives while the screen is up slides in; history just sits there.
    val openedAt = remember { System.currentTimeMillis() }

    // On screen means answers land here, not in a notification.
    val lifecycle = LocalLifecycleOwner.current.lifecycle
    DisposableEffect(lifecycle) {
        val observer = LifecycleEventObserver { _, event ->
            when (event) {
                Lifecycle.Event.ON_RESUME -> { c.chatVisible.value = true; Notifications.clearReply(ctx) }
                Lifecycle.Event.ON_PAUSE -> c.chatVisible.value = false
                else -> Unit
            }
        }
        if (lifecycle.currentState.isAtLeast(Lifecycle.State.RESUMED)) {
            c.chatVisible.value = true
            Notifications.clearReply(ctx)
        }
        lifecycle.addObserver(observer)
        onDispose {
            lifecycle.removeObserver(observer)
            c.chatVisible.value = false
        }
    }
    val thinking = orb == OrbState.THINKING && confirms.isEmpty()
    val rows = messages.size + confirms.size + (if (thinking) 1 else 0)
    // Also when the keyboard opens, so the latest line stays in view above it.
    LaunchedEffect(rows, typing) { if (rows > 0) list.animateScrollToItem(rows + 1) }

    fun send(text: String) { if (c.send(text)) draft = "" }

    Column(Modifier.fillMaxSize()) {
        val empty = messages.isEmpty() && confirms.isEmpty()
        AnimatedContent(if (typing) 2 else if (empty) 0 else 1, Modifier.fillMaxWidth(), label = "stage",
            transitionSpec = { fadeIn(tween(400)) togetherWith fadeOut(tween(200)) }) { stage ->
            when (stage) {
                0 -> Hero(orb)
                1 -> CompactStage(orb, 64.dp)
                else -> CompactStage(orb, 44.dp, Modifier.padding(top = 4.dp))
            }
        }

        // The transcript frame: glass, brackets, and a reticle when empty.
        Box(Modifier.weight(1f).fillMaxWidth().padding(top = 8.dp).glass(orb.color).brackets(orb.color)) {
            Column(Modifier.fillMaxSize()) {
                Row(Modifier.padding(horizontal = 12.dp, vertical = 9.dp), verticalAlignment = Alignment.CenterVertically) {
                    Diamond(9.dp, orb.color)
                    Spacer(Modifier.width(8.dp))
                    Tracked("TRANSCRIPT", Modifier.weight(1f), color = orb.color, size = 10.sp, spacing = 4.sp,
                        glow = true, font = Hud.Disp, weight = FontWeight.SemiBold)
                    Tracked("${messages.count { it.sender == Sender.ME }} TURNS", size = 9.sp)
                    if (messages.isNotEmpty()) {
                        Spacer(Modifier.width(8.dp))
                        Box(Modifier.border(1.dp, Hud.Cyan.copy(alpha = 0.15f)).clickable { c.chat.clear() }
                            .padding(horizontal = 6.dp, vertical = 2.dp)) { Tracked("CLEAR", size = 9.sp) }
                    }
                }
                Box(Modifier.fillMaxWidth().height(1.dp).background(orb.color.copy(alpha = 0.12f)))
                if (empty) {
                    AwaitingInput(orb.color, connected)
                } else {
                    LazyColumn(Modifier.fillMaxSize().padding(horizontal = 10.dp), state = list,
                        verticalArrangement = Arrangement.spacedBy(12.dp)) {
                        item { Spacer(Modifier.height(4.dp)) }
                        items(messages, key = { it.id }) { m ->
                            Enter(animate = m.ts > openedAt) {
                                Turn(m, latencyOf(m, messages), onRetry = { c.resend(m.id) })
                            }
                        }
                        items(confirms, key = { it.requestId }) { p ->
                            Enter(animate = true) {
                                Authorize(p,
                                    onYes = { if (p.tier >= 3) authenticate { c.answer(p.requestId, true) } else c.answer(p.requestId, true) },
                                    onNo = { c.answer(p.requestId, false) })
                            }
                        }
                        if (thinking) item(key = "typing") { Enter(animate = true) { Typing() } }
                        item { Spacer(Modifier.height(4.dp)) }
                    }
                }
            }
            if (orb != OrbState.IDLE && orb != OrbState.OFFLINE) FlowLine(orb.color, Modifier.align(Alignment.BottomCenter))
        }

        // Chips are a shortcut for not typing; once you are, they give the room back.
        AnimatedVisibility(!(typing && draft.isNotEmpty()), enter = expandVertically() + fadeIn(),
            exit = shrinkVertically() + fadeOut()) {
            LazyRow(Modifier.fillMaxWidth().padding(top = 10.dp), horizontalArrangement = Arrangement.spacedBy(6.dp)) {
                items(suggestions) { s -> Chip(s, { send(s) }, enabled = connected) }
            }
        }
        Row(Modifier.fillMaxWidth().padding(vertical = 10.dp), verticalAlignment = Alignment.CenterVertically) {
            HudField(draft, { draft = it.take(2000) }, if (connected) "Type a command" else "Link down",
                Modifier.weight(1f), onSend = { if (connected && draft.isNotBlank()) send(draft) })
            Spacer(Modifier.width(8.dp))
            HudButton("↵", { send(draft) }, enabled = connected && draft.isNotBlank(), strong = true, textSize = 20.sp)
        }
    }
}

/** The empty chat: the whole orb, the name, the state, the pipeline. */
@Composable
private fun Hero(orb: OrbState) {
    Column(Modifier.fillMaxWidth().padding(top = 4.dp), horizontalAlignment = Alignment.CenterHorizontally) {
        Orb(orb, 196.dp)
        Text("N.O.R.A", fontFamily = Hud.Disp, fontWeight = FontWeight.Bold, fontSize = 28.sp,
            letterSpacing = 12.sp, color = orb.color, style = TextStyle(shadow = Hud.glow(orb.color, 28f)))
        Tracked(orb.label, color = if (orb == OrbState.IDLE) Hud.Dim else orb.color, size = 10.sp,
            spacing = 6.sp, glow = orb != OrbState.IDLE)
        Spacer(Modifier.height(8.dp))
        StageBar(orb)
    }
}

/** Once there's a conversation: the orb shrinks into a strip above it. */
@Composable
private fun CompactStage(orb: OrbState, orbSize: androidx.compose.ui.unit.Dp, modifier: Modifier = Modifier) {
    Row(modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
        Orb(orb, orbSize)
        Spacer(Modifier.width(6.dp))
        Column(Modifier.weight(1f)) {
            Tracked("N.O.R.A", color = orb.color, size = 16.sp, spacing = 6.sp, glow = true,
                font = Hud.Disp, weight = FontWeight.Bold)
            Tracked(orb.label, color = if (orb == OrbState.IDLE) Hud.Dim else orb.color, size = 9.sp, spacing = 4.sp)
        }
        StageBar(orb)
    }
}

/**
 * The dashboard's LISTEN · STT · GUARD · LLM · ACT · SPEAK, for a typed turn:
 * what the phone can honestly see is its message going up, the core working,
 * and the answer coming back.
 */
@Composable
private fun StageBar(orb: OrbState) {
    val active = when (orb) {
        OrbState.THINKING -> 1
        OrbState.SPEAKING -> 2
        else -> -1
    }
    Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(4.dp)) {
        listOf("UPLINK", "CORE", "REPLY").forEachIndexed { i, s ->
            if (i > 0) Tracked("·", color = Hud.Cyan.copy(alpha = 0.25f), size = 9.sp)
            val on = i == active || (i == 0 && active > 0)
            val col = when (i) { 1 -> Hud.Violet; 2 -> Hud.Teal; else -> Hud.Cyan }
            Box(Modifier
                .background(if (on) col.copy(alpha = 0.10f) else Hud.Cyan.copy(alpha = 0.02f))
                .border(1.dp, if (on) col.copy(alpha = 0.55f) else Hud.Cyan.copy(alpha = 0.08f))
                .padding(horizontal = 6.dp, vertical = 3.dp)) {
                Tracked(s, color = if (on) col else Hud.Dim, size = 8.sp, spacing = 1.5.sp, glow = on)
            }
        }
    }
}

@Composable
private fun AwaitingInput(accent: Color, connected: Boolean) {
    BoxWithConstraints(Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
        // With the keyboard up there may be no room for the reticle: say it in one line.
        if (maxHeight < 150.dp) {
            Tracked(if (connected) "[ AWAITING INPUT ]" else "[ LINK DOWN ]", color = Hud.Light.copy(alpha = 0.6f),
                size = 10.sp, spacing = 4.sp)
            return@BoxWithConstraints
        }
        Column(Modifier
            .background(Brush.radialGradient(listOf(accent.copy(alpha = 0.06f), Color.Transparent)))
            .brackets(accent.copy(alpha = 0.42f), arm = 18.dp, all = true, width = 1.dp)
            .padding(horizontal = 36.dp, vertical = 26.dp),
            horizontalAlignment = Alignment.CenterHorizontally, verticalArrangement = Arrangement.spacedBy(12.dp)) {
            Equalizer(accent)
            Tracked(if (connected) "AWAITING INPUT" else "LINK DOWN", color = Hud.Light.copy(alpha = 0.72f),
                size = 11.sp, spacing = 5.sp)
            Tracked(if (connected) "TYPE BELOW · OR TAP A CHIP" else "WAITING FOR THE CORE",
                color = Hud.Faint, size = 8.sp, spacing = 2.4.sp)
        }
    }
}

/** How long NORA took to start answering, shown on her first line of a turn. */
private fun latencyOf(m: ChatMessage, all: List<ChatMessage>): String? {
    if (m.sender != Sender.NORA || m.ref.isEmpty()) return null
    val asked = all.firstOrNull { it.id == m.ref } ?: return null
    if (all.first { it.sender == Sender.NORA && it.ref == m.ref }.id != m.id) return null
    val ms = m.ts - asked.ts
    return if (ms < 0 || ms > 120_000) null else String.format(Locale.US, "%.1fs", ms / 1000.0)
}

/** One line of the transcript, drawn as the dashboard draws a turn. */
@Composable
private fun Turn(m: ChatMessage, latency: String?, onRetry: () -> Unit) {
    val mine = m.sender == Sender.ME
    val failed = mine && m.delivery == Delivery.FAILED
    val accent = when {
        m.sender == Sender.NOTICE && m.ref == "reminder" -> Hud.Amber
        failed -> Hud.Red
        mine -> Hud.Light
        else -> Hud.Cyan
    }
    val who = when (m.sender) {
        Sender.ME -> "YOU"
        Sender.NORA -> "NORA"
        Sender.NOTICE -> if (m.ref == "reminder") "REMINDER" else "NORA · UNPROMPTED"
    }
    Row(Modifier.fillMaxWidth().then(if (failed) Modifier.clickable(onClick = onRetry) else Modifier),
        horizontalArrangement = if (mine) Arrangement.End else Arrangement.Start) {
        if (!mine) { Box(Modifier.padding(top = 18.dp)) { Diamond(12.dp, accent, filled = true) }; Spacer(Modifier.width(9.dp)) }
        Column(Modifier.widthIn(max = 300.dp), horizontalAlignment = if (mine) Alignment.End else Alignment.Start) {
            Row(Modifier.padding(bottom = 4.dp), verticalAlignment = Alignment.CenterVertically,
                horizontalArrangement = Arrangement.spacedBy(7.dp)) {
                if (mine && failed) Tracked("NOT SENT · TAP TO RETRY", color = Hud.Red, size = 8.sp)
                Tracked(who, color = if (m.sender == Sender.NOTICE) accent else Hud.Dim, size = 8.sp, spacing = 2.2.sp)
                Tracked(stamp.format(Date(m.ts)), color = Hud.Faint, size = 8.sp, spacing = 1.sp)
                if (latency != null) {
                    Box(Modifier.border(1.dp, Hud.Cyan.copy(alpha = 0.15f), RoundedCornerShape(50))
                        .padding(horizontal = 5.dp)) { Tracked(latency, color = Hud.Cyan, size = 8.sp, spacing = 1.sp, upper = false) }
                }
            }
            val shape = RoundedCornerShape(3.dp)
            Box(Modifier
                .background(if (mine) Brush.verticalGradient(listOf(Color(0x08FFFFFF), Color(0x05FFFFFF)))
                    else Brush.verticalGradient(listOf(accent.copy(alpha = 0.10f), accent.copy(alpha = 0.028f))), shape)
                .border(1.dp, accent.copy(alpha = if (mine && !failed) 0.13f else 0.20f), shape)
                .drawBehind {
                    val w = 2.dp.toPx()
                    drawRect(if (mine) accent.copy(alpha = 0.45f) else accent,
                        Offset(if (mine) size.width - w else 0f, 0f), Size(w, size.height))
                }
                .padding(horizontal = 12.dp, vertical = 9.dp)) {
                SelectionContainer {
                    Text(m.text, color = if (mine) Hud.Light.copy(alpha = 0.82f) else Hud.Txt, fontFamily = Hud.Mono,
                        fontSize = 14.sp, lineHeight = 22.sp, letterSpacing = 0.3.sp,
                        style = TextStyle(shadow = if (mine) null else Hud.glow(accent, 12f)))
                }
            }
        }
        if (mine) { Spacer(Modifier.width(9.dp)); Box(Modifier.padding(top = 18.dp).size(12.dp)
            .background(accent.copy(alpha = 0.10f), CircleShape).border(1.dp, accent.copy(alpha = 0.4f), CircleShape)) }
    }
}

/** NORA working on it: her avatar and the block cursor. */
@Composable
private fun Typing() {
    val t = rememberInfiniteTransition(label = "typing")
    val b by t.animateFloat(0f, 1f, infiniteRepeatable(tween(800, easing = LinearEasing)), label = "b")
    Row(verticalAlignment = Alignment.CenterVertically) {
        Diamond(12.dp, Hud.Violet, filled = true)
        Spacer(Modifier.width(9.dp))
        Box(Modifier.background(Hud.Violet.copy(alpha = 0.08f), RoundedCornerShape(3.dp))
            .border(1.dp, Hud.Violet.copy(alpha = 0.2f), RoundedCornerShape(3.dp))
            .padding(horizontal = 12.dp, vertical = 7.dp)) {
            Row(verticalAlignment = Alignment.CenterVertically) {
                Tracked("PROCESSING", color = Hud.Violet, size = 9.sp, spacing = 3.sp)
                Text(" ▋", color = Hud.Violet.copy(alpha = if (b < 0.5f) 1f else 0f), fontSize = 11.sp)
            }
        }
    }
}

/** A confirmation: amber, the steps as the phone reads them, and time running out. */
@Composable
private fun Authorize(p: ConfirmPrompt, onYes: () -> Unit, onNo: () -> Unit) {
    val now = rememberNow(100)
    val total = remember(p.requestId) { (p.expiresAtMs - System.currentTimeMillis()).coerceAtLeast(1) }
    val left = (p.expiresAtMs - now).coerceAtLeast(0)
    Panel("AUTHORIZATION", accent = Hud.Amber, live = true, badge = {
        Badge(if (p.tier >= 3) "TIER ${p.tier} · UNLOCK" else "TIER ${p.tier}", Hud.Amber, live = true)
    }) {
        Tracked("NORA WANTS TO", color = Hud.Amber.copy(alpha = 0.7f), size = 9.sp, spacing = 3.sp)
        Spacer(Modifier.height(6.dp))
        for ((action, params) in p.steps) {
            // What will actually run, as the phone reads it — not the core's summary.
            Row(Modifier.padding(vertical = 2.dp)) {
                Text("› ", color = Hud.Amber, fontFamily = Hud.Mono, fontSize = 14.sp)
                Text(ConfirmSteps.describe(action, params), color = Hud.Txt, fontFamily = Hud.Mono, fontSize = 14.sp,
                    lineHeight = 20.sp)
            }
        }
        Spacer(Modifier.height(10.dp))
        Countdown(left.toFloat() / total, Hud.Amber)
        Row(Modifier.padding(top = 4.dp)) {
            Spacer(Modifier.weight(1f))
            Tracked("EXPIRES ${left / 1000}S", color = Hud.Amber.copy(alpha = 0.7f), size = 8.sp)
        }
        Spacer(Modifier.height(8.dp))
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            HudButton("DENY", onNo, Modifier.weight(1f), color = Hud.Light)
            HudButton(if (p.tier >= 3) "UNLOCK + AUTHORIZE" else "AUTHORIZE", onYes, Modifier.weight(1f),
                color = Hud.Amber, strong = true)
        }
    }
}

// ── system ───────────────────────────────────────────────────────────────────

@Composable
private fun SystemTab(c: LinkController, state: LinkState, killed: Boolean) {
    val ctx = LocalContext.current
    val disabled by c.disabled.collectAsState()
    val hiddenApps by c.hiddenNoteApps.collectAsState()
    val now = rememberNow()
    var confirmUnpair by remember { mutableStateOf(false) }

    val notifLauncher = rememberLauncherForActivityResult(ActivityResultContracts.RequestPermission()) {}
    val locationLauncher = rememberLauncherForActivityResult(ActivityResultContracts.RequestMultiplePermissions()) {}
    var notifGranted by remember { mutableStateOf(false) }
    var batteryExempt by remember { mutableStateOf(false) }
    var listenerGranted by remember { mutableStateOf(false) }
    var locationGranted by remember { mutableStateOf(false) }
    var seenApps by remember { mutableStateOf(emptyMap<String, String>()) }
    LaunchedEffect(now / 5000) {
        notifGranted = ctx.checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) == PackageManager.PERMISSION_GRANTED
        batteryExempt = ctx.getSystemService(PowerManager::class.java).isIgnoringBatteryOptimizations(ctx.packageName)
        listenerGranted = NoraNotificationListener.hasAccess(ctx)
        locationGranted = ctx.checkSelfPermission(Manifest.permission.ACCESS_FINE_LOCATION) == PackageManager.PERMISSION_GRANTED
        seenApps = NoraNotificationListener.seenApps()
    }
    LaunchedEffect(Unit) { if (!notifGranted) notifLauncher.launch(Manifest.permission.POST_NOTIFICATIONS) }

    Column(Modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(vertical = 4.dp),
        verticalArrangement = Arrangement.spacedBy(10.dp)) {
        val connected = state is LinkState.Connected
        val linkColor = when (state) {
            is LinkState.Connected -> Hud.Cyan
            is LinkState.NotAuthorised -> Hud.Amber
            else -> Hud.Red
        }
        Enter(true, 0) {
            Panel("CORE LINK", live = connected, accent = linkColor, badge = {
                Badge(if (connected) "RUNNING" else "DOWN", linkColor, live = connected)
            }) {
                Text(when (state) {
                    is LinkState.Connected -> "CONNECTED"
                    is LinkState.NotAuthorised -> "AWAITING APPROVAL"
                    is LinkState.Waiting -> "RECONNECTING"
                    LinkState.Connecting -> "CONNECTING"
                    LinkState.Stopped -> "NOT RUNNING"
                }, fontFamily = Hud.Disp, fontWeight = FontWeight.Bold, fontSize = 30.sp, letterSpacing = 3.sp,
                    color = linkColor, style = TextStyle(shadow = Hud.glow(linkColor, 24f)))
                when (state) {
                    is LinkState.NotAuthorised -> {
                        Detail("The core hasn't let this phone in yet (${state.message}). On the core:")
                        Code("python -m nora.hub approve ${c.deviceId}")
                        Detail("Retrying in ${((state.retryAtMs - now) / 1000).coerceAtLeast(0)} s")
                    }
                    is LinkState.Waiting -> Detail("${state.reason} — retrying in ${((state.retryAtMs - now) / 1000).coerceAtLeast(0)} s")
                    else -> Detail(Notifications.describe(state))
                }
                if (!connected) {
                    Spacer(Modifier.height(10.dp))
                    HudButton("RETRY NOW", { if (state == LinkState.Stopped) LinkService.start(ctx) else c.kick() },
                        Modifier.fillMaxWidth(), color = linkColor, strong = true)
                }
            }
        }

        Enter(true, 80) {
            Panel("REMOTE CONTROL", accent = if (killed) Hud.Amber else Hud.Cyan, badge = {
                Badge(if (killed) "LOCKED" else "ARMED", if (killed) Hud.Amber else Hud.Green, live = true)
            }) {
                Row(verticalAlignment = Alignment.CenterVertically) {
                    Detail(if (killed) "Off. NORA can't act on this phone." else "On. NORA can use what's switched on below.",
                        Modifier.weight(1f))
                    HudToggle(!killed, { c.setKilled(!it) })
                }
            }
        }

        Enter(true, 160) {
            Panel("CAPABILITIES", badge = { Badge("${c.capabilities.count { it.name !in disabled }}/${c.capabilities.size} ON") }) {
                c.capabilities.forEachIndexed { i, cap ->
                    if (i > 0) Hairline()
                    Row(Modifier.padding(vertical = 7.dp), verticalAlignment = Alignment.CenterVertically) {
                        Column(Modifier.weight(1f)) {
                            Row(verticalAlignment = Alignment.CenterVertically) {
                                Text(cap.name, color = if (cap.name in disabled) Hud.Dim else Hud.Cyan,
                                    fontFamily = Hud.Mono, fontSize = 13.sp)
                                Spacer(Modifier.width(8.dp))
                                Badge(tierLabel(cap.tier), tierColor(cap.tier), live = cap.name !in disabled)
                            }
                            Text(cap.description, color = Hud.Dim, fontFamily = Hud.Mono, fontSize = 11.sp, lineHeight = 15.sp)
                        }
                        Spacer(Modifier.width(10.dp))
                        HudToggle(cap.name !in disabled, { c.setEnabled(cap.name, it) })
                    }
                }
            }
        }

        Enter(true, 240) {
            Panel("PHONE ACCESS") {
                Access("NOTIFICATIONS", notifGranted) { notifLauncher.launch(Manifest.permission.POST_NOTIFICATIONS) }
                Hairline()
                Access("STAY CONNECTED IN BACKGROUND", batteryExempt) {
                    @SuppressLint("BatteryLife")
                    val i = Intent(Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS, Uri.parse("package:${ctx.packageName}"))
                    ctx.startActivity(i)
                }
                Hairline()
                Access("READ NOTIFICATIONS + MEDIA", listenerGranted) {
                    ctx.startActivity(Intent(Settings.ACTION_NOTIFICATION_LISTENER_DETAIL_SETTINGS)
                        .putExtra(Settings.EXTRA_NOTIFICATION_LISTENER_COMPONENT_NAME,
                            NoraNotificationListener.component(ctx).flattenToString()))
                }
                Hairline()
                Access("LOCATION, WHEN ASKED", locationGranted) {
                    locationLauncher.launch(arrayOf(Manifest.permission.ACCESS_FINE_LOCATION,
                        Manifest.permission.ACCESS_COARSE_LOCATION))
                }
                Spacer(Modifier.height(8.dp))
                Detail("Opening apps, links, Maps and the clock only works while this app is on screen; " +
                    "otherwise NORA leaves a notification to tap. Android's rule, not NORA's.")
            }
        }

        if (listenerGranted) {
            Enter(true, 320) {
                Panel("NOTIFICATION SOURCES") {
                    Detail("Read only when you ask, kept in memory for a day, never saved. Switch an app off " +
                        "and NORA never sees its notifications.")
                    Spacer(Modifier.height(6.dp))
                    val apps = (seenApps + hiddenApps.filter { it !in seenApps }.associateWith { it })
                        .entries.sortedBy { it.value.lowercase() }
                    if (apps.isEmpty()) Tracked("[ NONE SEEN YET ]", color = Hud.Faint, size = 9.sp, spacing = 2.6.sp)
                    for ((pkg, label) in apps) {
                        Row(Modifier.padding(vertical = 5.dp), verticalAlignment = Alignment.CenterVertically) {
                            Text(label, Modifier.weight(1f), color = Hud.Txt, fontFamily = Hud.Mono, fontSize = 13.sp)
                            HudToggle(pkg !in hiddenApps, { c.setNoteAppHidden(pkg, !it) })
                        }
                    }
                }
            }
        }

        Enter(true, 400) {
            Panel("DEVICE") {
                SelectionContainer {
                    Column(verticalArrangement = Arrangement.spacedBy(4.dp)) {
                        KeyValue("ID", c.deviceId.orEmpty())
                        KeyValue("CORE", c.url.orEmpty())
                        KeyValue("KEY", c.keyHardware())
                    }
                }
                Spacer(Modifier.height(10.dp))
                HudButton("UNPAIR THIS PHONE", { confirmUnpair = true }, Modifier.fillMaxWidth(), color = Hud.Red)
            }
        }
        Spacer(Modifier.height(8.dp))
    }

    if (confirmUnpair) {
        Dialog(onDismissRequest = { confirmUnpair = false }) {
            Box(Modifier.background(Hud.Bg)) {
                Panel("UNPAIR", accent = Hud.Red, live = true) {
                    Detail("This phone's key is deleted and the audit log and chat cleared. Revoke it on the core too:")
                    Code("python -m nora.hub revoke ${c.deviceId}")
                    Spacer(Modifier.height(12.dp))
                    Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                        HudButton("CANCEL", { confirmUnpair = false }, Modifier.weight(1f), color = Hud.Light)
                        HudButton("UNPAIR", { confirmUnpair = false; c.unpair() }, Modifier.weight(1f),
                            color = Hud.Red, strong = true)
                    }
                }
            }
        }
    }
}

@Composable
private fun Access(label: String, ok: Boolean, onFix: () -> Unit) {
    Row(Modifier.padding(vertical = 7.dp), verticalAlignment = Alignment.CenterVertically) {
        Tracked(label, Modifier.weight(1f), color = Hud.Txt, size = 10.sp, spacing = 1.5.sp)
        if (ok) {
            Row(verticalAlignment = Alignment.CenterVertically) {
                Dot(Hud.Green, blink = false); Spacer(Modifier.width(6.dp))
                Tracked("ALLOWED", color = Hud.Green, size = 9.sp)
            }
        } else {
            Box(Modifier.border(1.dp, Hud.Amber.copy(alpha = 0.5f), RoundedCornerShape(2.dp))
                .background(Hud.Amber.copy(alpha = 0.06f)).clickable(onClick = onFix)
                .padding(horizontal = 10.dp, vertical = 5.dp)) { Tracked("GRANT", color = Hud.Amber, size = 9.sp, spacing = 2.sp) }
        }
    }
}

@Composable
private fun Detail(text: String, modifier: Modifier = Modifier) {
    Text(text, modifier.padding(top = 2.dp), color = Hud.Dim, fontFamily = Hud.Mono, fontSize = 12.sp, lineHeight = 17.sp)
}

@Composable
private fun Code(text: String) {
    SelectionContainer {
        Text(text, Modifier.fillMaxWidth().padding(vertical = 6.dp)
            .background(Hud.Cyan.copy(alpha = 0.05f)).border(1.dp, Hud.Cyan.copy(alpha = 0.15f))
            .padding(10.dp), color = Hud.Cyan, fontFamily = Hud.Mono, fontSize = 12.sp)
    }
}

@Composable
private fun KeyValue(k: String, v: String) {
    Row {
        Tracked(k, Modifier.width(52.dp), size = 9.sp)
        Text(v, color = Hud.Txt, fontFamily = Hud.Mono, fontSize = 11.sp)
    }
}

@Composable
private fun Hairline() {
    Box(Modifier.fillMaxWidth().height(1.dp).background(Brush.horizontalGradient(listOf(
        Color.Transparent, Hud.Cyan.copy(alpha = 0.10f), Hud.Cyan.copy(alpha = 0.10f), Color.Transparent))))
}

/** Error codes, short enough to sit beside a capability name. */
private fun outcomeLabel(code: String) = when (code) {
    "ok" -> "DONE"
    "USER_DECLINED" -> "DECLINED"
    "BACKGROUND_RESTRICTED" -> "BACKGROUND"
    "EXECUTION_FAILED" -> "FAILED"
    "POLICY_BLOCKED" -> "BLOCKED"
    "CAPABILITY_UNAVAILABLE" -> "UNAVAILABLE"
    "INVALID_PARAMS" -> "INVALID"
    else -> code.replace('_', ' ').take(12)
}

private fun tierLabel(tier: Int) = when (tier) {
    0 -> "READ"
    1 -> "ACT"
    2 -> "ASKS"
    else -> "UNLOCK"
}

private fun tierColor(tier: Int) = when (tier) {
    0 -> Hud.Cyan
    1 -> Hud.Teal
    2 -> Hud.Amber
    else -> Hud.Red
}

// ── log ──────────────────────────────────────────────────────────────────────

@Composable
private fun LogTab(c: LinkController) {
    val tick by c.auditTick.collectAsState()
    var rows by remember { mutableStateOf(emptyList<AuditEntry>()) }
    LaunchedEffect(tick) { rows = c.auditLog() }
    Panel("COMMAND LOG", Modifier.padding(vertical = 4.dp), live = rows.isNotEmpty(),
        badge = { Badge("${rows.size} ENTRIES", live = rows.isNotEmpty()) }) {
        if (rows.isEmpty()) {
            Box(Modifier.fillMaxWidth().padding(vertical = 40.dp), contentAlignment = Alignment.Center) {
                Tracked("[ NO RECENT ACTIVITY ]", color = Hud.Faint, size = 9.sp, spacing = 2.6.sp)
            }
            return@Panel
        }
        LazyColumn(Modifier.fillMaxSize()) {
            items(rows) { e ->
                val ok = e.outcome == "ok"
                val col = if (ok) Hud.Green else if (e.outcome == "USER_DECLINED") Hud.Amber else Hud.Red
                Column(Modifier.padding(vertical = 8.dp)) {
                    Row(verticalAlignment = Alignment.CenterVertically) {
                        Dot(col, blink = false, size = 5.dp)
                        Spacer(Modifier.width(8.dp))
                        Text(e.capability, Modifier.weight(1f), color = Hud.Cyan, fontFamily = Hud.Mono, fontSize = 13.sp,
                            maxLines = 1, overflow = androidx.compose.ui.text.style.TextOverflow.Ellipsis)
                        Spacer(Modifier.width(8.dp))
                        Badge(outcomeLabel(e.outcome), col, live = true)
                    }
                    if (e.message.isNotEmpty()) {
                        Text(e.message, Modifier.padding(start = 13.dp, top = 3.dp), color = Hud.Txt,
                            fontFamily = Hud.Mono, fontSize = 12.sp, lineHeight = 17.sp)
                    }
                    Text("${dayStamp.format(Date(e.ts)).uppercase()} · ${e.origin.ifEmpty { "core" }} · tier ${e.tier} · ${e.durationMs} ms" +
                        if (e.params != "{}") " · ${e.params}" else "",
                        Modifier.padding(start = 13.dp, top = 3.dp), color = Hud.Faint, fontFamily = Hud.Mono,
                        fontSize = 10.sp, letterSpacing = 0.5.sp, maxLines = 2,
                        overflow = androidx.compose.ui.text.style.TextOverflow.Ellipsis)
                }
                Hairline()
            }
        }
    }
}

// ── pairing ──────────────────────────────────────────────────────────────────

@Composable
private fun PairScreen(c: LinkController, onScan: ((String?) -> Unit) -> Unit) {
    val scope = rememberCoroutineScope()
    var url by rememberSaveable { mutableStateOf("") }
    var code by rememberSaveable { mutableStateOf("") }
    var name by rememberSaveable { mutableStateOf(Build.MODEL) }
    var busy by remember { mutableStateOf(false) }
    var result by remember { mutableStateOf<String?>(null) }

    fun pair(invite: PairingInvite) {
        busy = true
        result = null
        scope.launch {
            result = when (val r = c.pair(invite, name.trim().ifEmpty { Build.MODEL })) {
                is PairResult.Paired -> null
                is PairResult.Refused -> "The core refused: ${r.message}"
                is PairResult.Failed -> "Couldn't reach the core: ${r.message}"
            }
            busy = false
        }
    }

    val orb = if (busy) OrbState.THINKING else OrbState.OFFLINE
    HudBackground(Hud.Cyan) {
        Column(Modifier.fillMaxSize().statusBarsPadding().navigationBarsPadding().imePadding()
            .verticalScroll(rememberScrollState()).padding(horizontal = 16.dp),
            horizontalAlignment = Alignment.CenterHorizontally, verticalArrangement = Arrangement.spacedBy(12.dp)) {
            Spacer(Modifier.height(12.dp))
            Orb(orb, 150.dp)
            Logotype(28.sp, 12.sp)
            Tracked(if (busy) "PAIRING" else "UNPAIRED", color = if (busy) Hud.Violet else Hud.Dim, spacing = 6.sp)
            Panel("PAIR WITH THE CORE") {
                Detail("On the core, run:")
                Code("python -m nora.hub pair")
                Detail("and scan the code it prints. Tailscale must be connected on this phone.")
                Spacer(Modifier.height(10.dp))
                Tracked("NAME FOR THIS PHONE", size = 9.sp)
                Spacer(Modifier.height(4.dp))
                HudField(name, { name = it }, "Pixel", Modifier.fillMaxWidth(), singleLine = true)
                Spacer(Modifier.height(10.dp))
                HudButton("SCAN PAIRING CODE", {
                    onScan { raw ->
                        val invite = raw?.let(PairingInvite::parse)
                        if (invite != null) pair(invite)
                        else if (raw != null) result = "That QR code isn't a NORA pairing code."
                    }
                }, Modifier.fillMaxWidth(), enabled = !busy, strong = true)
            }
            Panel("OR BY HAND") {
                Tracked("CORE ADDRESS", size = 9.sp)
                Spacer(Modifier.height(4.dp))
                HudField(url, { url = it.trim() }, "wss://…/v1/device", Modifier.fillMaxWidth(), singleLine = true)
                Spacer(Modifier.height(8.dp))
                Tracked("PAIRING CODE", size = 9.sp)
                Spacer(Modifier.height(4.dp))
                HudField(code, { code = it }, "ABCD-EFGH", Modifier.fillMaxWidth(), singleLine = true)
                Spacer(Modifier.height(10.dp))
                HudButton("PAIR", {
                    if (PairingInvite.isAcceptableUrl(url)) pair(PairingInvite(url, code.trim(), null))
                    else result = "The address must be wss:// and end in /v1/device."
                }, Modifier.fillMaxWidth(), enabled = !busy && url.isNotBlank() && code.isNotBlank())
            }
            result?.let {
                Text(it, color = Hud.Red, fontFamily = Hud.Mono, fontSize = 12.sp, textAlign = TextAlign.Center)
            }
            Spacer(Modifier.height(12.dp))
        }
    }
}
