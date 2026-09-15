package com.lxw.timetracker

import android.app.Activity
import android.app.AlertDialog
import android.content.ContentValues
import android.content.Intent
import android.content.SharedPreferences
import android.content.res.ColorStateList
import android.graphics.Color
import android.graphics.Paint
import android.graphics.Typeface
import android.graphics.drawable.ColorDrawable
import android.graphics.drawable.RippleDrawable
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.os.Environment
import android.os.Handler
import android.os.Looper
import android.provider.MediaStore
import android.provider.Settings
import android.view.Gravity
import android.view.View
import android.widget.Button
import android.widget.CheckBox
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.ProgressBar
import android.widget.TextView
import android.widget.Toast
import org.json.JSONArray
import org.json.JSONObject
import java.net.HttpURLConnection
import java.io.IOException
import java.net.URL
import java.util.Locale

/**
 * 手机端客户端：轮询电脑主机的 /state，点击卡片经 /control 远程开关计时。
 * 与桌面悬浮条共用同一套同步协议，零第三方依赖。
 */
class MainActivity : Activity() {

    companion object {
        val CATS = listOf("工作", "游戏", "学习")
        val COLORS = mapOf("工作" to "#4CAF50", "游戏" to "#F44336", "学习" to "#2196F3")
        const val PORT = 8765
        const val POLL_MS = 3000L
        const val TICK_MS = 1000L

        // 与桌面端相同的多源回退：raw.github 国内常超时，jsDelivr 一般可达
        val UPDATE_BASES = listOf(
            "https://raw.githubusercontent.com/rickylxw/SelfDiciplineClock/main/",
            "https://cdn.jsdelivr.net/gh/rickylxw/SelfDiciplineClock@main/",
            "https://fastly.jsdelivr.net/gh/rickylxw/SelfDiciplineClock@main/"
        )
        const val UPDATE_INFO_PATH = "android/version.json"
    }

    private lateinit var prefs: SharedPreferences
    private lateinit var hostEdit: EditText
    private lateinit var statusView: TextView
    private lateinit var updateView: TextView
    private val timeViews = mutableMapOf<String, TextView>()
    private val cardViews = mutableMapOf<String, LinearLayout>()
    private val bars = mutableMapOf<String, ProgressBar>()
    private lateinit var todoList: LinearLayout
    private lateinit var todoEdit: EditText

    // 显示口径：false=累计（与电脑端默认一致），true=今日
    private var showToday = false
    private var lastTodosSig = ""

    // 主机地址/连接状态等在 SyncState（与后台服务共享）

    // 自动更新：下载完成但缺「安装未知应用」权限时暂存的安装包
    private var pendingInstallUri: Uri? = null

    // 瞬时状态提示（连接中/切换中/失败），到期后恢复常规状态显示
    private var transientMsg: String? = null
    private var transientUntil = 0.0

    private val ui = Handler(Looper.getMainLooper())

    private val tick = object : Runnable {
        override fun run() {
            render()
            ui.postDelayed(this, TICK_MS)
        }
    }

