package com.aaditpani.nora.phone

import android.content.Context
import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyInfo
import android.security.keystore.KeyProperties
import android.security.keystore.StrongBoxUnavailableException
import com.aaditpani.nora.link.RotatableSigner
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
 *
 * Rotation (Phase 10): Keystore aliases can't be renamed, so which alias is
 * in use is kept in prefs, and the next key gets an alias of its own until
 * the core has taken it. The first key keeps its original alias.
 */
class KeystoreSigner(context: Context) : RotatableSigner {
    private val keyStore: KeyStore = KeyStore.getInstance(PROVIDER).apply { load(null) }
    private val prefs = context.getSharedPreferences("nora_key", Context.MODE_PRIVATE)

    private val alias: String get() = prefs.getString("alias", null) ?: LEGACY_ALIAS
    private val nextAlias: String? get() = prefs.getString("next_alias", null)

    override val createdAtMs: Long get() = prefs.getLong("created_at", 0L)

    fun hasKey(): Boolean = keyStore.containsAlias(alias)

    /** Create the key if there isn't one: in StrongBox if the phone has it. */
    fun ensureKey() {
        if (hasKey()) return
        generate(alias)
        prefs.edit().putLong("created_at", System.currentTimeMillis()).apply()
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

    /** Unpairing forgets the key (and any next one); pairing again makes a new identity. */
    fun deleteKey() {
        if (hasKey()) keyStore.deleteEntry(alias)
        discardNext()
    }

    override fun publicKeyDer(): ByteArray = keyStore.getCertificate(alias).publicKey.encoded

    override fun sign(payload: ByteArray): ByteArray = signWith(alias, payload)

    override fun prepareNext(): Signer {
        val a = nextAlias?.takeIf { keyStore.containsAlias(it) } ?: run {
            val fresh = "nora-device-key-${System.currentTimeMillis()}"
            generate(fresh)
            prefs.edit().putString("next_alias", fresh).commit()
            fresh
        }
        return object : Signer {
            override fun publicKeyDer(): ByteArray = keyStore.getCertificate(a).publicKey.encoded
            override fun sign(payload: ByteArray): ByteArray = signWith(a, payload)
        }
    }

    override fun commitNext() {
        val next = nextAlias ?: return
        val old = alias
        // commit(), not apply(): the alias in use must be on disk before the old key goes.
        prefs.edit().putString("alias", next).remove("next_alias")
            .putLong("created_at", System.currentTimeMillis()).commit()
        if (old != next && keyStore.containsAlias(old)) keyStore.deleteEntry(old)
    }

    override fun discardNext() {
        val next = nextAlias ?: return
        if (keyStore.containsAlias(next)) keyStore.deleteEntry(next)
        prefs.edit().remove("next_alias").commit()
    }

    /** Swap the two: harmless if the refusal was for some other reason, since the other stays. */
    override fun adoptNext(): Boolean {
        val next = nextAlias?.takeIf { keyStore.containsAlias(it) } ?: return false
        prefs.edit().putString("alias", next).putString("next_alias", alias).commit()
        return true
    }

    private fun signWith(a: String, payload: ByteArray): ByteArray = Signature.getInstance("SHA256withECDSA").run {
        initSign(keyStore.getKey(a, null) as PrivateKey)
        update(payload)
        sign()
    }

    private fun generate(a: String) {
        try {
            generate(a, strongBox = true)
        } catch (_: StrongBoxUnavailableException) {
            generate(a, strongBox = false)
        }
    }

    private fun generate(a: String, strongBox: Boolean) {
        val spec = KeyGenParameterSpec.Builder(a, KeyProperties.PURPOSE_SIGN)
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
        const val LEGACY_ALIAS = "nora-device-key"
    }
}
