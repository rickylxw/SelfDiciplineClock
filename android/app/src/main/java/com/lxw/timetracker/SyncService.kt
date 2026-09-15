package com.lxw.timetracker

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.os.Build
import android.os.Handler
import android.os.IBinder
import android.os.Looper
import android.util.Log
import org.json.JSONObject
import java.net.HttpURLConnection
import java.net.URL

/** 跨组件共享的同步状态：Service 后台轮询写入，Activity 前台读取渲染。 */
object SyncState {
    @Volatile var host = ""
    @Volatile var connected = false
    @Volatile var runningCat: String? = null
    @Volatile var liveStartElapsed = 0.0   // 收到 state 时主机已计的秒数
    @Volatile var liveStartTs = 0.0        // 收到 state 时本机时钟（秒）
    @Volatile var lastTouchTs = 0.0        // 最近一次手机端操作（秒）
    @Volatile var totals = mutableMapOf("工作" to 0L, "游戏" to 0L, "学习" to 0L)
    @Volatile var todayTotals = mutableMapOf("工作" to 0L, "游戏" to 0L, "学习" to 0L)
    @Volatile var goals = mapOf<String, Double>()

    // 待办：完整表按日期分组；todosToday 为今日展示列表（今天不存在时携入昨天未完成）
    @Volatile var todosAll = JSONObject()
    @Volatile var todosToday = org.json.JSONArray()
    @Volatile var todosPushedTs = 0.0      // 手机端最近一次推送待办的时刻，短暂抑制回写覆盖

    // 番茄钟：主机纪元秒制的阶段结束时刻；state 为 null 表示空闲
    @Volatile var pomEnabled = false
    @Volatile var pomState: String? = null
    @Volatile var pomEnd = 0.0

    fun todayStr(): String =
        java.text.SimpleDateFormat("yyyy-MM-dd", java.util.Locale.CHINA)
            .format(java.util.Date())
}

/**
 * 前台服务：App 退到后台后继续轮询主机并向主机上报「手机端在线」，
 * 避免人不在电脑前时主机因收不到任何活动报告而误判离开、自动暂停计时。
 */
class SyncService : Service() {

    companion object {
        const val TAG = "SyncSvc"
        const val CHANNEL_ID = "sync"
        const val NOTIF_ID = 1
    }

    private val ui = Handler(Looper.getMainLooper())
    private var loop: Runnable? = null

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onCreate() {
        super.onCreate()
        SyncState.lastTouchTs = System.nanoTime() / 1_000_000_000.0
        // 进程被系统回收后粘性重启时，从偏好里恢复主机地址
        if (SyncState.host.isEmpty()) {
            val p = getSharedPreferences("timetracker", Context.MODE_PRIVATE)
            SyncState.host = p.getString("host", "") ?: ""
        }
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        goForeground()
        loop?.let { ui.removeCallbacks(it) }
        val r = object : Runnable {
            override fun run() {
                if (SyncState.host.isEmpty()) {
                    stopSelf()
                    return
                }
                Log.v(TAG, "tick host=${SyncState.host}")
                fetchState()
                reportActivity()
                ui.postDelayed(this, MainActivity.POLL_MS)
            }
        }
        loop = r
        ui.post(r)
        return START_STICKY
    }

    override fun onDestroy() {
        loop?.let { ui.removeCallbacks(it) }
        super.onDestroy()
    }

    private fun goForeground() {
        val notif = buildNotification()
        if (Build.VERSION.SDK_INT >= 34)
            startForeground(NOTIF_ID, notif,
                ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE)
        else
            startForeground(NOTIF_ID, notif)
    }

    private fun buildNotification(): Notification {
        if (Build.VERSION.SDK_INT >= 26) {
            val ch = NotificationChannel(CHANNEL_ID, "主机同步",
                NotificationManager.IMPORTANCE_LOW)
            getSystemService(NotificationManager::class.java)
                .createNotificationChannel(ch)
        }
        val b = if (Build.VERSION.SDK_INT >= 26)
            Notification.Builder(this, CHANNEL_ID)
        else
            Notification.Builder(this)
        return b.setContentTitle("时长同步运行中")
            .setContentText("与主机 ${SyncState.host} 保持连接，防止计时被误暂停")
            .setSmallIcon(android.R.drawable.stat_notify_sync)
            .setOngoing(true)
            .build()
    }

