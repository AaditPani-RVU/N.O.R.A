package com.aaditpani.nora.phone

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.service.quicksettings.Tile
import android.service.quicksettings.TileService

/**
 * Quick-settings kill switch (plan §7.8). Active = NORA may act on this
 * phone. One tap turns it off at once, locally first: the phone refuses every
 * invocation before the core has even heard.
 */
class KillTileService : TileService() {
    override fun onStartListening() {
        super.onStartListening()
        render()
    }

    override fun onClick() {
        super.onClick()
        val c = controller
        if (!c.paired.value) return
        c.setKilled(!c.killed.value)
        render()
    }

    private fun render() {
        val tile = qsTile ?: return
        val c = controller
        tile.state = when {
            !c.paired.value -> Tile.STATE_UNAVAILABLE
            c.killed.value -> Tile.STATE_INACTIVE
            else -> Tile.STATE_ACTIVE
        }
        tile.subtitle = when {
            !c.paired.value -> "Not paired"
            c.killed.value -> "Off"
            else -> "On"
        }
        tile.updateTile()
    }
}

/** Reconnect after a reboot or an app update, if paired. */
class BootReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        if (intent.action != Intent.ACTION_BOOT_COMPLETED &&
            intent.action != Intent.ACTION_MY_PACKAGE_REPLACED) return
        if (context.controller.paired.value) LinkService.start(context)
    }
}
