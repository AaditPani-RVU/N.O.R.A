package com.aaditpani.nora.phone

import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyInfo
import android.security.keystore.KeyProperties
import android.security.keystore.StrongBoxUnavailableException
import com.aaditpani.nora.link.Signer
import java.security.KeyFactory
import java.security.KeyPairGenerator
import java.security.KeyStore
import java.security.PrivateKey
import java.security.Signature
import java.security.spec.ECGenParameterSpec

/**
 * The phone's identity: a P-256 key generated inside Android Keystore
 * (StrongBox on the Pixel when it's available) that never leaves it. The
 * core holds only the public half; there is no shared secret on the phone
 * (plan §5 "Pairing", §7.1).
 */
class KeystoreSigner(private val alias: String = "nora-device-key") : Signer {
    private val keyStore: KeyStore = KeyStore.getInstance(PROVIDER).apply { load(null) }

    fun hasKey(): Boolean = keyStore.containsAlias(alias)

    /** Create the key if there isn't one: in StrongBox if the phone has it. */
    fun ensureKey() {
        if (hasKey()) return
        try {
            generate(strongBox = true)
        } catch (_: StrongBoxUnavailableException) {
            generate(strongBox = false)
        }
    }

    /** Where the key lives, for the status screen: "StrongBox", "TEE" or "software". */
    fun hardware(): String {
        val key = keyStore.getKey(alias, null) as? PrivateKey ?: return "none"
        val info = KeyFactory.getInstance(key.algorithm, PROVIDER).getKeySpec(key, KeyInfo::class.java)
        return when (info.securityLevel) {
            KeyProperties.SECURITY_LEVEL_STRONGBOX -> "StrongBox"
            KeyProperties.SECURITY_LEVEL_TRUSTED_ENVIRONMENT -> "TEE"
            else -> "software"
        }
    }

    /** Unpairing forgets the key; pairing again makes a new identity. */
    fun deleteKey() {
        if (hasKey()) keyStore.deleteEntry(alias)
    }

    override fun publicKeyDer(): ByteArray = keyStore.getCertificate(alias).publicKey.encoded

    override fun sign(payload: ByteArray): ByteArray = Signature.getInstance("SHA256withECDSA").run {
        initSign(keyStore.getKey(alias, null) as PrivateKey)
        update(payload)
        sign()
    }

    private fun generate(strongBox: Boolean) {
        val spec = KeyGenParameterSpec.Builder(alias, KeyProperties.PURPOSE_SIGN)
            .setAlgorithmParameterSpec(ECGenParameterSpec("secp256r1"))
            .setDigests(KeyProperties.DIGEST_SHA256)
            .setIsStrongBoxBacked(strongBox)
            .build()
        KeyPairGenerator.getInstance(KeyProperties.KEY_ALGORITHM_EC, PROVIDER).apply {
            initialize(spec)
            generateKeyPair()
        }
    }

    private companion object {
        const val PROVIDER = "AndroidKeyStore"
    }
}
