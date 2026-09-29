package com.aaditpani.nora.ui

import android.Manifest
import android.annotation.SuppressLint
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.os.PowerManager
import android.provider.Settings
import androidx.activity.ComponentActivity
import androidx.activity.compose.rememberLauncherForActivityResult
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
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.text.selection.SelectionContainer
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Switch
import androidx.compose.material3.Tab
import androidx.compose.material3.TabRow
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.dynamicDarkColorScheme
import androidx.compose.material3.dynamicLightColorScheme
import androidx.compose.foundation.isSystemInDarkTheme
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.unit.dp
import com.aaditpani.nora.link.AuditEntry
import com.aaditpani.nora.link.LinkState
import com.aaditpani.nora.link.PairResult
import com.aaditpani.nora.link.PairingInvite
import com.aaditpani.nora.phone.LinkController
import com.aaditpani.nora.phone.LinkService
import com.aaditpani.nora.phone.Notifications
import com.aaditpani.nora.phone.controller
import com.google.mlkit.vision.barcode.common.Barcode
import com.google.mlkit.vision.codescanner.GmsBarcodeScannerOptions
import com.google.mlkit.vision.codescanner.GmsBarcodeScanning
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import java.text.DateFormat
import java.util.Date

class MainActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        enableEdgeToEdge()
        val c = controller
        if (c.paired.value) LinkService.start(this)
        setContent {
            NoraTheme {
                val paired by c.paired.collectAsState()
                Scaffold(modifier = Modifier.fillMaxSize()) { pad ->
                    Column(Modifier.padding(pad).padding(horizontal = 16.dp)) {
                        if (paired) PairedScreen(c) else PairScreen(c, onScan = ::scan)
                    }
                }
            }
        }
    }

    private fun scan(onResult: (String?) -> Unit) {
        val options = GmsBarcodeScannerOptions.Builder().setBarcodeFormats(Barcode.FORMAT_QR_CODE).build()
        GmsBarcodeScanning.getClient(this, options).startScan()
            .addOnSuccessListener { onResult(it.rawValue) }
            .addOnFailureListener { onResult(null) }
            .addOnCanceledListener { onResult(null) }
    }
}

@Composable
private fun NoraTheme(content: @Composable () -> Unit) {
    val ctx = LocalContext.current
    val dark = isSystemInDarkTheme()
    MaterialTheme(colorScheme = if (dark) dynamicDarkColorScheme(ctx) else dynamicLightColorScheme(ctx),
        content = content)
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

    Column(Modifier.verticalScroll(rememberScrollState()), verticalArrangement = Arrangement.spacedBy(12.dp)) {
        Spacer(Modifier.height(16.dp))
        Text("Pair with NORA", style = MaterialTheme.typography.headlineMedium)
        Text("On the core, run:")
        Mono("python -m nora.hub pair")
        Text("and scan the code it prints. Tailscale must be connected on this phone.")
        OutlinedTextField(name, { name = it }, label = { Text("Name for this phone") },
            singleLine = true, modifier = Modifier.fillMaxWidth())
        Button(enabled = !busy, modifier = Modifier.fillMaxWidth(), onClick = {
            onScan { raw ->
                val invite = raw?.let(PairingInvite::parse)
                if (invite != null) pair(invite)
                else if (raw != null) result = "That QR code isn't a NORA pairing code."
            }
        }) { Text("Scan pairing code") }

        HorizontalDivider()
        Text("Or enter it by hand", style = MaterialTheme.typography.titleMedium)
        OutlinedTextField(url, { url = it.trim() }, label = { Text("Core address (wss://…/v1/device)") },
            singleLine = true, modifier = Modifier.fillMaxWidth())
        OutlinedTextField(code, { code = it }, label = { Text("Pairing code") },
            singleLine = true, modifier = Modifier.fillMaxWidth())
        OutlinedButton(enabled = !busy && url.isNotBlank() && code.isNotBlank(), modifier = Modifier.fillMaxWidth(),
            onClick = {
                if (PairingInvite.isAcceptableUrl(url)) pair(PairingInvite(url, code.trim(), null))
                else result = "The address must be wss:// and end in /v1/device."
            }) { Text("Pair") }
        if (busy) Text("Pairing…")
        result?.let { Text(it, color = MaterialTheme.colorScheme.error) }
    }
}

