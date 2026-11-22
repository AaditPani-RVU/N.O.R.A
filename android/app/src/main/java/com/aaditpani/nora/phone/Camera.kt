package com.aaditpani.nora.phone

import android.content.Context
import android.content.Intent
import android.graphics.Bitmap
import android.graphics.ImageDecoder
import android.net.Uri
import android.os.Bundle
import android.util.Log
import androidx.activity.ComponentActivity
import androidx.activity.result.contract.ActivityResultContracts
import androidx.core.content.FileProvider
import com.aaditpani.nora.link.Capability
import com.aaditpani.nora.link.CapabilityResult
import com.aaditpani.nora.link.ErrorCode
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import kotlinx.coroutines.withTimeoutOrNull
import org.json.JSONObject
import java.io.ByteArrayOutputStream
import java.io.File
import java.util.UUID
import java.util.concurrent.ConcurrentHashMap

/**
 * "What is this?" (plan Phase 7, spec example 3): one photo, through the
 * phone's own camera app, sent to the core for the vision model.
 *
 * Only when the user asks, live (`requiresLiveUser`): never from a job, a
 * rule or a notification. Android only opens the camera from the screen,
 * so NORA has to be visible (the voice screen or chat); otherwise it says
 * so. The photo is shrunk to [MAX_EDGE] px, sent over the link and deleted
 * here; the core keeps it in memory for a few minutes unless told to save it.
 *
 * Not offered to the model as-is: the core reaches it through
 * `look_with_phone`, which also asks the vision model.
 */
class TakePhoto(private val context: Context) : Capability {
    override val name = "phone.take_photo"
    override val description = "Take one photo with the phone's camera for NORA to look at"
    override val tier = 1
    override val requiresLiveUser = true
    override val paramsSchema: JSONObject = JSONObject("""
        {"type":"object","properties":{"prompt":{"type":"string","maxLength":80}}}""")

    override suspend fun execute(params: JSONObject): CapabilityResult {
        if (!Foreground.visible) {
            return CapabilityResult.Failed(ErrorCode.BACKGROUND_RESTRICTED,
                "Android only opens the camera while NORA is on the phone's screen. Open NORA and ask again.",
                userAction = "open_app")
        }
        val id = UUID.randomUUID().toString()
        val dir = File(context.cacheDir, "photos").apply { mkdirs() }
        dir.listFiles()?.forEach { it.delete() }              // nothing left from an earlier one
        val file = File(dir, "$id.jpg")
        val uri = FileProvider.getUriForFile(context, "${context.packageName}.photos", file)
        val done = Photos.start(id)
        context.startActivity(Intent(context, PhotoActivity::class.java)
            .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
            .putExtra(PhotoActivity.EXTRA_ID, id)
            .putExtra(PhotoActivity.EXTRA_URI, uri))
        val taken = withTimeoutOrNull(WAIT_MS) { done.await() }
        Photos.forget(id)
        try {
            if (taken != true || !file.exists() || file.length() == 0L) {
                return CapabilityResult.Failed(ErrorCode.USER_DECLINED, "No photo was taken.")
            }
            val jpeg = withContext(Dispatchers.Default) { shrink(file) }
                ?: return CapabilityResult.Failed(ErrorCode.EXECUTION_FAILED, "The phone couldn't read the photo.")
            if (!context.controller.sendBlob(id, jpeg, "image/jpeg")) {
                return CapabilityResult.Failed(ErrorCode.EXECUTION_FAILED,
                    "The link dropped before the photo could go.", retryable = true)
            }
            Log.i(TAG, "photo sent: ${jpeg.size / 1024} KB")
            return CapabilityResult.Ok(JSONObject().put("blob_id", id).put("bytes", jpeg.size)
                .put("message", "Photo taken."))
        } finally {
            file.delete()
        }
    }

    /** Long edge to [MAX_EDGE], JPEG at [QUALITY]. ImageDecoder applies the EXIF rotation. */
    private fun shrink(file: File): ByteArray? = runCatching {
        val bitmap = ImageDecoder.decodeBitmap(ImageDecoder.createSource(file)) { decoder, info, _ ->
            val w = info.size.width
            val h = info.size.height
            val scale = MAX_EDGE.toFloat() / maxOf(w, h)
            if (scale < 1f) decoder.setTargetSize((w * scale).toInt().coerceAtLeast(1), (h * scale).toInt().coerceAtLeast(1))
            decoder.allocator = ImageDecoder.ALLOCATOR_SOFTWARE
        }
        val out = ByteArrayOutputStream()
        bitmap.compress(Bitmap.CompressFormat.JPEG, QUALITY, out)
        bitmap.recycle()
        out.toByteArray()
    }.onFailure { Log.w(TAG, "shrinking the photo failed: $it") }.getOrNull()

    private companion object {
        const val TAG = "NoraCamera"
        const val WAIT_MS = 80_000L
        const val MAX_EDGE = 1600
        const val QUALITY = 82
    }
}

/** Photos being taken, by id: the capability waits on its entry. */
object Photos {
    private val waiting = ConcurrentHashMap<String, CompletableDeferred<Boolean>>()

    fun start(id: String): CompletableDeferred<Boolean> = CompletableDeferred<Boolean>().also { waiting[id] = it }
    fun finish(id: String, taken: Boolean) { waiting.remove(id)?.complete(taken) }
    fun forget(id: String) { waiting.remove(id)?.cancel() }
}

/** Invisible: hands over to the camera app and back. */
class PhotoActivity : ComponentActivity() {
    private var id: String = ""

    private val take = registerForActivityResult(ActivityResultContracts.TakePicture()) { ok ->
        Photos.finish(id, ok)
        finish()
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        id = intent.getStringExtra(EXTRA_ID).orEmpty()
        @Suppress("DEPRECATION")
        val uri = intent.getParcelableExtra(EXTRA_URI, Uri::class.java)
        if (savedInstanceState == null) {
            if (id.isEmpty() || uri == null) {
                Photos.finish(id, false)
                finish()
                return
            }
            try {
                take.launch(uri)
            } catch (e: Exception) {
                Log.w("NoraCamera", "no camera app: $e")
                Photos.finish(id, false)
                finish()
            }
        } else {
            id = savedInstanceState.getString(EXTRA_ID, id)
        }
    }

    override fun onSaveInstanceState(outState: Bundle) {
        super.onSaveInstanceState(outState)
        outState.putString(EXTRA_ID, id)
    }

    companion object {
        const val EXTRA_ID = "id"
        const val EXTRA_URI = "uri"
    }
}
