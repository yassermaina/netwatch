package io.github.netwatch.android.platform

import android.content.ContentProvider
import android.content.ContentValues
import android.database.Cursor
import android.database.MatrixCursor
import android.net.Uri
import android.os.ParcelFileDescriptor
import android.provider.OpenableColumns
import io.github.netwatch.android.core.Reports
import java.io.File
import java.io.FileNotFoundException

/** Serves generated reports from the cache directory to share targets (minimal FileProvider replacement). */
class ReportsProvider : ContentProvider() {
    companion object {
        const val AUTHORITY = "io.github.netwatch.android.reports"
        fun dir(context: android.content.Context) = File(context.cacheDir, "shared-reports").apply { mkdirs() }
        fun uriFor(file: File): Uri = Uri.parse("content://$AUTHORITY/${Uri.encode(file.name)}")
    }

    private fun file(uri: Uri): File {
        val name = uri.lastPathSegment ?: throw FileNotFoundException()
        val f = File(dir(context!!), name)
        if (f.parentFile != dir(context!!) || !f.isFile) throw FileNotFoundException(name) // no path traversal
        return f
    }

    override fun onCreate() = true

    override fun openFile(uri: Uri, mode: String): ParcelFileDescriptor =
        ParcelFileDescriptor.open(file(uri), ParcelFileDescriptor.MODE_READ_ONLY)

    override fun query(uri: Uri, projection: Array<out String>?, selection: String?, args: Array<out String>?, sort: String?): Cursor {
        val f = file(uri)
        return MatrixCursor(arrayOf(OpenableColumns.DISPLAY_NAME, OpenableColumns.SIZE)).apply { addRow(arrayOf<Any>(f.name, f.length())) }
    }

    override fun getType(uri: Uri): String = Reports.mimeTypes[uri.lastPathSegment?.substringAfterLast('.')] ?: "application/octet-stream"
    override fun insert(uri: Uri, values: ContentValues?): Uri? = null
    override fun delete(uri: Uri, selection: String?, args: Array<out String>?) = 0
    override fun update(uri: Uri, values: ContentValues?, selection: String?, args: Array<out String>?) = 0
}