// ── paired ───────────────────────────────────────────────────────────────────

@Composable
private fun PairedScreen(c: LinkController) {
    var tab by rememberSaveable { mutableIntStateOf(0) }
    TabRow(selectedTabIndex = tab) {
        Tab(tab == 0, { tab = 0 }, text = { Text("Status") })
        Tab(tab == 1, { tab = 1 }, text = { Text("Audit log") })
    }
    if (tab == 0) StatusTab(c) else AuditTab(c)
}

@Composable
private fun StatusTab(c: LinkController) {
    val ctx = LocalContext.current
    val state by c.state.collectAsState()
    val killed by c.killed.collectAsState()
    val disabled by c.disabled.collectAsState()
    var confirmUnpair by remember { mutableStateOf(false) }
    var now by remember { mutableStateOf(System.currentTimeMillis()) }
    LaunchedEffect(Unit) { while (true) { delay(1000); now = System.currentTimeMillis() } }

    val notifLauncher = rememberLauncherForActivityResult(ActivityResultContracts.RequestPermission()) {}
    var notifGranted by remember { mutableStateOf(false) }
    var batteryExempt by remember { mutableStateOf(false) }
    LaunchedEffect(now / 5000) {
        notifGranted = ctx.checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) == PackageManager.PERMISSION_GRANTED
        batteryExempt = ctx.getSystemService(PowerManager::class.java).isIgnoringBatteryOptimizations(ctx.packageName)
    }
    LaunchedEffect(Unit) { if (!notifGranted) notifLauncher.launch(Manifest.permission.POST_NOTIFICATIONS) }

    Column(Modifier.verticalScroll(rememberScrollState()).padding(vertical = 12.dp),
        verticalArrangement = Arrangement.spacedBy(12.dp)) {
        Card(Modifier.fillMaxWidth()) {
            Column(Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
                Text(when (state) {
                    is LinkState.Connected -> "Connected"
                    is LinkState.NotAuthorised -> "Waiting for approval"
                    is LinkState.Waiting -> "Reconnecting"
                    LinkState.Connecting -> "Connecting…"
                    LinkState.Stopped -> "Not running"
                }, style = MaterialTheme.typography.titleLarge)
                when (val s = state) {
                    is LinkState.NotAuthorised -> {
                        Text("The core hasn't let this phone in yet (${s.message}). On the core:")
                        Mono("python -m nora.hub approve ${c.deviceId}")
                        Text("Retrying in ${((s.retryAtMs - now) / 1000).coerceAtLeast(0)} s")
                    }
                    is LinkState.Waiting -> Text("${s.reason} — retrying in ${((s.retryAtMs - now) / 1000).coerceAtLeast(0)} s")
                    else -> Text(Notifications.describe(s))
                }
                if (state !is LinkState.Connected) {
                    OutlinedButton(onClick = { if (state == LinkState.Stopped) LinkService.start(ctx) else c.kick() }) {
                        Text("Retry now")
                    }
                }
            }
        }

        Card(Modifier.fillMaxWidth()) {
            Row(Modifier.padding(16.dp), verticalAlignment = Alignment.CenterVertically) {
                Column(Modifier.weight(1f)) {
                    Text("Remote control", style = MaterialTheme.typography.titleMedium)
                    Text(if (killed) "Off: NORA can't act on this phone." else "On: NORA can use what's switched on below.")
                }
                Switch(checked = !killed, onCheckedChange = { c.setKilled(!it) })
            }
        }

        Text("What NORA can do here", style = MaterialTheme.typography.titleMedium)
        for (cap in c.capabilities) {
            Row(verticalAlignment = Alignment.CenterVertically) {
                Column(Modifier.weight(1f)) {
                    Text(cap.name, fontFamily = FontFamily.Monospace)
                    Text("${tierLabel(cap.tier)} · ${cap.description}", style = MaterialTheme.typography.bodySmall)
                }
                Switch(checked = cap.name !in disabled, onCheckedChange = { c.setEnabled(cap.name, it) })
            }
        }

        HorizontalDivider()
        Text("Phone settings", style = MaterialTheme.typography.titleMedium)
        SettingRow("Notifications", notifGranted, "Allow") { notifLauncher.launch(Manifest.permission.POST_NOTIFICATIONS) }
        SettingRow("Stay connected in the background", batteryExempt, "Allow") {
            @SuppressLint("BatteryLife")
            val i = Intent(Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS, Uri.parse("package:${ctx.packageName}"))
            ctx.startActivity(i)
        }

        HorizontalDivider()
        SelectionContainer {
            Column {
                Text("Device ${c.deviceId}", style = MaterialTheme.typography.bodySmall)
                Text("Core ${c.url}", style = MaterialTheme.typography.bodySmall)
                Text("Key held in ${c.keyHardware()}", style = MaterialTheme.typography.bodySmall)
            }
        }
        TextButton(onClick = { confirmUnpair = true }) { Text("Unpair this phone") }
    }

    if (confirmUnpair) {
        AlertDialog(
            onDismissRequest = { confirmUnpair = false },
            title = { Text("Unpair?") },
            text = { Text("This phone's key is deleted and the audit log cleared. Revoke it on the core too: python -m nora.hub revoke ${c.deviceId}") },
            confirmButton = { TextButton(onClick = { confirmUnpair = false; c.unpair() }) { Text("Unpair") } },
            dismissButton = { TextButton(onClick = { confirmUnpair = false }) { Text("Cancel") } },
        )
    }
}

