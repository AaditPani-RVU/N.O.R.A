package com.aaditpani.nora.phone

import android.app.NotificationManager
import android.app.Service
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.content.pm.ServiceInfo
import android.net.ConnectivityManager
import android.net.Network
import android.os.IBinder
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.flow.combine
import kotlinx.coroutines.launch

/**
 * Keeps the link to the core open while the phone is paired. A foreground
 * service (special use: there is no FGS type for "stay reachable by my own
 * server") so the socket survives the app leaving the screen; its
 * notification carries the kill switch.
 *
 * When the network comes back — Wi-Fi joins, airplane mode ends — it kicks
 * the link to reconnect at once instead of waiting out the backoff. Charger
 * plugged or unplugged is sent to the core as a `device.status` event.
 */
class LinkService : Service() {
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Main)
    private var networkCallback: ConnectivityManager.NetworkCallback? = null

    private val power = object : BroadcastReceiver() {
        override fun onReceive(context: Context, intent: Intent) = controller.emitStatus()
    }

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onCreate() {
        super.onCreate()
        startForeground(Notifications.LINK_ID,
            Notifications.link(this, controller.state.value, controller.killed.value),
            ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE)
        scope.launch {
            controller.state.combine(controller.killed) { s, k -> s to k }.collect { (s, k) ->
                getSystemService(NotificationManager::class.java)
                    .notify(Notifications.LINK_ID, Notifications.link(this@LinkService, s, k))
            }
        }
        networkCallback = object : ConnectivityManager.NetworkCallback() {
            override fun onAvailable(network: Network) {
                controller.kick()
            }
        }.also { getSystemService(ConnectivityManager::class.java).registerDefaultNetworkCallback(it) }
        registerReceiver(power, IntentFilter().apply {
            addAction(Intent.ACTION_POWER_CONNECTED)
            addAction(Intent.ACTION_POWER_DISCONNECTED)
        }, RECEIVER_NOT_EXPORTED)
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        if (intent?.action == ACTION_TOGGLE_KILL) {
            controller.setKilled(!controller.killed.value)
        }
        if (controller.paired.value) controller.startLink() else stopSelf()
        return START_STICKY
    }

    override fun onDestroy() {
        networkCallback?.let { getSystemService(ConnectivityManager::class.java).unregisterNetworkCallback(it) }
        unregisterReceiver(power)
        scope.cancel()
        super.onDestroy()
    }

    companion object {
        const val ACTION_TOGGLE_KILL = "com.aaditpani.nora.TOGGLE_KILL"

        fun start(context: Context) {
            context.startForegroundService(Intent(context, LinkService::class.java))
        }

        fun stop(context: Context) {
            context.stopService(Intent(context, LinkService::class.java))
        }

        /** Redraw the notification after a change made elsewhere (tile, app). */
        fun refresh(context: Context) {
            val c = context.controller
            if (!c.paired.value) return
            context.getSystemService(NotificationManager::class.java)
                .notify(Notifications.LINK_ID, Notifications.link(context, c.state.value, c.killed.value))
        }
    }
}
