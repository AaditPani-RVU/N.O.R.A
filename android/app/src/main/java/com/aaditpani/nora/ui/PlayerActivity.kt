package com.aaditpani.nora.ui

import android.app.KeyguardManager
import android.content.Context
import android.content.Intent
import android.net.Uri
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.SystemBarStyle
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.compose.animation.animateColorAsState
import androidx.compose.animation.core.tween
import androidx.compose.foundation.border
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.imePadding
import androidx.compose.foundation.layout.navigationBarsPadding
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.statusBarsPadding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import com.aaditpani.nora.phone.LockPlayer
import com.aaditpani.nora.phone.Spotify
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.darkColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableLongStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberUpdatedState
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import kotlinx.coroutines.delay
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

/**
 * The music screen on its own launcher icon. It needs nothing from the link —
 * not pairing, not the laptop — only notification access, so it works
 * wherever the phone is. [LockPlayer] also opens it over the lock screen.
 */
class PlayerActivity : ComponentActivity() {
    /** Opened over the lock screen by [LockPlayer], rather than from the launcher. */
    private val overLock = mutableStateOf(false)
    private val tab = mutableIntStateOf(TAB_NOW)
    /** The browser's return from Spotify sign-in, for the LIBRARY screen to finish. */
    private val callback = mutableStateOf<Uri?>(null)

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        takeLockIntent(intent)
        takeCallback(intent)
        enableEdgeToEdge(SystemBarStyle.dark(android.graphics.Color.TRANSPARENT),
            SystemBarStyle.dark(android.graphics.Color.TRANSPARENT))
        setContent {
            MaterialTheme(colorScheme = darkColorScheme(primary = Hud.Cyan, background = Color.Black,
                surface = Color.Black, onSurface = Hud.Txt, onBackground = Hud.Txt)) {
                PlayerScreen(overLock.value, onUnlock = ::unlock, tab.intValue, { tab.intValue = it },
                    callback.value, onCallbackDone = { callback.value = null })
            }
        }
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        takeLockIntent(intent)
        takeCallback(intent)
    }

    private fun takeCallback(intent: Intent?) {
        val data = intent?.data
        if (!Spotify.isCallback(data)) return
        callback.value = data
        tab.intValue = TAB_LIBRARY
    }

    private fun takeLockIntent(intent: Intent?) {
        if (intent?.getBooleanExtra(EXTRA_LOCK, false) != true) return
        overLock.value = true
        setShowWhenLocked(true)
        LockPlayer.opened(this)
    }

    /**
     * Over the lock screen it is a visitor: once it leaves the screen — the
     * phone sleeps, home, unlock to somewhere else — it goes, so the next
     * wake brings up a fresh one and the lock screen is never left covered.
     */
    override fun onStop() {
        super.onStop()
        if (overLock.value) { setShowWhenLocked(false); finish() }
    }

    private fun unlock() {
        getSystemService(KeyguardManager::class.java).requestDismissKeyguard(this,
            object : KeyguardManager.KeyguardDismissCallback() {
                override fun onDismissSucceeded() { finish() }
            })
    }

    companion object {
        private const val EXTRA_LOCK = "over_lock"
        const val TAB_NOW = 0
        const val TAB_LIBRARY = 1

        fun lockIntent(context: Context): Intent = Intent(context, PlayerActivity::class.java)
            .putExtra(EXTRA_LOCK, true)
            .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
    }
}

private val playerClock = SimpleDateFormat("HH:mm", Locale.US)

@Composable
private fun PlayerScreen(
    overLock: Boolean,
    onUnlock: () -> Unit,
    tab: Int,
    onTab: (Int) -> Unit,
    callback: Uri?,
    onCallbackDone: () -> Unit,
) {
    var art by remember { mutableStateOf<Color?>(null) }
    val accent by animateColorAsState(art ?: Hud.Cyan, tween(900), label = "accent")
    // Over the lock screen there is only the player: the library would need the phone unlocked anyway.
    val shown = if (overLock) PlayerActivity.TAB_NOW else tab
    val showing by rememberUpdatedState(shown)
    HudBackground(accent, oled = true) {
        Column(Modifier.fillMaxSize().statusBarsPadding().navigationBarsPadding().imePadding()) {
            PlayerHeader(accent, overLock, onUnlock)
            if (!overLock) PlayerTabs(accent, shown, onTab)
            Box(Modifier.weight(1f).padding(horizontal = 16.dp)) {
                // The library keeps the colour of whatever is playing: leaving NOW
                // PLAYING reports null on the way out, which is ignored here.
                if (shown == PlayerActivity.TAB_NOW) MusicTab(settings = !overLock) { if (it != null || showing == PlayerActivity.TAB_NOW) art = it }
                else LibraryTab(accent, callback, onCallbackDone)
            }
        }
    }
}

@Composable
private fun PlayerTabs(accent: Color, tab: Int, onTab: (Int) -> Unit) {
    Row(Modifier.fillMaxWidth().padding(start = 16.dp, end = 16.dp, bottom = 10.dp),
        horizontalArrangement = Arrangement.spacedBy(8.dp)) {
        listOf("NOW PLAYING", "LIBRARY").forEachIndexed { i, label ->
            val on = i == tab
            Box(Modifier.weight(1f)
                .background(if (on) accent.copy(alpha = 0.08f) else Color.Transparent)
                .border(1.dp, accent.copy(alpha = if (on) 0.45f else 0.12f))
                .clickable { onTab(i) }
                .padding(vertical = 8.dp), contentAlignment = Alignment.Center) {
                Tracked(label, color = if (on) accent else accent.copy(alpha = 0.45f), size = 9.sp, spacing = 2.5.sp, glow = on)
            }
        }
    }
}

/** The app header, cut down: sigil, logotype, a PLAYER tag and the clock. No link pill — there is no link here. */
@Composable
private fun PlayerHeader(accent: Color, overLock: Boolean, onUnlock: () -> Unit) {
    var now by remember { mutableLongStateOf(System.currentTimeMillis()) }
    LaunchedEffect(Unit) { while (true) { delay(1000); now = System.currentTimeMillis() } }
    Column {
        Row(Modifier.fillMaxWidth().padding(horizontal = 16.dp, vertical = 10.dp),
            verticalAlignment = Alignment.CenterVertically) {
            Sigil(accent)
            Spacer(Modifier.width(10.dp))
            Logotype(19.sp, 7.sp, accent)
            Spacer(Modifier.width(8.dp))
            Box(Modifier.border(1.dp, accent.copy(alpha = 0.15f)).padding(horizontal = 5.dp, vertical = 2.dp)) {
                Tracked("PLAYER", color = accent, size = 8.sp, spacing = 1.5.sp)
            }
            Spacer(Modifier.weight(1f))
            Tracked(playerClock.format(Date(now)), color = accent, size = 13.sp, spacing = 3.sp, glow = true)
            if (overLock) {
                Spacer(Modifier.width(10.dp))
                Box(Modifier.border(1.dp, accent.copy(alpha = 0.4f)).clickable(onClick = onUnlock)
                    .padding(horizontal = 9.dp, vertical = 5.dp)) {
                    Tracked("UNLOCK", color = accent, size = 9.sp, spacing = 2.sp)
                }
            }
        }
        Box(Modifier.fillMaxWidth().height(1.dp).background(accent.copy(alpha = 0.14f)))
        Spacer(Modifier.height(10.dp))
    }
}