@Composable
private fun SettingRow(label: String, ok: Boolean, action: String, onFix: () -> Unit) {
    Row(verticalAlignment = Alignment.CenterVertically) {
        Text(label, Modifier.weight(1f))
        if (ok) Text("Allowed", color = MaterialTheme.colorScheme.primary)
        else OutlinedButton(onClick = onFix) { Text(action) }
    }
}

@Composable
private fun AuditTab(c: LinkController) {
    val tick by c.auditTick.collectAsState()
    var rows by remember { mutableStateOf(emptyList<AuditEntry>()) }
    LaunchedEffect(tick) { rows = c.auditLog() }
    if (rows.isEmpty()) {
        Text("Nothing yet. Every request NORA makes of this phone shows up here.",
            Modifier.padding(vertical = 16.dp))
        return
    }
    val fmt = remember { DateFormat.getDateTimeInstance(DateFormat.SHORT, DateFormat.MEDIUM) }
    LazyColumn(Modifier.fillMaxSize(), verticalArrangement = Arrangement.spacedBy(8.dp)) {
        items(rows) { e ->
            Card(Modifier.fillMaxWidth()) {
                Column(Modifier.padding(12.dp)) {
                    Row {
                        Text(e.capability, Modifier.weight(1f), fontFamily = FontFamily.Monospace)
                        Text(if (e.outcome == "ok") "done" else e.outcome,
                            color = if (e.outcome == "ok") MaterialTheme.colorScheme.primary else MaterialTheme.colorScheme.error)
                    }
                    if (e.message.isNotEmpty()) Text(e.message, style = MaterialTheme.typography.bodyMedium)
                    Text("${fmt.format(Date(e.ts))} · ${e.origin.ifEmpty { "core" }} · tier ${e.tier} · ${e.durationMs} ms" +
                        if (e.params != "{}") " · ${e.params}" else "",
                        style = MaterialTheme.typography.bodySmall)
                }
            }
        }
    }
}

@Composable
private fun Mono(text: String) {
    SelectionContainer { Text(text, fontFamily = FontFamily.Monospace) }
}

private fun tierLabel(tier: Int) = when (tier) {
    0 -> "reads"
    1 -> "acts, logged"
    2 -> "asks first"
    else -> "asks + unlock"
}
