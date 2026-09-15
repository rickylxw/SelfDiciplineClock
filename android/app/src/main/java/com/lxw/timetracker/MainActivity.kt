package com.lxw.timetracker

import android.app.Activity
import android.content.SharedPreferences
import android.graphics.Color
import android.graphics.Typeface
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.view.Gravity
import android.widget.Button
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.TextView
import android.widget.Toast
import org.json.JSONObject
import java.net.HttpURLConnection
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
    }

    private lateinit var prefs: SharedPreferences
    private lateinit var hostEdit: EditText
    private lateinit var statusView: TextView
    private val timeViews = mutableMapOf<String, TextView>()
    private val cardViews = mutableMapOf<String, LinearLayout>()

    private var host = ""
    private var totals = mutableMapOf("工作" to 0L, "游戏" to 0L, "学习" to 0L)
    private var runningCat: String? = null
    private var liveStartElapsed = 0.0   // 收到 state 时主机已计的秒数
    private var liveStartTs = 0.0        // 收到 state 时的本机时钟（秒）
    private var lastTouchTs = 0.0        // 最近一次触屏（秒），上报主机防误判离开
    private var connected = false

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

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)
        prefs = getSharedPreferences("timetracker", MODE_PRIVATE)
        hostEdit = findViewById(R.id.host_edit)
        statusView = findViewById(R.id.status_view)
        hostEdit.setText(prefs.getString("host", ""))
        findViewById<Button>(R.id.connect_btn).setOnClickListener { connect() }
        buildCards()
    }

    override fun onResume() {
        super.onResume()
        ui.post(tick)
        ui.post(poll)
    }

    override fun onPause() {
        super.onPause()
        ui.removeCallbacks(tick)
        ui.removeCallbacks(poll)
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
        statusView.text = when {
            !connected -> "未连接，请填写主机 IP"
            runningCat != null -> "● 主机正在计时：$runningCat"
            else -> "已连接 $host · 空闲"
        }
        statusView.setTextColor(
            if (connected) Color.parseColor("#4CAF50") else Color.parseColor("#888888"))
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
        Thread {
            try {
                httpPost("http://$host:$PORT/control",
                    """{"action":"toggle","cat":"$cat"}""")
                runOnUiThread { fetchState() }
            } catch (e: Exception) {
                runOnUiThread {
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

    private fun httpGet(url: String): String {
        val conn = URL(url).openConnection() as HttpURLConnection
        conn.connectTimeout = 2500
        conn.readTimeout = 2500
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
