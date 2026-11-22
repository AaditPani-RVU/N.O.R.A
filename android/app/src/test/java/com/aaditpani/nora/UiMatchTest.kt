package com.aaditpani.nora

import com.aaditpani.nora.link.UiMatch
import com.aaditpani.nora.link.UiNode
import com.aaditpani.nora.link.UiRecipes
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

class UiMatchTest {
    private val wa = UiRecipes.WHATSAPP

    @Test fun findsWhatsAppsSendButtonById() {
        val send = UiNode(wa, viewId = "com.whatsapp:id/send", clickable = true)
        val root = UiNode(wa, children = listOf(UiNode(wa, viewId = "com.whatsapp:id/entry", clickable = true), send))
        assertEquals(send, UiMatch.find(root, wa, UiRecipes.WHATSAPP_SEND))
    }

    @Test fun aLabelInsideAButtonClicksTheButton() {
        val label = UiNode(UiRecipes.MAPS, text = "Start")
        val button = UiNode(UiRecipes.MAPS, clickable = true, children = listOf(label))
        val root = UiNode(UiRecipes.MAPS, children = listOf(button))
        assertEquals(button, UiMatch.find(root, UiRecipes.MAPS, UiRecipes.MAPS_START))
    }

    @Test fun neverPressesAnotherAppsButton() {
        val bank = UiNode("com.bank", children = listOf(UiNode("com.bank", desc = "Send", clickable = true)))
        assertNull(UiMatch.find(bank, wa, UiRecipes.WHATSAPP_SEND))
        // Even nested under an allowed app's window.
        val mixed = UiNode(wa, children = listOf(bank))
        assertNull(UiMatch.find(mixed, wa, UiRecipes.WHATSAPP_SEND))
    }

    @Test fun aDisabledButtonIsntPressed() {
        val root = UiNode(wa, children = listOf(UiNode(wa, desc = "Send", clickable = true, enabled = false)))
        assertNull(UiMatch.find(root, wa, UiRecipes.WHATSAPP_SEND))
    }
}
