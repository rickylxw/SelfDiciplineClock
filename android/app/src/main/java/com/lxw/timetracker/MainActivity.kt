package com.lxw.timetracker

import android.app.Activity
import android.app.AlertDialog
import android.content.ContentValues
import android.content.Intent
import android.content.SharedPreferences
import android.content.res.ColorStateList
import android.graphics.Color
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
import android.widget.Button
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.TextView
import android.widget.Toast
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

    private var host = ""
    private var totals = mutableMapOf("工作" to 0L, "游戏" to 0L, "学习" to 0L)
    private var runningCat: String? = null
    private var liveStartElapsed = 0.0   // 收到 state 时主机已计的秒数
    private var liveStartTs = 0.0        // 收到 state 时本机时钟（秒）
    private var lastTouchTs = 0.0        // 最近一次触屏（秒），上报主机防误判离开
    private var connected = false

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

    private val poll = object : Runnable {
        override fun run() {
            if (host.isNotEmpty()) {
                fetchState()
                reportActivity()
            }
            ui.postDelayed(this, POLL_MS)
        }
    }

    // 启动 2 秒后静默检查更新（有新版才弹窗，失败不打扰）
    private val startupUpdateCheck = Runnable { checkUpdate(manual = false) }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)
        prefs = getSharedPreferences("timetracker", MODE_PRIVATE)
        hostEdit = findViewById(R.id.host_edit)
        statusView = findViewById(R.id.status_view)
        updateView = findViewById(R.id.update_view)
        updateView.text = "当前版本 v${versionInfo().second}"
        hostEdit.setText(prefs.getString("host", ""))
        findViewById<Button>(R.id.connect_btn).setOnClickListener { connect() }
        findViewById<Button>(R.id.update_btn).setOnClickListener { checkUpdate(manual = true) }
        buildCards()
    }

    override fun onResume() {
        super.onResume()
        if (!connected && host.isNotEmpty()) flashStatus("正在连接 $host…", 4.0)
        ui.post(tick)
        ui.post(poll)
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
        ui.removeCallbacks(poll)
        ui.removeCallbacks(startupUpdateCheck)
    }

    // ---------------- 界面 ----------------

    private fun buildCards() {
        val container = findViewById<LinearLayout>(R.id.cards)
        for (cat in CATS) {
            val row = LinearLayout(this).apply {
                orientation = LinearLayout.HORIZONTAL
                gravity = Gravity.CENTER_VERTICAL
                setPadding(36, 32, 36, 32)
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
            row.addView(name)
            row.addView(time)
            container.addView(row)
            timeViews[cat] = time
            cardViews[cat] = row
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
            var seconds = totals[cat] ?: 0L
            val live = if (runningCat == cat)
                (liveStartElapsed + (liveTs - liveStartTs)).toLong() else 0L
            seconds += live
            timeViews[cat]?.text = fmt(seconds)
            if (live > 0L) {
                timeViews[cat]?.setTextColor(Color.parseColor(COLORS[cat]!!))
                cardViews[cat]?.setBackgroundColor(Color.parseColor("#26332A"))
            } else {
                timeViews[cat]?.setTextColor(Color.parseColor("#BBBBBB"))
                cardViews[cat]?.setBackgroundColor(Color.parseColor("#1F1F24"))
            }
        }
        val t = transientMsg
        if (t != null && liveTs < transientUntil) {
            statusView.text = t
            statusView.setTextColor(Color.parseColor("#FFC107"))
        } else {
            statusView.text = when {
                !connected -> if (host.isEmpty()) "未连接，请填写主机 IP"
                              else "连接 $host 失败，点「连接」重试"
                runningCat != null -> "● 主机正在计时：$runningCat"
                else -> "已连接 $host · 空闲"
            }
            statusView.setTextColor(
                if (connected) Color.parseColor("#4CAF50") else Color.parseColor("#888888"))
        }
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
        val addr = hostEdit.text.toString().trim()
        if (addr.isEmpty()) {
            Toast.makeText(this, "请输入主机 IP", Toast.LENGTH_SHORT).show()
            return
        }
        host = addr
        lastTouchTs = System.nanoTime() / 1_000_000_000.0
        flashStatus("正在连接 $host…", 5.0)
        fetchState()
    }

    private fun fetchState() {
        Thread {
            try {
                val body = httpGet("http://$host:$PORT/state")
                val st = JSONObject(body)
                val daily = st.optJSONObject("daily") ?: JSONObject()
                val sum = mutableMapOf("工作" to 0L, "游戏" to 0L, "学习" to 0L)
                for (key in daily.keys()) {
                    val day = daily.getJSONObject(key)
                    for (cat in CATS) sum[cat] = sum[cat]!! + day.optLong(cat, 0)
                }
                val rc = if (st.isNull("running_cat")) null else st.getString("running_cat")
                runOnUiThread {
                    connected = true
                    totals = sum
                    runningCat = rc
                    if (rc != null) {
                        liveStartElapsed = st.optDouble("elapsed", 0.0)
                        liveStartTs = System.nanoTime() / 1_000_000_000.0
                    }
                    prefs.edit().putString("host", host).apply()
                    render()
                }
            } catch (e: Exception) {
                runOnUiThread {
                    connected = false
                    runningCat = null
                    render()
                    if (host.isNotEmpty())
                        flashStatus("连接 $host 失败，请检查主机与网络", 3.0)
                }
            }
        }.start()
    }

    private fun toggle(cat: String) {
        if (!connected) {
            Toast.makeText(this, "请先连接主机", Toast.LENGTH_SHORT).show()
            return
        }
        lastTouchTs = System.nanoTime() / 1_000_000_000.0
        flashStatus("⏳ 正在切换「$cat」…")
        Thread {
            try {
                httpPost("http://$host:$PORT/control",
                    """{"action":"toggle","cat":"$cat"}""")
                runOnUiThread { fetchState() }
            } catch (e: Exception) {
                runOnUiThread {
                    flashStatus("❌ 主机不可达，未切换「$cat」", 4.0)
                    Toast.makeText(this, "主机不可达", Toast.LENGTH_SHORT).show()
                }
            }
        }.start()
    }

    private fun reportActivity() {
        val idle = System.nanoTime() / 1_000_000_000.0 - lastTouchTs
        Thread {
            try {
                httpPost("http://$host:$PORT/activity",
                         """{"idle":$idle}""")
            } catch (e: Exception) {
                // 上报失败不影响主流程
            }
        }.start()
    }

    private fun httpGet(url: String, timeout: Int = 2500): String {
        val conn = URL(url).openConnection() as HttpURLConnection
        conn.connectTimeout = timeout
        conn.readTimeout = timeout
        try {
            return conn.inputStream.bufferedReader().readText()
        } finally {
            conn.disconnect()
        }
    }

    private fun httpPost(url: String, body: String): String {
        val conn = URL(url).openConnection() as HttpURLConnection
        conn.requestMethod = "POST"
        conn.connectTimeout = 2500
        conn.readTimeout = 2500
        conn.doOutput = true
        conn.setRequestProperty("Content-Type", "application/json")
        try {
            conn.outputStream.use { it.write(body.toByteArray(Charsets.UTF_8)) }
            return conn.inputStream.bufferedReader().readText()
        } finally {
            conn.disconnect()
        }
    }

    private fun fmt(seconds: Long): String {
        val h = seconds / 3600
        val m = seconds % 3600 / 60
        val s = seconds % 60
        return String.format(Locale.CHINA, "%02d:%02d:%02d", h, m, s)
    }
}
