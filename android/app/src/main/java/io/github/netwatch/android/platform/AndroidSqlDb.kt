package io.github.netwatch.android.platform

import android.content.Context
import android.database.Cursor
import android.database.sqlite.SQLiteCursor
import android.database.sqlite.SQLiteDatabase
import android.database.sqlite.SQLiteOpenHelper
import android.database.sqlite.SQLiteProgram
import io.github.netwatch.android.core.DatabaseException
import io.github.netwatch.android.core.Row
import io.github.netwatch.android.core.Schema
import io.github.netwatch.android.core.SqlDb
import java.io.File
import java.io.InputStream
import java.io.OutputStream

/**
 * [SqlDb] on Android's SQLite. Arguments are bound with their real types (rawQuery alone would bind
 * everything as TEXT, which breaks numeric comparisons such as MAX(started_at, ?)).
 *
 * Uses the default rollback journal (not WAL) so the database file is always consistent while a write
 * transaction is held, which makes backups by file copy safe.
 */
class AndroidSqlDb(private val context: Context, val name: String = "netwatch.db") : SqlDb {
    private var helper = newHelper()

    private fun newHelper() = object : SQLiteOpenHelper(context, name, null, 1) {
        override fun onConfigure(db: SQLiteDatabase) { db.setForeignKeyConstraintsEnabled(true) }
        override fun onCreate(db: SQLiteDatabase) {} // schema is managed by Schema.migrate (shared with the Linux app)
        override fun onUpgrade(db: SQLiteDatabase, oldVersion: Int, newVersion: Int) {}
    }

    private val db: SQLiteDatabase get() = helper.writableDatabase
    val file: File get() = context.getDatabasePath(name)

    private fun bind(p: SQLiteProgram, args: List<Any?>) {
        args.forEachIndexed { i, a ->
            val idx = i + 1
            when (a) {
                null -> p.bindNull(idx)
                is Boolean -> p.bindLong(idx, if (a) 1 else 0)
                is Int -> p.bindLong(idx, a.toLong())
                is Long -> p.bindLong(idx, a)
                is Double -> p.bindDouble(idx, a)
                is Float -> p.bindDouble(idx, a.toDouble())
                is ByteArray -> p.bindBlob(idx, a)
                else -> p.bindString(idx, a.toString())
            }
        }
    }

    override fun exec(sql: String, args: List<Any?>) {
        if (args.isEmpty()) db.execSQL(sql) else db.compileStatement(sql).use { bind(it, args); it.execute() }
    }

    override fun insert(sql: String, args: List<Any?>): Long = db.compileStatement(sql).use { bind(it, args); it.executeInsert() }

    override fun update(sql: String, args: List<Any?>): Int = db.compileStatement(sql).use { bind(it, args); it.executeUpdateDelete() }

    override fun query(sql: String, args: List<Any?>): List<Row> {
        val cursor = db.rawQueryWithFactory({ _, driver, table, q -> bind(q, args); SQLiteCursor(driver, table, q) }, sql, null, "")
        return cursor.use { c ->
            val out = ArrayList<Row>(c.count.coerceAtLeast(0))
            val names = c.columnNames
            while (c.moveToNext()) {
                val row = HashMap<String, Any?>(names.size * 2)
                for (i in names.indices) {
                    row[names[i]] = when (c.getType(i)) {
                        Cursor.FIELD_TYPE_INTEGER -> c.getLong(i)
                        Cursor.FIELD_TYPE_FLOAT -> c.getDouble(i)
                        Cursor.FIELD_TYPE_STRING -> c.getString(i)
                        Cursor.FIELD_TYPE_BLOB -> c.getBlob(i)
                        else -> null
                    }
                }
                out += row
            }
            out
        }
    }

    override fun <T> tx(block: () -> T): T {
        val d = db
        d.beginTransaction()
        try {
            val r = block()
            d.setTransactionSuccessful()
            return r
        } finally {
            d.endTransaction()
        }
    }

    override fun close() = helper.close()

    /** Consistent copy: writers are blocked by the open transaction while the file is copied. */
    fun backupTo(out: OutputStream) {
        val d = db
        d.beginTransaction()
        try {
            file.inputStream().use { it.copyTo(out) }
        } finally {
            d.endTransaction()
        }
    }

    /** Validate a backup copied to [candidate]; returns its schema version or throws. */
    fun validateBackup(candidate: File): Int {
        try {
            SQLiteDatabase.openDatabase(candidate.path, null, SQLiteDatabase.OPEN_READONLY).use { d ->
                d.rawQuery("PRAGMA integrity_check", null).use { c ->
                    if (!c.moveToFirst() || c.getString(0) != "ok") throw DatabaseException("The backup failed SQLite's integrity check.")
                }
                for (t in listOf("monitoring_sessions", "connection_states", "outages")) d.rawQuery("SELECT 1 FROM $t LIMIT 1", null).close()
                val v = d.rawQuery("SELECT MAX(version) FROM schema_migrations", null).use { if (it.moveToFirst()) it.getInt(0) else 0 }
                if (v > Schema.latest) throw DatabaseException("The backup was made by a newer version of NetWatch.")
                return v
            }
        } catch (e: DatabaseException) {
            throw e
        } catch (e: Exception) {
            throw DatabaseException("Not a NetWatch database: ${e.message}")
        }
    }

    /** Replace the live database with [input] after validating it. The caller stops monitoring first. */
    fun restoreFrom(input: InputStream, cacheDir: File, now: Double) {
        val tmp = File(cacheDir, "restore-candidate.db")
        tmp.outputStream().use { input.copyTo(it) }
        try {
            validateBackup(tmp)
            helper.close()
            tmp.copyTo(file, overwrite = true)
            File(file.path + "-journal").delete()
            helper = newHelper()
            Schema.migrate(this, now)
        } finally {
            tmp.delete()
        }
    }
}