    private fun fetchState() {
        Thread {
            try {
                val body = httpGet("http://${SyncState.host}:${MainActivity.PORT}/state")
                val st = JSONObject(body)
                val daily = st.optJSONObject("daily") ?: JSONObject()
                val sum = mutableMapOf("工作" to 0L, "游戏" to 0L, "学习" to 0L)
                val todaySum = mutableMapOf("工作" to 0L, "游戏" to 0L, "学习" to 0L)
                val todayKey = SyncState.todayStr()
                val todayObj = daily.optJSONObject(todayKey)
                for (key in daily.keys()) {
                    val day = daily.getJSONObject(key)
                    for (cat in MainActivity.CATS) {
                        sum[cat] = sum[cat]!! + day.optLong(cat, 0)
                        if (key == todayKey)
                            todaySum[cat] = todaySum[cat]!! + day.optLong(cat, 0)
                    }
                }
                val goals = mutableMapOf<String, Double>()
                val goalsObj = st.optJSONObject("goals")
                if (goalsObj != null)
                    for (cat in MainActivity.CATS)
                        goals[cat] = goalsObj.optDouble(cat, 0.0)
                val rc = if (st.isNull("running_cat")) null
                         else st.getString("running_cat")
                SyncState.totals = sum
                SyncState.todayTotals = todaySum
                SyncState.goals = goals
                SyncState.runningCat = rc
                SyncState.connected = true
                val pom = st.optJSONObject("pom")
                SyncState.pomEnabled = pom?.optBoolean("enabled", false) ?: false
                SyncState.pomState = if (pom == null || pom.isNull("state")) null
                                     else pom.optString("state")
                SyncState.pomEnd = pom?.optDouble("end", 0.0) ?: 0.0
                if (rc != null) {
                    SyncState.liveStartElapsed = st.optDouble("elapsed", 0.0)
                    SyncState.liveStartTs = System.nanoTime() / 1_000_000_000.0
                }
                // 待办：手机端刚推送过就先不覆盖，等主机消化后再取，避免显示回跳
                if (System.nanoTime() / 1_000_000_000.0 - SyncState.todosPushedTs > 5.0) {
                    val todosObj = st.optJSONObject("todos") ?: JSONObject()
                    SyncState.todosAll = todosObj
                    SyncState.todosToday = displayTodos(todosObj, todayKey)
                }
            } catch (e: Exception) {
                SyncState.connected = false
                SyncState.runningCat = null
            }
        }.start()
    }

    /** 今日待办列表；今天还没有条目时展示昨天未完成的（与电脑端携入逻辑一致）。 */
    private fun displayTodos(todosObj: JSONObject, todayKey: String): org.json.JSONArray {
        todosObj.optJSONArray(todayKey)?.let { return it }
        val yesterday = java.text.SimpleDateFormat("yyyy-MM-dd", java.util.Locale.CHINA)
            .format(java.util.Date(System.currentTimeMillis() - 86_400_000L))
        val list = org.json.JSONArray()
        todosObj.optJSONArray(yesterday)?.let { yl ->
            for (i in 0 until yl.length()) {
                val item = yl.optJSONObject(i) ?: continue
                if (!item.optBoolean("done")) list.put(item)
            }
        }
        return list
    }

    private fun reportActivity() {
        // 服务存活即视为手机端在线（idle=0），主机端不会误判离开自动暂停
        Thread {
            try {
                httpPost("http://${SyncState.host}:${MainActivity.PORT}/activity",
                         """{"idle":0}""")
            } catch (e: Exception) {
                // 上报失败不影响主流程
            }
        }.start()
    }
}

internal fun httpGet(url: String, timeout: Int = 4000): String {
    val conn = URL(url).openConnection() as HttpURLConnection
    conn.connectTimeout = timeout
    conn.readTimeout = timeout
    try {
        return conn.inputStream.bufferedReader().readText()
    } finally {
        conn.disconnect()
    }
}

internal fun httpPost(url: String, body: String): String {
    val conn = URL(url).openConnection() as HttpURLConnection
    conn.requestMethod = "POST"
    conn.connectTimeout = 4000
    conn.readTimeout = 4000
    conn.doOutput = true
    conn.setRequestProperty("Content-Type", "application/json")
    try {
        conn.outputStream.use { it.write(body.toByteArray(Charsets.UTF_8)) }
        return conn.inputStream.bufferedReader().readText()
    } finally {
        conn.disconnect()
    }
}