    // 启动 2 秒后静默检查更新（有新版才弹窗，失败不打扰）
    private val startupUpdateCheck = Runnable { checkUpdate(manual = false) }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)
        prefs = getSharedPreferences("timetracker", MODE_PRIVATE)
        SyncState.host = prefs.getString("host", "") ?: ""
        SyncState.lastTouchTs = System.nanoTime() / 1_000_000_000.0
        hostEdit = findViewById(R.id.host_edit)
        statusView = findViewById(R.id.status_view)
        updateView = findViewById(R.id.update_view)
        updateView.text = "当前版本 v${versionInfo().second}"
        hostEdit.setText(SyncState.host)
        findViewById<Button>(R.id.connect_btn).setOnClickListener { connect() }
        findViewById<Button>(R.id.update_btn).setOnClickListener { checkUpdate(manual = true) }
        findViewById<Button>(R.id.mode_btn).setOnClickListener {
            showToday = !showToday
            findViewById<Button>(R.id.mode_btn).text =
                if (showToday) "当前：今日" else "当前：累计"
            render()
        }
        findViewById<Button>(R.id.pom_btn).setOnClickListener {
            togglePomodoro(!SyncState.pomEnabled)
        }
        todoList = findViewById(R.id.todo_list)
        todoEdit = findViewById(R.id.todo_edit)
        todoEdit.setTextColor(Color.parseColor("#EEEEEE"))
        findViewById<Button>(R.id.todo_add_btn).setOnClickListener { addTodo() }
        buildCards()
        // 常驻通知需要通知权限（Android 13+），拒绝不影响同步，只是通知不可见
        if (Build.VERSION.SDK_INT >= 33 &&
            checkSelfPermission(android.Manifest.permission.POST_NOTIFICATIONS)
                != android.content.pm.PackageManager.PERMISSION_GRANTED)
            requestPermissions(arrayOf(android.Manifest.permission.POST_NOTIFICATIONS), 1)
    }

    override fun onResume() {
        super.onResume()
        if (!SyncState.connected && SyncState.host.isNotEmpty())
            flashStatus("正在连接 ${SyncState.host}…", 4.0)
        ui.post(tick)
        // 后台同步交给前台服务：退到 App 外也持续轮询并上报在线，防止主机误暂停
        if (SyncState.host.isNotEmpty())
            startForegroundService(Intent(this, SyncService::class.java))
        ui.postDelayed(startupUpdateCheck, 2000)
        // 用户从设置里授予安装权限返回后，继续刚才中断的安装
        pendingInstallUri?.let { uri ->
            if (canInstallPackages()) {
                pendingInstallUri = null
                installApk(uri, versionInfo().second)
            }
        }
    }

    override fun onPause() {
        super.onPause()
        ui.removeCallbacks(tick)
        ui.removeCallbacks(startupUpdateCheck)
    }

    // ---------------- 界面 ----------------

    private fun buildCards() {
        val container = findViewById<LinearLayout>(R.id.cards)
        val density = resources.displayMetrics.density
        for (cat in CATS) {
            val row = LinearLayout(this).apply {
                orientation = LinearLayout.VERTICAL
                setPadding(36, 28, 36, 24)
                setBackgroundColor(Color.parseColor("#1F1F24"))
                // 按压涟漪：让点击有立即可见的视觉响应
                foreground = RippleDrawable(
                    ColorStateList.valueOf(Color.parseColor("#40FFFFFF")),
                    null, ColorDrawable(Color.WHITE))
                layoutParams = LinearLayout.LayoutParams(
                    LinearLayout.LayoutParams.MATCH_PARENT,
                    LinearLayout.LayoutParams.WRAP_CONTENT
                ).apply { bottomMargin = 24 }
                setOnClickListener { toggle(cat) }
            }
            val line = LinearLayout(this).apply {
                orientation = LinearLayout.HORIZONTAL
                gravity = Gravity.CENTER_VERTICAL
            }
            val name = TextView(this).apply {
                text = cat
                textSize = 17f
                setTypeface(typeface, Typeface.BOLD)
                setTextColor(Color.parseColor(COLORS[cat]))
                layoutParams = LinearLayout.LayoutParams(0,
                    LinearLayout.LayoutParams.WRAP_CONTENT, 0.35f)
            }
            val time = TextView(this).apply {
                textSize = 22f
                setTextColor(Color.parseColor("#BBBBBB"))
                layoutParams = LinearLayout.LayoutParams(0,
                    LinearLayout.LayoutParams.WRAP_CONTENT, 0.65f)
                gravity = Gravity.END
            }
            line.addView(name)
            line.addView(time)
            // 目标进度条：今日时长 / 每日目标（与电脑端口径一致）
            val bar = ProgressBar(this, null, android.R.attr.progressBarStyleHorizontal).apply {
                max = 1000
                progressTintList = ColorStateList.valueOf(Color.parseColor(COLORS[cat]))
                progressBackgroundTintList =
                    ColorStateList.valueOf(Color.parseColor("#333338"))
                layoutParams = LinearLayout.LayoutParams(
                    LinearLayout.LayoutParams.MATCH_PARENT,
                    (7 * density).toInt()
                ).apply { topMargin = (9 * density).toInt() }
            }
            row.addView(line)
            row.addView(bar)
            container.addView(row)
            timeViews[cat] = time
            cardViews[cat] = row
            bars[cat] = bar
        }
    }

    /** 瞬时状态提示（连接中/切换中/失败），到期后恢复常规状态显示。 */
    private fun flashStatus(msg: String, seconds: Double = 3.0) {
        transientMsg = msg
        transientUntil = System.nanoTime() / 1_000_000_000.0 + seconds
    }

    private fun render() {
        val liveTs = System.nanoTime() / 1_000_000_000.0
        for (cat in CATS) {
            val live = if (SyncState.runningCat == cat)
                (SyncState.liveStartElapsed + (liveTs - SyncState.liveStartTs)).toLong()
            else 0L
            val base = if (showToday) SyncState.todayTotals[cat] ?: 0L
                       else SyncState.totals[cat] ?: 0L
            timeViews[cat]?.text = fmt(base + live)
            if (live > 0L) {
                timeViews[cat]?.setTextColor(Color.parseColor(COLORS[cat]!!))
                cardViews[cat]?.setBackgroundColor(Color.parseColor("#26332A"))
            } else {
                timeViews[cat]?.setTextColor(Color.parseColor("#BBBBBB"))
                cardViews[cat]?.setBackgroundColor(Color.parseColor("#1F1F24"))
            }
            // 目标进度条固定按今日口径（与电脑端一致）
            val goal = SyncState.goals[cat] ?: 0.0
            val bar = bars[cat] ?: continue
            if (goal > 0.0) {
                val today = (SyncState.todayTotals[cat] ?: 0L) + live
                bar.visibility = View.VISIBLE
                bar.progress = ((today / goal).coerceIn(0.0, 1.0) * 1000).toInt()
            } else {
                bar.visibility = View.GONE
            }
        }
        findViewById<Button>(R.id.pom_btn).text =
            if (SyncState.pomEnabled) "🍅 番茄钟：开" else "🍅 番茄钟：关"
        val t = transientMsg
        if (t != null && liveTs < transientUntil) {
            statusView.text = t
            statusView.setTextColor(Color.parseColor("#FFC107"))
        } else {
            val nowEpoch = System.currentTimeMillis() / 1000.0
            val remain = (SyncState.pomEnd - nowEpoch).coerceAtLeast(0.0)
            val pomTag = when {
                SyncState.pomState == "focus" -> " · 🍅 剩余 ${mmss(remain)}"
                SyncState.pomState == "break" -> " · ☕ 休息剩余 ${mmss(remain)}"
                else -> ""
            }
            statusView.text = when {
                !SyncState.connected -> if (SyncState.host.isEmpty())
                    "未连接，请填写主机 IP"
                    else "连接 ${SyncState.host} 失败，点「连接」重试"
                SyncState.pomState == "break" ->
                    "☕ 休息中，剩余 ${mmss(remain)}，结束后自动开始专注"
                SyncState.runningCat != null ->
                    "● 主机正在计时：${SyncState.runningCat}$pomTag"
                else -> "已连接 ${SyncState.host} · 空闲"
            }
            statusView.setTextColor(if (SyncState.connected)
                Color.parseColor("#4CAF50") else Color.parseColor("#888888"))
        }
        // 待办内容有变化才重建列表，避免每秒闪烁
        val sig = SyncState.todosToday.toString()
        if (sig != lastTodosSig) {
            lastTodosSig = sig
            rebuildTodos()
        }
    }

    // ---------------- 今日待办 ----------------

    private fun rebuildTodos() {
        todoList.removeAllViews()
        val list = SyncState.todosToday
        val density = resources.displayMetrics.density
        if (list.length() == 0) {
            val empty = TextView(this).apply {
                text = "今天还没有待办，在下面添加一条吧"
                setTextColor(Color.parseColor("#666666"))
                textSize = 14f
                setPadding((4 * density).toInt(), (6 * density).toInt(), 0, 0)
            }
            todoList.addView(empty)
            return
        }
        for (i in 0 until list.length()) {
            val item = list.optJSONObject(i) ?: continue
            val done = item.optBoolean("done")
            val row = LinearLayout(this).apply {
                orientation = LinearLayout.HORIZONTAL
                gravity = Gravity.CENTER_VERTICAL
                setPadding(0, (6 * density).toInt(), 0, (6 * density).toInt())
                setOnClickListener { toggleTodo(i) }
            }
            CheckBox(this).apply {
                isChecked = done
                isClickable = false
                buttonTintList = ColorStateList.valueOf(Color.parseColor("#4CAF50"))
            }.let { row.addView(it) }
            TextView(this).apply {
                text = item.optString("text")
                textSize = 15f
                setTextColor(if (done) Color.parseColor("#666666")
                             else Color.parseColor("#DDDDDD"))
                if (done) paintFlags = paintFlags or Paint.STRIKE_THRU_TEXT_FLAG
                layoutParams = LinearLayout.LayoutParams(0,
                    LinearLayout.LayoutParams.WRAP_CONTENT, 1f).apply {
                    marginStart = (6 * density).toInt()
                }
            }.let { row.addView(it) }
            todoList.addView(row)
        }
    }

    /** 勾选/取消第 i 条待办：乐观更新界面，同时整表推送主机（时间戳新者胜）。 */
    private fun toggleTodo(index: Int) {
        val item = SyncState.todosToday.optJSONObject(index) ?: return
        val updated = JSONObject(item.toString()).put("done", !item.optBoolean("done"))
        applyAndPushTodo(index, updated)
    }

    private fun addTodo() {
        val text = todoEdit.text.toString().trim()
        if (text.isEmpty()) return
        applyAndPushTodo(SyncState.todosToday.length(),
            JSONObject().put("text", text).put("done", false))
        todoEdit.setText("")
    }

    /** 把修改写入待办表（今天不存在则按携入规则物化今天），乐观刷新并推送。 */
    private fun applyAndPushTodo(index: Int, newItem: JSONObject) {
        val todayKey = SyncState.todayStr()
        // 基于最新展示列表生成新今日列表
        val list = JSONArray()
        for (i in 0 until SyncState.todosToday.length()) {
            list.put(if (i == index) newItem
                     else JSONObject(SyncState.todosToday.optJSONObject(i)?.toString() ?: "{}"))
        }
        // 整表替换语义：必须基于完整表修改，否则会丢掉其他日期的待办
        val all = JSONObject(SyncState.todosAll.toString())
        all.put(todayKey, list)
        SyncState.todosToday = list
        SyncState.todosAll = all
        lastTodosSig = ""          // 强制下次 render 重建
        render()
        pushTodos(all)
    }

    private fun pushTodos(all: JSONObject) {
        if (SyncState.host.isEmpty()) return
        SyncState.lastTouchTs = System.nanoTime() / 1_000_000_000.0
        val ts = System.currentTimeMillis() / 1000.0
        Thread {
            try {
                httpPost("http://${SyncState.host}:$PORT/merge",
                    """{"todos":$all,"todos_ts":$ts,"user_idle":0}""")
                SyncState.todosPushedTs = System.nanoTime() / 1_000_000_000.0
                runOnUiThread {
                    startForegroundService(Intent(this, SyncService::class.java))
                }
            } catch (e: Exception) {
                runOnUiThread { flashStatus("❌ 待办同步失败，主机不可达", 4.0) }
            }
        }.start()
    }

    // ---------------- 自动更新 ----------------

    private fun versionInfo(): Pair<Long, String> {
        val pi = packageManager.getPackageInfo(packageName, 0)
        val code = if (Build.VERSION.SDK_INT >= 28) pi.longVersionCode
                   else pi.versionCode.toLong()
        return code to (pi.versionName ?: "?")
    }

    private fun canInstallPackages(): Boolean =
        Build.VERSION.SDK_INT < 26 || packageManager.canRequestPackageInstalls()

    /** 按源顺序拉取 version.json；manual=true 时把「失败/已最新」也提示出来。 */
    private fun checkUpdate(manual: Boolean) {
        updateView.text = "正在检查更新…"
        Thread {
            var info: JSONObject? = null
            var base = ""
            for (b in UPDATE_BASES) {
                try {
                    info = JSONObject(httpGet(b + UPDATE_INFO_PATH, timeout = 5000))
                    base = b
                    break
                } catch (e: Exception) {
                }
            }
            val newCode = info?.optInt("versionCode", 0)?.toLong() ?: -1L
            runOnUiThread {
                val (curCode, curName) = versionInfo()
                when {
                    newCode <= 0 -> {
                        updateView.text = "当前版本 v$curName"
                        if (manual) Toast.makeText(this,
                            "检查失败：无法访问更新源", Toast.LENGTH_SHORT).show()
                    }
                    newCode <= curCode -> {
                        updateView.text = "已是最新版本 v$curName"
                        if (manual) Toast.makeText(this,
                            "已是最新版本", Toast.LENGTH_SHORT).show()
                    }
                    else -> {
                        val newName = info!!.optString("versionName", newCode.toString())
                        val apk = info.optString("apk", "android/apk/timetracker.apk")
                        AlertDialog.Builder(this)
                            .setTitle("发现新版本")
                            .setMessage("新版本 v$newName 可用（当前 v$curName）。\n"
                                + "下载并安装？")
                            .setPositiveButton("更新") { _, _ ->
                                downloadApk(base, apk, newName)
                            }
                            .setNegativeButton("稍后", null)
                            .show()
                    }
                }
            }
        }.start()
    }

    /**
     * 应用内直接下载 APK 到「下载」目录（MediaStore，免存储权限）。
     * version.json 很小哪个源都通，APK 大得多——劣源会中途卡死，
     * 所以优先用检查更新时成功的源，失败再逐个换源重试。
     */
    private fun downloadApk(preferred: String, apkPath: String, verName: String) {
        updateView.text = "正在下载 v$verName…"
        Thread {
            val bases = if (preferred in UPDATE_BASES)
            listOf(preferred) + UPDATE_BASES.filter { it != preferred } else UPDATE_BASES
            var uri: Uri? = null
            var lastErr: Exception? = null
            for ((idx, b) in bases.withIndex()) {
                if (idx > 0) runOnUiThread {
                    updateView.text = "下载源超时，切换源重试 ${idx + 1}/${bases.size}…"
                }
                try {
                    uri = downloadOnce(b + apkPath, verName)
                    break
                } catch (e: Exception) {
                    lastErr = e
                    uri = null
                }
            }
            val done = uri
            runOnUiThread {
                if (done != null) {
                    updateView.text = "下载完成 v$verName"
                    installApk(done, verName)
                } else {
                    val msg = when (lastErr) {
                        is java.net.SocketTimeoutException -> "网络超时，请稍后重试或换网络"
                        else -> lastErr?.message ?: "网络错误"
                    }
                    updateView.text = "下载失败：$msg"
                }
            }
        }.start()
    }

    /** 单个源的一次完整下载；失败时清理残留的 MediaStore 记录后向上抛。 */
    private fun downloadOnce(url: String, verName: String): Uri {
        if (Build.VERSION.SDK_INT < 29) throw IOException("需要 Android 10 及以上")
        val conn = URL(url).openConnection() as HttpURLConnection
        conn.connectTimeout = 8000
        conn.readTimeout = 20000
        try {
            val total = conn.contentLengthLong
            val resolver = contentResolver
            val values = ContentValues().apply {
                put(MediaStore.Downloads.DISPLAY_NAME, "timetracker_$verName.apk")
                put(MediaStore.Downloads.MIME_TYPE,
                    "application/vnd.android.package-archive")
            }
            val uri = resolver.insert(MediaStore.Downloads.EXTERNAL_CONTENT_URI, values)
                ?: throw IOException("无法创建下载文件")
            try {
                resolver.openOutputStream(uri)?.use { out ->
                    conn.inputStream.use { input ->
                        val buf = ByteArray(64 * 1024)
                        var done = 0L
                        var lastPct = -1
                        while (true) {
                            val n = input.read(buf)
                            if (n < 0) break
                            out.write(buf, 0, n)
                            done += n
                            if (total > 0) {
                                val pct = (done * 100 / total).toInt()
                                if (pct != lastPct) {
                                    lastPct = pct
                                    runOnUiThread {
                                        if (!isFinishing && !isDestroyed)
                                            updateView.text =
                                                "正在下载 v$verName… $pct%"
                                    }
                                }
                            }
                        }
                    }
                } ?: throw IOException("下载流打开失败")
                return uri
            } catch (e: Exception) {
                contentResolver.delete(uri, null, null)
                throw e
            }
        } finally {
            conn.disconnect()
        }
    }

    /** 唤起系统安装器；无「安装未知应用」权限时先带用户去授权。 */
    private fun installApk(uri: Uri, verName: String) {
        if (!canInstallPackages()) {
            pendingInstallUri = uri
            updateView.text = "需要「安装未知应用」权限，授权后返回即可继续"
            startActivity(Intent(Settings.ACTION_MANAGE_UNKNOWN_APP_SOURCES,
                Uri.parse("package:$packageName")))
            return
        }
        updateView.text = "正在唤起系统安装器…"
        startActivity(Intent(Intent.ACTION_VIEW)
            .setDataAndType(uri, "application/vnd.android.package-archive")
            .addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION))
    }

    // ---------------- 网络 ----------------

    private fun connect() {
        // 容错解析：误带端口（10.0.2.2:8765）、http:// 前缀或路径都只保留主机地址
        val addr = hostEdit.text.toString().trim()
            .removePrefix("https://").removePrefix("http://")
            .substringBefore('/').substringBefore(':').trim()
        if (addr.isEmpty()) {
            Toast.makeText(this, "请输入主机 IP", Toast.LENGTH_SHORT).show()
            return
        }
        SyncState.host = addr
        SyncState.lastTouchTs = System.nanoTime() / 1_000_000_000.0
        flashStatus("正在连接 $addr…", 5.0)
        prefs.edit().putString("host", addr).apply()
        // 轮询与上报都在前台服务里，连接后立即开始
        startForegroundService(Intent(this, SyncService::class.java))
    }

    private fun toggle(cat: String) {
        if (!SyncState.connected) {
            Toast.makeText(this, "请先连接主机", Toast.LENGTH_SHORT).show()
            return
        }
        SyncState.lastTouchTs = System.nanoTime() / 1_000_000_000.0
        flashStatus("⏳ 正在切换「$cat」…")
        Thread {
            try {
                httpPost("http://${SyncState.host}:$PORT/control",
                    """{"action":"toggle","cat":"$cat"}""")
                runOnUiThread {
                    // 触发服务立即轮询一次，尽快刷新卡片状态
                    startForegroundService(Intent(this, SyncService::class.java))
                }
            } catch (e: Exception) {
                runOnUiThread {
                    flashStatus("❌ 主机不可达，未切换「$cat」", 4.0)
                    Toast.makeText(this, "主机不可达", Toast.LENGTH_SHORT).show()
                }
            }
        }.start()
    }

    private fun fmt(seconds: Long): String {
        val h = seconds / 3600
        val m = seconds % 3600 / 60
        val s = seconds % 60
        return String.format(Locale.CHINA, "%02d:%02d:%02d", h, m, s)
    }

    private fun mmss(seconds: Double): String {
        val total = seconds.toInt()
        return String.format(Locale.CHINA, "%02d:%02d", total / 60, total % 60)
    }

    /** 远程开关主机端番茄钟；主机空闲时开启会自动开始「工作」专注。 */
    private fun togglePomodoro(on: Boolean) {
        if (!SyncState.connected) {
            Toast.makeText(this, "请先连接主机", Toast.LENGTH_SHORT).show()
            return
        }
        SyncState.lastTouchTs = System.nanoTime() / 1_000_000_000.0
        flashStatus(if (on) "⏳ 正在开启番茄钟…" else "⏳ 正在关闭番茄钟…")
        Thread {
            try {
                httpPost("http://${SyncState.host}:$PORT/control",
                    """{"action":"pomodoro","on":$on}""")
                runOnUiThread {
                    startForegroundService(Intent(this, SyncService::class.java))
                }
            } catch (e: Exception) {
                runOnUiThread {
                    flashStatus("❌ 主机不可达，操作未执行", 4.0)
                    Toast.makeText(this, "主机不可达", Toast.LENGTH_SHORT).show()
                }
            }
        }.start()
    }
}
