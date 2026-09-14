# -*- coding: utf-8 -*-
"""桌面悬浮计时器（仿 QQ 音乐歌词条）。

功能：
- 无边框半透明悬浮窗，置顶显示，可拖动，位置自动记忆
- 左键点击分类：开始 / 暂停；右键菜单：全部功能入口
- 每日目标 + 进度条；今日 / 累计显示切换
- 番茄钟模式（25 分钟专注 + 5 分钟休息自动循环）
- 连续超时提醒（工作/游戏满 1 小时弹提醒）
- 全局快捷键：Ctrl+Alt+1/2/3 切换分类，Ctrl+Alt+L 鼠标穿透锁定/解锁
- 键鼠空闲 5 分钟自动暂停计时
- 空闲时轮播名言
- 历史统计（按天/周）、CSV 报表、带图表的 HTML 周报
- 开机自启（写入 HKCU 注册表）

数据保存在脚本同目录的 time_data.json。
"""

import ctypes
import html
import json
import os
import queue
import socket
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import messagebox, filedialog, simpledialog, ttk
from datetime import datetime, timedelta
from collections import defaultdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SYNC_PORT = 8765
SYNC_POLL_MS = 3000

CATEGORIES = ["工作", "游戏", "学习"]

DATA_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "time_data.json")

APP_NAME = "DesktopTimeTracker"

VERSION = "1.0.0"
RAW_URL = ("https://raw.githubusercontent.com/rickylxw/SelfDiciplineClock/"
           "main/time_tracker.py")

COLORS = {
    "工作": "#4CAF50",
    "游戏": "#F44336",
    "学习": "#2196F3",
}
BG = "#1B1B1F"
FG_DIM = "#BBBBBB"

TICK_MS = 1000

# 可调常量（测试时可改小）
POMODORO_FOCUS = 25 * 60
POMODORO_BREAK = 5 * 60
IDLE_PAUSE_SECONDS = 5 * 60
CONTINUOUS_ALERT_SECONDS = 60 * 60

QUOTES = [
    "业精于勤，荒于嬉；行成于思，毁于随。",
    "不积跬步，无以至千里。",
    "时间就像海绵里的水，挤一挤总会有的。",
    "宝剑锋从磨砺出，梅花香自苦寒来。",
    "少壮不努力，老大徒伤悲。",
    "路漫漫其修远兮，吾将上下而求索。",
    "千里之行，始于足下。",
    "逝者如斯夫，不舍昼夜。",
    "天才就是百分之一的灵感加百分之九十九的汗水。",
    "休息是为了走更长的路。",
    "今日事，今日毕。",
    "学而不思则罔，思而不学则殆。",
]

user32 = ctypes.windll.user32


# ---------------- 数据 ----------------

def load_data():
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        data = {}

    # 旧版格式迁移：{"工作": 秒, ...} 单文件累计 → 按天记录
    if any(c in data for c in CATEGORIES):
        legacy = {"工作": data.pop("工作", 0),
                  "游戏": data.pop("游戏", 0),
                  "学习": data.pop("学习", 0)}
        daily = data.setdefault("daily", {})
        day = daily.setdefault(today_str(), {c: 0 for c in CATEGORIES})
        for c in CATEGORIES:
            day[c] = max(day.get(c, 0), legacy[c])
        try:  # 迁移前先把原文件备份一份
            import shutil
            shutil.copy2(DATA_FILE, DATA_FILE + ".migrated.bak")
        except OSError:
            pass

    daily = data.get("daily", {})
    for day in daily.values():
        for c in CATEGORIES:
            day.setdefault(c, 0)
    settings = data.get("settings", {})
    settings.setdefault("goals", {c: 0 for c in CATEGORIES})  # 秒，0 = 无目标
    settings.setdefault("pomodoro", False)
    settings.setdefault("idle_pause", True)
    settings.setdefault("host_addr", "")      # 客户端模式连接的主机 IP
    settings.setdefault("sync_host", False)   # 是否作为主机共享
    settings.setdefault("font_size", 13)      # 悬浮条字号（Ctrl+滚轮调节）
    data["daily"] = daily
    data["settings"] = settings
    return data


def merge_daily(dst, src):
    """按 日期+分类 取较大值合并，返回 dst。"""
    for d, day in src.items():
        tgt = dst.setdefault(d, {c: 0 for c in CATEGORIES})
        for c in CATEGORIES:
            v = day.get(c, 0)
            if v > tgt.get(c, 0):
                tgt[c] = v
    return dst


def lan_ip():
    """取本机局域网 IP（UDP connect 不实际发包）。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


# ---------------- 自动更新 ----------------

def parse_version(text):
    for line in text.splitlines():
        if line.startswith("VERSION = "):
            return line.split("= ", 1)[1].strip().strip('"')
    return None


def version_gt(a, b):
    def parts(v):
        return tuple(int(x) for x in v.split("."))
    try:
        return parts(a) > parts(b)
    except (ValueError, TypeError):
        return False


def fetch_latest(timeout=5):
    """返回 (版本号, 最新脚本全文)；无网络或结构不对时抛异常。"""
    import urllib.request
    with urllib.request.urlopen(RAW_URL, timeout=timeout) as resp:
        text = resp.read().decode("utf-8")
    ver = parse_version(text)
    if not ver:
        raise ValueError("远端文件缺少版本号")
    return ver, text


def apply_update(text):
    """备份当前脚本并替换为新版，返回是否成功。"""
    path = os.path.abspath(__file__)
    try:
        compile(text, path, "exec")  # 语法校验，防止写入残缺脚本
        with open(path, "r", encoding="utf-8") as f:
            old = f.read()
        with open(path + ".update.bak", "w", encoding="utf-8") as f:
            f.write(old)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        return True
    except (OSError, SyntaxError):
        return False


def restart_app():
    subprocess.Popen([sys.executable, os.path.abspath(__file__)])


class SyncServer:
    """主机端内置 HTTP 服务：/state 查询、/merge 数据合并、/control 远程操作。

    处理线程不直接碰 Tk，控制与合并请求放入队列由主循环消费。
    """

    def __init__(self, app):
        self.app = app
        self.cmd_q = queue.Queue()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _send(self, obj):
                body = json.dumps(obj).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if self.path == "/state":
                    a = outer.app
                    self._send({"daily": a.data["daily"],
                                "running_cat": a.running_cat,
                                "elapsed": a.elapsed()})
                else:
                    self.send_error(404)

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                try:
                    payload = json.loads(self.rfile.read(n) or b"{}")
                except (json.JSONDecodeError, ValueError):
                    self.send_error(400)
                    return
                if self.path == "/merge":
                    outer.cmd_q.put(("merge", payload.get("daily", {})))
                    self._send({"ok": True})
                elif self.path == "/control":
                    outer.cmd_q.put(("control", payload))
                    self._send({"ok": True})
                else:
                    self.send_error(404)

        self.httpd = ThreadingHTTPServer(("0.0.0.0", SYNC_PORT), Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever,
                                       daemon=True)
        self.thread.start()

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def today_str():
    return datetime.now().strftime("%Y-%m-%d")


def week_range(date_str):
    """返回该日期所在周（周一起始）的标签。"""
    d = datetime.strptime(date_str, "%Y-%m-%d").date()
    monday = d - timedelta(days=d.weekday())
    sunday = monday + timedelta(days=6)
    return f"{monday.strftime('%m-%d')} ~ {sunday.strftime('%m-%d')}"


# ---------------- Windows 系统接口 ----------------

class LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]


def idle_seconds():
    """键鼠空闲秒数。"""
    info = LASTINPUTINFO()
    info.cbSize = ctypes.sizeof(LASTINPUTINFO)
    if not user32.GetLastInputInfo(ctypes.byref(info)):
        return 0
    return (ctypes.windll.kernel32.GetTickCount() - info.dwTime) / 1000.0


def key_down(vk):
    return bool(user32.GetAsyncKeyState(vk) & 0x8000)


def top_hwnd(widget):
    GA_ROOT = 2
    return user32.GetAncestor(widget.winfo_id(), GA_ROOT)


def set_clickthrough(widget, enable):
    """设置鼠标穿透（锁定后点击直达下层应用）。"""
    GWL_EXSTYLE = -20
    WS_EX_LAYERED = 0x00080000
    WS_EX_TRANSPARENT = 0x00000020
    hwnd = top_hwnd(widget)
    style = user32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE)
    if enable:
        style |= WS_EX_LAYERED | WS_EX_TRANSPARENT
    else:
        style &= ~WS_EX_TRANSPARENT
    user32.SetWindowLongPtrW(hwnd, GWL_EXSTYLE, style)


# ---------------- 开机自启 ----------------

def autostart_enabled():
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Run") as k:
            winreg.QueryValueEx(k, APP_NAME)
            return True
    except OSError:
        return False


def autostart_command():
    return f'"{sys.executable}" "{os.path.abspath(__file__)}"'


def set_autostart(enable):
    import winreg
    try:
        with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Run",
                0, winreg.KEY_SET_VALUE) as key:
            if enable:
                winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ,
                                  autostart_command())
            else:
                try:
                    winreg.DeleteValue(key, APP_NAME)
                except FileNotFoundError:
                    pass
    except OSError as e:
        messagebox.showerror("开机自启", f"设置失败：{e}")
        return False
    return True


class TimeTracker(tk.Tk):
    def __init__(self):
        super().__init__()
        self.data = load_data()
        self.settings = self.data["settings"]
        self._last_mtime = self.file_mtime()

        self.overrideredirect(True)      # 无边框
        self.attributes("-topmost", True)
        self.attributes("-alpha", 0.82)  # 半透明
        self.configure(bg=BG)
        self.position_window()

        # 运行状态
        self.running_cat = None
        self.start_ts = None
        self.show_mode = "cumulative"    # cumulative | today
        self.locked = False
        self.idle_paused = False
        self.last_remind_ts = 0.0

        # 番茄钟状态：None / "focus" / "break"
        self.pom_state = None
        self.pom_end_ts = 0.0

        # 局域网同步
        self.sync_server = None
        self.mirror = None          # 客户端镜像：{"cat", "elapsed", "ts"}
        self.sync_warned = False
        self.pending_ver = None
        if self.settings.get("sync_host"):
            self.start_host()

        self.quote = QUOTES[0]
        self.build_ui()
        self.quote_loop()
        self.update_loop()
        self.hotkey_loop()
        self.sync_loop()
        self.after(15000, self.silent_update_check)

    def position_window(self):
        w, h = 480, 62
        geo = self.data.get("geometry")
        if geo:
            self.geometry(f"+{geo[0]}+{geo[1]}")
        else:
            x = (self.winfo_screenwidth() - w) // 2
            self.geometry(f"+{x}+60")
        self.update_idletasks()

    # ---------------- 悬浮条界面 ----------------

    def build_ui(self):
        bar = tk.Frame(self, bg=BG)
        bar.pack(fill="both", expand=True)

        # 顶部两行共用一个 grid，保证分类文字与进度条严格同列同宽
        grid = tk.Frame(bar, bg=BG)
        grid.pack(fill="x", padx=8, pady=(6, 0))
        grid.columnconfigure(0, weight=0)   # 模式按钮列
        for col in range(1, 4):
            grid.columnconfigure(col, weight=1, uniform="cat")

        self.mode_btn = tk.Label(grid, text="累计", font=("微软雅黑", 10),
                                 bg=BG, fg="#888888", cursor="hand2")
        self.mode_btn.grid(row=0, column=0, sticky="w", padx=(2, 6))
        self.mode_btn.bind("<Button-1>", self.toggle_show_mode)

        self.labels = {}
        for col, cat in enumerate(CATEGORIES, start=1):
            lbl = tk.Label(grid, font=("微软雅黑", 13), bg=BG, fg=FG_DIM)
            lbl.grid(row=0, column=col, sticky="ew")
            # 松开时若未拖动则视为点击切换该分类
            lbl.bind("<ButtonRelease-1>",
                     lambda e, c=cat: self.click_or_drag_end(c))
            self.labels[cat] = lbl

        # 拖动绑定在根窗口：通过 bindtags 覆盖所有子控件；
        # drag_start 里 grab 独占指针，窗口移动后事件不断流
        self.bind("<Button-1>", self.drag_start, add="+")
        self.bind("<B1-Motion>", self.drag_move, add="+")
        self.bind("<ButtonRelease-1>", self.drag_release, add="+")

        # 进度条行：与分类文字同一 grid 同一列
        self.bars = {}
        for col, cat in enumerate(CATEGORIES, start=1):
            canvas = tk.Canvas(grid, height=4, bg="#333338",
                               highlightthickness=0)
            canvas.grid(row=1, column=col, sticky="ew", pady=(2, 0))
            canvas.bind("<Configure>",
                        lambda e, cv=canvas: self.redraw_bar(cv))
            self.bars[cat] = canvas

        # 底部：状态 / 名言行
        self.status = tk.Label(bar, text="", font=("微软雅黑", 9),
                               bg=BG, fg="#888888")
        self.status.pack(fill="x", pady=(1, 4))

        # 右键菜单
        self.menu = tk.Menu(self, tearoff=0)
        self.menu.add_command(label="历史统计", command=self.show_history)
        self.menu.add_command(label="导出 CSV 报表", command=self.export_csv)
        self.menu.add_command(label="导出 HTML 周报", command=self.export_html)
        self.menu.add_command(label="每日目标设置…", command=self.edit_goals)
        self.menu.add_separator()
        self.pom_var = tk.BooleanVar(value=self.settings["pomodoro"])
        self.menu.add_checkbutton(label="番茄钟模式（25 分钟专注）",
                                  variable=self.pom_var,
                                  command=self.toggle_pomodoro)
        self.idle_var = tk.BooleanVar(value=self.settings["idle_pause"])
        self.menu.add_checkbutton(label="离开 5 分钟自动暂停",
                                  variable=self.idle_var,
                                  command=self.toggle_idle_pause)
        self.lock_var = tk.BooleanVar(value=False)
        self.menu.add_checkbutton(label="鼠标穿透（Ctrl+Alt+L 解锁）",
                                  variable=self.lock_var,
                                  command=self.toggle_lock)
        self.menu.add_separator()
        sync_menu = tk.Menu(self.menu, tearoff=0)
        self.menu.add_cascade(label="局域网同步", menu=sync_menu)
        self.host_var = tk.BooleanVar(value=self.settings.get("sync_host", False))
        sync_menu.add_checkbutton(label="作为主机共享（端口 8765）",
                                  variable=self.host_var, command=self.toggle_host)
        sync_menu.add_command(label="连接主机…", command=self.connect_host)
        sync_menu.add_command(label="立即同步", command=self.sync_now)
        self.menu.add_command(label="大小：按住 Ctrl 滚动滚轮调节",
                              state="disabled")
        self.menu.add_separator()
        self.autostart_item = tk.BooleanVar(value=autostart_enabled())
        self.menu.add_checkbutton(label="开机自启",
                                  variable=self.autostart_item,
                                  command=self.toggle_autostart)
        self.menu.add_command(label="检查更新", command=self.check_update)
        self.menu.add_command(label="退出", command=self.on_close)
        for w in (bar, self.status, self.mode_btn):
            w.bind("<Button-3>", self.popup_menu)
        for lbl in self.labels.values():
            lbl.bind("<Button-3>", self.popup_menu)

        # Ctrl+滚轮调节悬浮条大小
        self.bind("<Control-MouseWheel>", self.on_zoom)
        self.apply_ui_size()

    def apply_ui_size(self):
        """按 settings 中的字号重建悬浮条尺寸与字体。"""
        size = self.settings.get("font_size", 13)
        for cat, lbl in self.labels.items():
            lbl.config(font=("微软雅黑", size))
        self.mode_btn.config(font=("微软雅黑", size - 3))
        self.status.config(font=("微软雅黑", size - 4))
        bar_h = max(3, size // 4)
        for canvas in self.bars.values():
            canvas.config(height=bar_h)
        w = max(360, size * 36)
        h = size * 3 + bar_h + 18
        x, y = self.winfo_x(), self.winfo_y()
        self.geometry(f"{w}x{h}+{x}+{y}")
        self.update_idletasks()
        self.refresh()

    def on_zoom(self, event):
        size = self.settings.get("font_size", 13)
        step = 1 if event.delta > 0 else -1
        new_size = max(9, min(26, size + step))
        if new_size != size:
            self.settings["font_size"] = new_size
            self.apply_ui_size()
            self.save()

    def popup_menu(self, event):
        self.menu.tk_popup(event.x_root, event.y_root)

    # ---------------- 拖动 / 点击 ----------------

    def drag_start(self, event):
        if self.locked:
            return
        self.drag_moved = False
        self.drag_off = (event.x_root - self.winfo_x(),
                         event.y_root - self.winfo_y())
        try:
            self.grab_set()  # 窗口移动后 motion 事件仍归本窗口
        except tk.TclError:
            pass

    def drag_move(self, event):
        if not hasattr(self, "drag_off"):
            return
        self.drag_moved = True
        self.geometry(f"+{event.x_root - self.drag_off[0]}"
                      f"+{event.y_root - self.drag_off[1]}")

    def drag_release(self, event):
        try:
            self.grab_release()
        except tk.TclError:
            pass
        if getattr(self, "drag_moved", False):
            self.data["geometry"] = [self.winfo_x(), self.winfo_y()]
            self.save()

    def click_or_drag_end(self, cat):
        if self.locked or self.drag_moved:
            return
        if self.settings.get("host_addr") and not self.sync_server:
            # 客户端：操作转发给主机执行，保持全网单一计时源
            self.remote_toggle(cat)
            return
        self.toggle(cat)
        self.data["geometry"] = [self.winfo_x(), self.winfo_y()]
        self.save()

    def toggle_show_mode(self, event=None):
        self.show_mode = "today" if self.show_mode == "cumulative" else "cumulative"

    # ---------------- 计时逻辑 ----------------

    def display_seconds(self, cat):
        """当前显示的时长：今日或累计，含本机与镜像主机的进行中时间。"""
        if self.show_mode == "today":
            base = self.data["daily"].get(today_str(), {}).get(cat, 0)
        else:
            base = self.total(cat)
        return base + self.live_for(cat)

    def live_for(self, cat):
        """正在进行的秒数：本机会话优先，否则显示主机镜像会话。"""
        if self.running_cat == cat:
            return self.elapsed()
        if (self.mirror and self.mirror["cat"] == cat
                and not self.running_cat):
            return self.mirror["elapsed"] + \
                (datetime.now().timestamp() - self.mirror["ts"])
        return 0

    def total(self, cat):
        return sum(day.get(cat, 0) for day in self.data["daily"].values())

    def elapsed(self):
        if self.running_cat and self.start_ts:
            return datetime.now().timestamp() - self.start_ts
        return 0

    def toggle(self, cat):
        if self.running_cat == cat:
            self.stop_and_save()
            return
        if self.running_cat:
            self.stop_and_save()
        self.running_cat = cat
        self.start_ts = datetime.now().timestamp()
        self.idle_paused = False
        self.last_remind_ts = 0.0
        if self.pom_var.get() and self.pom_state != "focus":
            self.pom_state = "focus"
            self.pom_end_ts = self.start_ts + POMODORO_FOCUS
        self.refresh()

    def stop_and_save(self):
        if self.running_cat and self.start_ts:
            day = self.data["daily"].setdefault(today_str(),
                                                {c: 0 for c in CATEGORIES})
            day[self.running_cat] += round(self.elapsed(), 1)
        self.running_cat = None
        self.start_ts = None
        self.pom_state = None
        self.save()
        self.refresh()

    # ---------------- 主循环 ----------------

    def update_loop(self):
        self.check_external_change()
        self.process_sync_queue()
        self.check_idle()
        self.check_pomodoro()
        self.check_continuous_alert()
        self.refresh()
        self.after(TICK_MS, self.update_loop)

    def check_idle(self):
        if not self.idle_var.get():
            return
        if self.running_cat and idle_seconds() > IDLE_PAUSE_SECONDS:
            self.stop_and_save()
            self.idle_paused = True
            self.toast("离开检测", "检测到离开，计时已自动暂停。")

    def check_pomodoro(self):
        if not self.pom_var.get():
            return
        now = datetime.now().timestamp()
        if self.pom_state == "focus" and now >= self.pom_end_ts:
            self.stop_and_save()
            self.pom_state = "break"
            self.pom_end_ts = now + POMODORO_BREAK
            self.toast("番茄钟", "专注 25 分钟完成，休息 5 分钟！")
        elif self.pom_state == "break" and now >= self.pom_end_ts:
            self.pom_state = None
            self.toast("番茄钟", "休息结束，开始新的专注！")
            self.toggle(CATEGORIES[0])  # 自动开始下一轮工作专注

    def check_continuous_alert(self):
        if not self.running_cat:
            return
        elapsed = self.elapsed()
        if (elapsed >= CONTINUOUS_ALERT_SECONDS
                and elapsed - self.last_remind_ts >= CONTINUOUS_ALERT_SECONDS):
            self.last_remind_ts = elapsed
            cat = self.running_cat
            if cat == "工作":
                self.toast("健康提醒", f"已连续工作 1 小时，起来活动一下吧！")
            elif cat == "游戏":
                self.toast("时长提醒", "游戏已连续 1 小时，注意休息哦～")
            else:
                self.toast("时长提醒", f"「{cat}」已连续 1 小时。")

    def refresh(self):
        for cat in CATEGORIES:
            total = self.display_seconds(cat)
            if self.running_cat == cat:
                fg = COLORS[cat]
                text = f"● {cat} {self.fmt(total)}"
            elif self.mirror and self.mirror["cat"] == cat \
                    and not self.running_cat:
                fg = COLORS[cat]
                text = f"◐ {cat} {self.fmt(total)}"
            else:
                fg = FG_DIM
                text = f"{cat} {self.fmt(total)}"
            self.labels[cat].config(text=text, fg=fg)
            self.draw_progress(cat)
        self.mode_btn.config(text="今日" if self.show_mode == "today" else "累计")
        self.refresh_status()

    def redraw_bar(self, canvas):
        """窗口/列宽变化时立即重绘对应进度条。"""
        for cat, cv in self.bars.items():
            if cv is canvas:
                self.draw_progress(cat)
                return

    def draw_progress(self, cat):
        canvas = self.bars[cat]
        width = canvas.winfo_width()
        height = canvas.winfo_height()
        if width <= 1:
            return
        goal = self.settings["goals"].get(cat, 0)
        canvas.delete("all")
        if not goal:
            return
        done = self.data["daily"].get(today_str(), {}).get(cat, 0)
        if cat == self.running_cat:
            done += self.elapsed()
        pct = max(0.0, min(1.0, done / goal))
        canvas.create_rectangle(0, 0, max(2, int(width * pct)), height,
                                fill=COLORS[cat], width=0)

    def refresh_status(self):
        now = datetime.now().timestamp()
        sync_tag = ""
        if self.sync_server:
            sync_tag = f"（主机 {lan_ip()}:{SYNC_PORT}）"
        elif self.settings.get("host_addr"):
            sync_tag = f"（同步自 {self.settings['host_addr']}）"
        if self.locked:
            self.status.config(text="已锁定：鼠标穿透，按 Ctrl+Alt+L 解锁",
                               fg="#FFC107")
        elif self.pom_state == "focus" and self.running_cat:
            remain = int(self.pom_end_ts - now)
            self.status.config(
                text=f"🍅 专注中，剩余 {self.fmt(max(0, remain))}{sync_tag}",
                fg=COLORS["工作"])
        elif self.pom_state == "break":
            remain = int(self.pom_end_ts - now)
            self.status.config(
                text=f"☕ 休息中，剩余 {self.fmt(max(0, remain))}{sync_tag}",
                fg="#888888")
        elif self.mirror and not self.running_cat:
            self.status.config(
                text=f"主机正在计时：{self.mirror['cat']}{sync_tag}",
                fg=COLORS.get(self.mirror["cat"], "#888888"))
        elif self.idle_paused:
            self.status.config(text="已自动暂停（检测到离开），点击分类继续"
                                    + sync_tag, fg="#FFC107")
        else:
            self.status.config(text=self.quote + sync_tag if sync_tag
                               else self.quote, fg="#777777")

    def quote_loop(self):
        self.quote = QUOTES[int(datetime.now().timestamp()) // 60 % len(QUOTES)]

    # ---------------- 快捷键 ----------------

    def hotkey_loop(self):
        VK = {"1": 0x31, "2": 0x32, "3": 0x33, "L": 0x4C}
        if key_down(0x11) and key_down(0x12):  # Ctrl + Alt
            for digit, cat in zip("123", CATEGORIES):
                if key_down(VK[digit]):
                    if not getattr(self, f"_hk{digit}", False):
                        self.toggle(cat)
                    setattr(self, f"_hk{digit}", True)
                else:
                    setattr(self, f"_hk{digit}", False)
            if key_down(VK["L"]):
                if not getattr(self, "_hkL", False):
                    self.toggle_lock()
                self._hkL = True
            else:
                self._hkL = False
        else:
            for k in ("1", "2", "3", "L"):
                setattr(self, f"_hk{k}", False)
        self.after(150, self.hotkey_loop)

    # ---------------- 锁定（鼠标穿透） ----------------

    def toggle_lock(self):
        self.locked = not self.locked
        self.lock_var.set(self.locked)
        set_clickthrough(self, self.locked)
        self.refresh()

    # ---------------- 番茄钟 / 设置开关 ----------------

    def toggle_pomodoro(self):
        self.settings["pomodoro"] = self.pom_var.get()
        self.save()
        if self.pom_var.get():
            self.toggle(CATEGORIES[0]) if not self.running_cat else None
            if not self.pom_state and self.running_cat:
                self.pom_state = "focus"
                self.pom_end_ts = self.start_ts + POMODORO_FOCUS
            self.toast("番茄钟", "番茄钟已开启：25 分钟专注 + 5 分钟休息。")
        else:
            self.pom_state = None
            self.toast("番茄钟", "番茄钟已关闭。")

    def toggle_idle_pause(self):
        self.settings["idle_pause"] = self.idle_var.get()
        self.save()

    def toggle_autostart(self):
        enable = self.autostart_item.get()
        if set_autostart(enable):
            if enable != autostart_enabled():
                self.autostart_item.set(not enable)  # 失败则回退显示

    def edit_goals(self):
        win = tk.Toplevel(self)
        win.title("每日目标设置")
        win.attributes("-topmost", True)
        win.resizable(False, False)
        entries = {}
        for i, cat in enumerate(CATEGORIES):
            tk.Label(win, text=f"{cat} 每日目标（小时，0 = 不设目标）",
                     font=("微软雅黑", 10)).grid(row=i, column=0,
                                                 padx=10, pady=6, sticky="w")
            var = tk.StringVar()
            hours = self.settings["goals"][cat] / 3600
            var.set(f"{hours:g}" if hours else "0")
            e = tk.Entry(win, textvariable=var, width=8)
            e.grid(row=i, column=1, padx=10, pady=6)
            entries[cat] = var

        def apply():
            try:
                for cat, var in entries.items():
                    self.settings["goals"][cat] = max(0.0, float(var.get())) * 3600
            except ValueError:
                messagebox.showerror("每日目标", "请输入有效数字。", parent=win)
                return
            self.save()
            self.refresh()
            win.destroy()

        tk.Button(win, text="保存", command=apply, width=10).grid(
            row=len(CATEGORIES), column=0, columnspan=2, pady=8)

    # ---------------- 提醒气泡 ----------------

    def toast(self, title, message, duration_ms=8000):
        win = tk.Toplevel(self)
        win.overrideredirect(True)
        win.attributes("-topmost", True)
        win.attributes("-alpha", 0.92)
        win.configure(bg="#2A2A30")
        pad = tk.Frame(win, bg="#2A2A30", padx=14, pady=10)
        pad.pack(fill="both", expand=True)
        tk.Label(pad, text=title, font=("微软雅黑", 10, "bold"),
                 bg="#2A2A30", fg="#FFC107").pack(anchor="w")
        tk.Label(pad, text=message, font=("微软雅黑", 10),
                 bg="#2A2A30", fg="#EEEEEE", wraplength=260,
                 justify="left").pack(anchor="w", pady=(2, 0))
        win.update_idletasks()
        w, h = win.winfo_reqwidth(), win.winfo_reqheight()
        x = self.winfo_screenwidth() - w - 30
        y = self.winfo_screenheight() - h - 60
        win.geometry(f"+{x}+{y}")
        win.after(duration_ms, win.destroy)

    # ---------------- 统计窗口 / 导出 ----------------

    def week_map(self):
        weeks = defaultdict(lambda: {c: 0 for c in CATEGORIES})
        for date_str, day in self.data["daily"].items():
            label = week_range(date_str)
            for c in CATEGORIES:
                weeks[label][c] += day.get(c, 0)
        return weeks

    def show_history(self):
        win = tk.Toplevel(self)
        win.title("历史统计")
        win.attributes("-topmost", True)

        nb = ttk.Notebook(win)
        nb.pack(fill="both", expand=True, padx=8, pady=8)

        day_frame = ttk.Frame(nb)
        nb.add(day_frame, text=" 按天 ")
        day_tree = ttk.Treeview(day_frame, columns=("date", *CATEGORIES, "sum"),
                                show="headings", height=12)
        day_tree.heading("date", text="日期")
        day_tree.column("date", width=100, anchor="center")
        for c in CATEGORIES:
            day_tree.heading(c, text=c)
            day_tree.column(c, width=90, anchor="center")
        day_tree.heading("sum", text="合计")
        day_tree.column("sum", width=90, anchor="center")
        day_tree.pack(fill="both", expand=True)

        days = sorted(self.data["daily"].items(), reverse=True)
        for date_str, day in days[-60:]:
            total_day = sum(day.get(c, 0) for c in CATEGORIES)
            day_tree.insert("", "end",
                            values=[date_str] +
                            [self.fmt(day.get(c, 0)) for c in CATEGORIES] +
                            [self.fmt(total_day)])
        if not days:
            day_tree.insert("", "end", values=("暂无记录", "", "", "", ""))

        week_frame = ttk.Frame(nb)
        nb.add(week_frame, text=" 按周 ")
        week_tree = ttk.Treeview(week_frame, columns=("week", *CATEGORIES, "sum"),
                                 show="headings", height=12)
        week_tree.heading("week", text="周（周一起始）")
        week_tree.column("week", width=140, anchor="center")
        for c in CATEGORIES:
            week_tree.heading(c, text=c)
            week_tree.column(c, width=90, anchor="center")
        week_tree.heading("sum", text="合计")
        week_tree.column("sum", width=90, anchor="center")
        week_tree.pack(fill="both", expand=True)

        weeks = self.week_map()
        for label in sorted(weeks, reverse=True):
            w = weeks[label]
            total_w = sum(w[c] for c in CATEGORIES)
            week_tree.insert("", "end",
                             values=[label] +
                             [self.fmt(w[c]) for c in CATEGORIES] +
                             [self.fmt(total_w)])
        if not weeks:
            week_tree.insert("", "end", values=("暂无记录", "", "", "", ""))

    def export_csv(self):
        if not self.data["daily"]:
            messagebox.showinfo("导出报表", "还没有任何记录可导出。")
            return
        path = filedialog.asksaveasfilename(
            parent=self, title="导出 CSV 报表",
            initialfile=f"时长报表_{today_str()}.csv",
            defaultextension=".csv",
            filetypes=[("CSV 文件", "*.csv")])
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8-sig", newline="") as f:
                f.write("按天统计\n")
                f.write("日期," + ",".join(CATEGORIES) + ",合计\n")
                for date_str, day in sorted(self.data["daily"].items()):
                    t = sum(day.get(c, 0) for c in CATEGORIES)
                    f.write(date_str + "," +
                            ",".join(self.fmt(day.get(c, 0)) for c in CATEGORIES) +
                            "," + self.fmt(t) + "\n")
                f.write("\n按周统计\n")
                f.write("周（周一起始）," + ",".join(CATEGORIES) + ",合计\n")
                for label in sorted(self.week_map()):
                    w = self.week_map()[label]
                    t = sum(w[c] for c in CATEGORIES)
                    f.write(label + "," +
                            ",".join(self.fmt(w[c]) for c in CATEGORIES) +
                            "," + self.fmt(t) + "\n")
        except OSError as e:
            messagebox.showerror("导出报表", f"导出失败：{e}")
            return
        messagebox.showinfo("导出报表", f"已导出到：\n{path}")

    def export_html(self):
        if not self.data["daily"]:
            messagebox.showinfo("导出周报", "还没有任何记录可导出。")
            return
        path = filedialog.asksaveasfilename(
            parent=self, title="导出 HTML 周报",
            initialfile=f"时长周报_{today_str()}.html",
            defaultextension=".html",
            filetypes=[("HTML 文件", "*.html")])
        if not path:
            return
        try:
            self.write_html(path)
        except OSError as e:
            messagebox.showerror("导出周报", f"导出失败：{e}")
            return
        messagebox.showinfo("导出周报", f"已导出到：\n{path}")

    def write_html(self, path):
        days = sorted(self.data["daily"].items())[-14:]  # 最近 14 天
        max_val = max((sum(d.get(c, 0) for c in CATEGORIES) for _, d in days),
                      default=0) or 1

        # SVG 柱状图：三分类分组柱
        chart_w, chart_h, group_gap = 720, 240, 18
        group_w = (chart_w - group_gap * (len(days) + 1)) / max(1, len(days))
        bar_w = group_w / (len(CATEGORIES) + 1)
        rects, labels_txt, legend = [], [], []
        for gi, (date_str, day) in enumerate(days):
            gx = group_gap + gi * group_w
            labels_txt.append(
                f'<text x="{gx + group_w / 2}" y="{chart_h - 4}" text-anchor="middle" '
                f'font-size="10" fill="#666">{html.escape(date_str[5:])}</text>')
            for bi, cat in enumerate(CATEGORIES):
                v = day.get(cat, 0)
                bh = (v / max_val) * (chart_h - 30)
                x = gx + bi * bar_w
                rects.append(
                    f'<rect x="{x:.1f}" y="{chart_h - 20 - bh:.1f}" '
                    f'width="{bar_w - 2:.1f}" height="{bh:.1f}" '
                    f'fill="{COLORS[cat]}"><title>{cat} {self.fmt(v)}</title></rect>')
        for bi, cat in enumerate(CATEGORIES):
            legend.append(
                f'<rect x="{20 + bi * 90}" y="8" width="12" height="12" '
                f'fill="{COLORS[cat]}"/>'
                f'<text x="{36 + bi * 90}" y="18" font-size="12" fill="#444">{cat}</text>')

        week_rows = []
        for label in sorted(self.week_map(), reverse=True):
            w = self.week_map()[label]
            t = sum(w[c] for c in CATEGORIES)
            week_rows.append(
                "<tr>" + f"<td>{label}</td>" +
                "".join(f"<td>{self.fmt(w[c])}</td>" for c in CATEGORIES) +
                f"<td><b>{self.fmt(t)}</b></td></tr>")

        day_rows = []
        for date_str, day in reversed(days):
            t = sum(day.get(c, 0) for c in CATEGORIES)
            day_rows.append(
                "<tr>" + f"<td>{date_str}</td>" +
                "".join(f"<td>{self.fmt(day.get(c, 0))}</td>" for c in CATEGORIES) +
                f"<td><b>{self.fmt(t)}</b></td></tr>")

        doc = f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>时长周报 {today_str()}</title>
<style>
body {{ font-family: "Microsoft YaHei", sans-serif; margin: 40px auto;
       max-width: 800px; color: #333; }}
h1 {{ font-size: 22px; }} h2 {{ font-size: 16px; margin-top: 32px; }}
table {{ border-collapse: collapse; width: 100%; font-size: 13px; }}
th, td {{ border: 1px solid #ddd; padding: 6px 10px; text-align: center; }}
th {{ background: #f5f5f5; }}
svg {{ background: #fafafa; border: 1px solid #eee; }}
</style></head><body>
<h1>时长统计周报</h1>
<p>生成时间：{datetime.now().strftime("%Y-%m-%d %H:%M")}（含最近 14 天明细）</p>
<h2>每日时长（小时）</h2>
<svg width="{chart_w}" height="{chart_h}" xmlns="http://www.w3.org/2000/svg">
{''.join(legend)}
{chr(10).join(rects)}
{chr(10).join(labels_txt)}
</svg>
<h2>按天统计</h2>
<table><tr><th>日期</th>{''.join(f'<th>{c}</th>' for c in CATEGORIES)}<th>合计</th></tr>
{''.join(day_rows)}</table>
<h2>按周统计</h2>
<table><tr><th>周（周一起始）</th>{''.join(f'<th>{c}</th>' for c in CATEGORIES)}<th>合计</th></tr>
{''.join(week_rows)}</table>
</body></html>"""
        with open(path, "w", encoding="utf-8") as f:
            f.write(doc)

    # ---------------- 局域网同步 ----------------

    def start_host(self):
        if self.sync_server:
            return
        try:
            self.sync_server = SyncServer(self)
        except OSError as e:
            messagebox.showerror("局域网同步",
                                 f"主机服务启动失败（端口 {SYNC_PORT} 可能被占用）：\n{e}")
            self.host_var.set(False)
            self.settings["sync_host"] = False
            self.save()

    def stop_host(self):
        if self.sync_server:
            self.sync_server.stop()
            self.sync_server = None

    def toggle_host(self):
        self.settings["sync_host"] = self.host_var.get()
        self.save()
        if self.host_var.get():
            if self.settings.get("host_addr"):
                self.settings["host_addr"] = ""
                self.save()
                messagebox.showinfo("局域网同步", "已切换为主机模式。")
            self.start_host()
            self.toast("局域网同步",
                       f"主机模式已开启。\n本机 IP：{lan_ip()}"
                       f"\n客户端连接时填入此地址（端口 {SYNC_PORT}）。")
        else:
            self.stop_host()
            self.toast("局域网同步", "主机模式已关闭。")

    def connect_host(self):
        addr = simpledialog.askstring(
            "连接主机", "输入主机在局域网中的 IP 地址：",
            initialvalue=self.settings.get("host_addr", ""), parent=self)
        if not addr:
            return
        addr = addr.strip()
        if self.sync_server:
            messagebox.showwarning("连接主机", "本机是主机，不能连接其他主机。")
            return
        # 试连一次确认可达
        try:
            self.fetch_state(addr)
        except OSError:
            messagebox.showerror("连接主机",
                                 f"无法连接 {addr}:{SYNC_PORT}，"
                                 "请确认对方已开启「作为主机共享」且防火墙放行。")
            return
        self.settings["host_addr"] = addr
        self.save()
        self.mirror = None
        self.toast("局域网同步", f"已连接 {addr}，计时操作将由主机执行。")

    def fetch_state(self, addr=None):
        import urllib.request
        addr = addr or self.settings.get("host_addr")
        with urllib.request.urlopen(
                f"http://{addr}:{SYNC_PORT}/state", timeout=2) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def post(self, path, payload):
        import urllib.request
        addr = self.settings.get("host_addr")
        req = urllib.request.Request(
            f"http://{addr}:{SYNC_PORT}{path}",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=2) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def sync_loop(self):
        """客户端轮询：双向合并数据 + 镜像主机计时状态。"""
        if self.settings.get("host_addr"):
            try:
                st = self.fetch_state()
                self.sync_warned = False
                merge_daily(self.data["daily"], st.get("daily", {}))
                # 本地数据推回主机，形成双向合并
                self.post("/merge", {"daily": self.data["daily"]})
                if st.get("running_cat"):
                    self.mirror = {"cat": st["running_cat"],
                                   "elapsed": st.get("elapsed", 0),
                                   "ts": datetime.now().timestamp()}
                    if self.running_cat and self.running_cat != self.mirror["cat"] \
                            and not self.sync_warned:
                        self.sync_warned = True
                        self.toast("同步冲突",
                                   "主机正在计时，本机计时也在进行。\n"
                                   "合并时将保留较长的一段时间。")
                else:
                    self.mirror = None
            except (OSError, json.JSONDecodeError, KeyError):
                self.mirror = None
        self.after(SYNC_POLL_MS, self.sync_loop)

    def process_sync_queue(self):
        """主机端消费同步服务线程投递的任务。"""
        if not self.sync_server:
            return
        while True:
            try:
                kind, payload = self.sync_server.cmd_q.get_nowait()
            except queue.Empty:
                break
            if kind == "merge":
                merge_daily(self.data["daily"], payload)
                self.save()
                self.refresh()
            elif kind == "control" and payload.get("action") == "toggle":
                cat = payload.get("cat")
                if cat in CATEGORIES:
                    self.toggle(cat)

    def sync_now(self):
        """手动双向同步一次。"""
        addr = self.settings.get("host_addr")
        if addr and not self.sync_server:
            try:
                st = self.fetch_state(addr)
                merge_daily(self.data["daily"], st.get("daily", {}))
                self.post("/merge", {"daily": self.data["daily"]})
                self.save()
                self.refresh()
                messagebox.showinfo("立即同步", f"已与 {addr} 完成双向同步。")
            except OSError:
                messagebox.showerror("立即同步", f"无法连接 {addr}。")
        elif self.sync_server:
            messagebox.showinfo("立即同步",
                                "主机模式无需手动同步，客户端会自动推送合并。")
        else:
            messagebox.showinfo("立即同步", "请先通过「连接主机…」指定主机 IP。")

    def remote_toggle(self, cat):
        try:
            self.post("/control", {"action": "toggle", "cat": cat})
            self.toast("局域网同步", f"已通知主机切换「{cat}」。")
        except OSError:
            messagebox.showerror("局域网同步",
                                 "主机不可达，操作未执行。"
                                 "\n可用「立即同步」前先检查网络。")

    # ---------------- 自动更新 ----------------

    def silent_update_check(self):
        """启动 15 秒后静默检查一次，有新版仅提示，由用户决定。"""
        self._update_worker(lambda res: self.toast(
            "发现新版本",
            f"当前 v{VERSION}，最新 v{res[0]}。\n右键 →「检查更新」可立即升级。")
            if res else None)

    def check_update(self):
        """手动检查：有新版则下载替换并询问重启。"""
        def done(res):
            if res is None:
                messagebox.showinfo("检查更新", "检查失败：无法访问 GitHub，"
                                    "请检查网络。")
            elif not res:
                messagebox.showinfo("检查更新",
                                    f"已是最新版本 v{VERSION}。")
            elif res is True:
                if messagebox.askyesno(
                        "检查更新",
                        f"已更新到 v{self.pending_ver}，重启程序生效。现在重启吗？"):
                    restart_app()
                    self.on_close()
            else:
                ver, text = res
                self.pending_ver = ver
                if messagebox.askyesno(
                        "检查更新",
                        f"发现新版本 v{ver}（当前 v{VERSION}），现在更新吗？"):
                    if apply_update(text):
                        done(True)
                    else:
                        messagebox.showerror("检查更新", "更新失败：文件写入被占用。")
        self._update_worker(done)

    def _update_worker(self, on_done):
        """后台线程拉取远端版本；on_done 收到：
        None=检查失败 / False=已最新 / (版本, 全文)=有更新。"""
        def run():
            try:
                ver, text = fetch_latest()
                res = (ver, text) if version_gt(ver, VERSION) else False
            except (OSError, ValueError):
                res = None
            self.after(0, lambda: on_done(res))
        threading.Thread(target=run, daemon=True).start()

    # ---------------- 通用 ----------------

    def save(self):
        # 写入前保留上一份，写坏或误删时还能从 .bak 找回
        import shutil
        try:
            if os.path.exists(DATA_FILE):
                shutil.copy2(DATA_FILE, DATA_FILE + ".bak")
        except OSError:
            pass
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(self.data, f, ensure_ascii=False, indent=1)
        self._last_mtime = self.file_mtime()

    @staticmethod
    def file_mtime():
        try:
            return os.stat(DATA_FILE).st_mtime
        except OSError:
            return None

    def check_external_change(self):
        """热重载：外部修改 time_data.json 后自动加载（合并，不覆盖）。"""
        mtime = self.file_mtime()
        if mtime is None or mtime == getattr(self, "_last_mtime", None):
            return
        self._last_mtime = mtime
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                external = json.load(f)
        except (OSError, json.JSONDecodeError):
            return  # 文件可能正被写一半，下个 tick 再试
        changed = merge_daily(self.data["daily"],
                              external.get("daily", {}))
        ext_settings = external.get("settings", {})
        for key in ("goals", "host_addr", "sync_host", "font_size"):
            if key in ext_settings and ext_settings[key] != self.settings.get(key):
                self.settings[key] = ext_settings[key]
                changed = True
        if changed:
            if isinstance(self.settings.get("font_size"), int):
                self.apply_ui_size()
            self.refresh()
            self.toast("热重载", "检测到数据文件被外部修改，已重新加载。")

    @staticmethod
    def fmt(seconds):
        h, rem = divmod(int(seconds), 3600)
        m, s = divmod(rem, 60)
        return f"{h:02d}:{m:02d}:{s:02d}"

    def on_close(self):
        self.stop_and_save()
        self.stop_host()
        if not self.locked:
            self.data["geometry"] = [self.winfo_x(), self.winfo_y()]
            self.save()
        self.destroy()


if __name__ == "__main__":
    app = TimeTracker()
    app.mainloop()
