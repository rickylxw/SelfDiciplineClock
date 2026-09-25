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
import tkinter.font as tkfont
from tkinter import messagebox, filedialog, simpledialog, ttk
from datetime import datetime, timedelta
from collections import defaultdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SYNC_PORT = 8765
SYNC_POLL_MS = 3000

CATEGORIES = ["工作", "游戏", "学习"]

DATA_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "time_data.json")

APP_NAME = "DesktopTimeTracker"

VERSION = "1.8.0"
_REPO = "rickylxw/SelfDiciplineClock"
# 多源回退：raw.githubusercontent 国内经常超时，jsDelivr CDN 一般可达
UPDATE_URLS = [
    f"https://raw.githubusercontent.com/{_REPO}/main/time_tracker.py",
    f"https://cdn.jsdelivr.net/gh/{_REPO}@main/time_tracker.py",
    f"https://fastly.jsdelivr.net/gh/{_REPO}@main/time_tracker.py",
]

COLORS = {
    "工作": "#4CAF50",
    "游戏": "#F44336",
    "学习": "#2196F3",
}
BG = "#17171C"        # 悬浮条底色
PANEL = "#1E1E25"     # 待办区面板底色
TRACK = "#2A2A33"     # 进度条轨道
HOVER = "#22222B"     # 悬停高亮
FG_DIM = "#A9A9B2"    # 次级文字
TXT = "#EAEAEE"       # 主文字
ACCENT = "#FFC107"    # 强调色（气泡边条/标题）
FAMILY = "微软雅黑"   # 界面字体（皮肤可覆盖）
SKIN_IMAGE = ""       # 皮肤背景图（绝对路径，PNG/GIF）
SKIN_DIM = 0.0        # 背景图压暗强度 0~1
SKIN_IMAGE_H = None   # 背景区高度（像素，None=按图片比例）
SKIN_IMAGE_POS = "top"  # 图片位置：top 横幅 / left 左侧立绘
SKIN_IMAGE_W = 90      # 左侧立绘宽度
SKIN_SHOW_DOT = True   # 分类名前是否显示状态圆点
# 默认配色快照：切回「默认」皮肤时恢复
_DEFAULTS = {"BG": BG, "PANEL": PANEL, "TRACK": TRACK, "HOVER": HOVER,
             "FG_DIM": FG_DIM, "TXT": TXT, "ACCENT": ACCENT,
             "FAMILY": FAMILY, "SKIN_IMAGE": SKIN_IMAGE, "SKIN_DIM": SKIN_DIM,
             "SKIN_IMAGE_H": SKIN_IMAGE_H, "SKIN_IMAGE_POS": SKIN_IMAGE_POS,
             "SKIN_IMAGE_W": SKIN_IMAGE_W, "SKIN_SHOW_DOT": SKIN_SHOW_DOT}
_DEFAULT_CATS = dict(COLORS)

# 皮肤可覆盖的颜色键 → 对应模块全局名
SKIN_COLOR_MAP = {
    "bg": "BG", "panel": "PANEL", "track": "TRACK", "hover": "HOVER",
    "fg_dim": "FG_DIM", "text": "TXT", "accent": "ACCENT",
}


def valid_color(v):
    return isinstance(v, str) and v.startswith("#") and len(v) in (4, 7)


def apply_skin(skin, base_dir=""):
    """把皮肤配置写入模块全局（函数运行时读取，改后即时生效）。"""
    g = globals()
    changed = False
    if isinstance(skin, dict):
        colors = skin.get("colors")
        if isinstance(colors, dict):
            for key, gname in SKIN_COLOR_MAP.items():
                v = colors.get(key)
                if valid_color(v):
                    g[gname] = v
                    changed = True
            for cat in CATEGORIES:
                v = colors.get(cat)
                if valid_color(v):
                    COLORS[cat] = v
                    changed = True
        # 字体
        if isinstance(skin.get("font"), str) and skin["font"].strip():
            g["FAMILY"] = skin["font"].strip()
            changed = True
        # 背景压暗
        dim = skin.get("image_dim")
        if isinstance(dim, (int, float)) and 0 <= dim <= 1:
            g["SKIN_DIM"] = float(dim)
            changed = True
        # 背景区高度
        ih = skin.get("image_h")
        if ih is None or isinstance(ih, int) and 24 <= ih <= 400:
            g["SKIN_IMAGE_H"] = ih
            changed = True
        # 图片位置与左侧宽度
        pos = skin.get("image_pos")
        if pos in ("top", "left"):
            g["SKIN_IMAGE_POS"] = pos
            changed = True
        iw = skin.get("image_w")
        if isinstance(iw, int) and 40 <= iw <= 300:
            g["SKIN_IMAGE_W"] = iw
            changed = True
        # 分类前圆点开关
        sd = skin.get("show_dot")
        if isinstance(sd, bool):
            g["SKIN_SHOW_DOT"] = sd
            changed = True
        # 背景图（PNG/GIF，相对皮肤文件所在目录）
        img = skin.get("image")
        if isinstance(img, str) and img:
            path = img if os.path.isabs(img) else os.path.join(base_dir, img)
            if os.path.isfile(path) and path.lower().endswith((".png", ".gif")):
                g["SKIN_IMAGE"] = path
                changed = True
    return changed

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
    settings.setdefault("goals_updated_ts", 0.0)  # 目标最后修改时间，同步用
    settings.setdefault("todos_updated_ts", 0.0)  # 待办最后修改时间，同步用
    settings.setdefault("todo_visible", True)     # 待办列表是否展开
    settings.setdefault("mini_edge", "right")     # 迷你图标停靠边
    settings.setdefault("mini_pos", None)         # 迷你图标沿边位置
    settings.setdefault("pomodoro", False)
    settings.setdefault("idle_pause", True)
    settings.setdefault("host_addr", "")      # 客户端模式连接的主机 IP
    settings.setdefault("sync_host", False)   # 是否作为主机共享
    settings.setdefault("font_size", 13)      # 悬浮条字号（Ctrl+滚轮调节）
    # 每日待办：{日期: [{"text":..., "done": bool}, ...]}
    todos = data.get("todos", {})
    data["daily"] = daily
    data["settings"] = settings
    data["todos"] = todos
    return data


def merge_daily(dst, src):
    """按 日期+分类 取较大值合并，返回发生变化的格子数。"""
    changed = 0
    for d, day in src.items():
        tgt = dst.setdefault(d, {c: 0 for c in CATEGORIES})
        for c in CATEGORIES:
            v = day.get(c, 0)
            if v > tgt.get(c, 0):
                tgt[c] = v
                changed += 1
    return changed


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


def _latest_commit_sha(timeout=4):
    """从 GitHub API 拿 main 最新提交号；失败返回 None。"""
    import urllib.request
    url = f"https://api.github.com/repos/{_REPO}/commits/main"
    req = urllib.request.Request(url, headers={"User-Agent": "desktop-clock-updater"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))["sha"]


def _fetch_codeload(timeout=10):
    """GitHub codeload 整仓 zip：权威无缓存源，国内一般可达。"""
    import io
    import urllib.request
    import zipfile
    url = f"https://codeload.github.com/{_REPO}/zip/refs/heads/main"
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        zf = zipfile.ZipFile(io.BytesIO(resp.read()))
    for name in zf.namelist():
        if name.endswith("/time_tracker.py"):
            return zf.read(name).decode("utf-8")
    raise ValueError("zip 中找不到 time_tracker.py")


def fetch_latest(timeout=6):
    """查询所有可达更新源，取版本号最大的一份。

    codeload/raw 为 GitHub 权威源（无缓存）；CDN 可能缓存过期
    （jsDelivr @main 可缓存数小时），只作回退且不单独采信。
    """
    import urllib.request
    candidates = []

    def try_parse(text, source):
        ver = parse_version(text)
        if not ver:
            raise ValueError(f"{source} 缺少版本号")
        return ver

    try:
        text = _fetch_codeload()
        candidates.append((try_parse(text, "codeload"), text))
    except (OSError, ValueError):
        pass

    urls = list(UPDATE_URLS)
    try:
        sha = _latest_commit_sha()
        if sha:
            urls[1:1] = [u.replace("@main", f"@{sha}") for u in UPDATE_URLS[1:]]
    except (OSError, KeyError, ValueError):
        pass
    last_err = OSError("无可用更新源")
    for url in urls:
        try:
            with urllib.request.urlopen(url, timeout=timeout) as resp:
                text = resp.read().decode("utf-8")
            candidates.append((try_parse(text, url), text))
        except (OSError, ValueError) as e:
            last_err = e

    if not candidates:
        raise last_err

    def ver_key(v):
        try:
            return tuple(int(x) for x in v.split("."))
        except ValueError:
            return (0, 0, 0)

    return max(candidates, key=lambda c: ver_key(c[0]))


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


def pythonw_executable():
    """pythonw.exe：无控制台窗口运行 GUI。"""
    exe = sys.executable
    d, base = os.path.split(exe)
    cand = os.path.join(d, base.replace("python", "pythonw"))
    return cand if os.path.isfile(cand) else exe


def restart_app():
    """延迟启动新实例：旧进程退出并释放同步端口后，新进程再启动。

    用 pythonw 启动，更新重启后不再弹出黑色控制台窗口。
    """
    path = os.path.abspath(__file__)
    no_window = 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW
    starter = ("import time, subprocess, os, sys; time.sleep(1.2); "
               f"subprocess.Popen([r{pythonw_executable()!r}, {path!r}], "
               f"creationflags={no_window})")
    subprocess.Popen([sys.executable, "-c", starter],
                     creationflags=no_window)


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
                                "elapsed": a.elapsed(),
                                "goals": a.settings.get("goals", {}),
                                "goals_ts": a.settings.get("goals_updated_ts", 0),
                                "todos": a.data.get("todos", {}),
                                "todos_ts": a.settings.get("todos_updated_ts", 0),
                                "pom": {"enabled": a.pom_var.get(),
                                        "state": a.pom_state,
                                        "end": a.pom_end_ts}})
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
                    outer.cmd_q.put(("merge", payload))
                    self._send({"ok": True})
                elif self.path == "/control":
                    outer.cmd_q.put(("control", payload))
                    self._send({"ok": True})
                elif self.path == "/activity":
                    outer.cmd_q.put(("activity", payload))
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
    # pythonw 无控制台窗口，开机自启不再弹黑框
    return f'"{pythonw_executable()}" "{os.path.abspath(__file__)}"'


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
        messagebox.showerror("开机自启", f"设置失败：{e}", parent=self.dlg)
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
        self.after(200, self.apply_rounded_corners)

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
        self._last_tick = datetime.now().timestamp()

        # 局域网同步
        self.sync_server = None
        self.mirror = None          # 客户端镜像：{"cat", "elapsed", "ts"}
        self.sync_warned = False
        self.last_client_active_ts = 0.0  # 最近一次客户端报告的用户活动
        if self.settings.get("sync_host"):
            self.start_host()

        self.quote = QUOTES[0]
        # 对话框宿主：正常有边框窗口（隐藏）。所有弹窗挂它下面，
        # 避免无边框悬浮条导致的弹窗被压到下层的问题。
        self.dlg = tk.Toplevel(self)
        self.dlg.withdraw()
        self._topmost_done = set()  # 已打上置顶的弹窗，避免重复抢层级
        self.apply_skin_by_name(self.settings.get("skin", ""))  # 启动即换肤
        self.build_ui()
        if self.settings.get("mini"):
            self.minimize_to_mini()
        self.quote_loop()
        self.update_loop()
        self.hotkey_loop()
        self.sync_loop()
        self.after(15000, self.silent_update_check)

    def apply_rounded_corners(self):
        """Win11 DWM 圆角（DWMWA_WINDOW_CORNER_PREFERENCE=33, ROUND=2）。
        Win10 及以下没有该属性，静默跳过。"""
        try:
            hwnd = user32.GetAncestor(self.winfo_id(), 2)  # GA_ROOT
            pref = ctypes.c_int(2)
            ctypes.windll.dwmapi.DwmSetWindowAttribute(
                hwnd, 33, ctypes.byref(pref), 4)
        except (OSError, AttributeError):
            pass

    def position_window(self):
        # 记忆位置可能因分辨率变化/多屏拔插落在屏幕外，钳制回可视区
        sw = self.winfo_screenwidth()
        sh = self.winfo_screenheight()
        geo = self.data.get("geometry")
        if geo:
            x = max(0, min(int(geo[0]), sw - 100))
            y = max(0, min(int(geo[1]), sh - 40))
        else:
            x = (sw - 480) // 2
            y = 60
        self.geometry(f"+{x}+{y}")
        self.update_idletasks()

    # ---------------- 悬浮条界面 ----------------

    def build_ui(self):
        # 画布化界面：全部内容绘制在单一 Canvas 上，
        # 皮肤图片可以作为真正的全条背景，文字直接画在图上
        self.cv = tk.Canvas(self, highlightthickness=0, bg=BG)
        self.cv.pack(fill="both", expand=True)
        self._regions = []       # (x1,y1,x2,y2,kind,data) 命中区域
        self._hover = None       # 当前悬停区域键
        self._img_cache = {}     # 皮肤背景图缩放缓存

        self.cv.bind("<Motion>", self._on_motion)
        self.cv.bind("<Leave>", self._on_leave)
        self.cv.bind("<Double-Button-1>", self._on_double)
        self.cv.bind("<Button-3>", self._on_right_click)
        # 窗口尺寸变化（首次映射/缩放/待办增减）立即重绘，
        # 保证点击区域与画面始终一致
        self.cv.bind("<Configure>", lambda e: self._render())

        # 拖动绑定在根窗口：通过 bindtags 覆盖画布；
        # drag_start 里 grab 独占指针，窗口移动后事件不断流
        self.bind("<Button-1>", self.drag_start, add="+")
        self.bind("<B1-Motion>", self.drag_move, add="+")
        self.bind("<ButtonRelease-1>", self.drag_release, add="+")
        self.bind("<ButtonRelease-1>", self._on_click, add="+")

        # 右键菜单
        self.menu = tk.Menu(self, tearoff=0)
        self.menu.add_command(label=f"⏱ 时长记录器 v{VERSION}",
                              state="disabled")
        self.menu.add_command(label="历史统计", command=self.show_history)
        self.menu.add_command(label="导出 CSV 报表", command=self.export_csv)
        self.menu.add_command(label="导出 HTML 周报", command=self.export_html)
        self.menu.add_command(label="每日目标设置…", command=self.edit_goals)
        self.menu.add_command(label="添加待办…", command=self.add_todo_item)
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
        self.menu.add_command(label="缩小为贴边小图标",
                              command=self.minimize_to_mini)
        self.menu.add_command(label="大小：按住 Ctrl 滚动滚轮调节",
                              state="disabled")
        skin_menu = tk.Menu(self.menu, tearoff=0)
        self.menu.add_cascade(label="皮肤", menu=skin_menu)
        self.skin_var = tk.StringVar(value=self.settings.get("skin", ""))
        skin_menu.add_radiobutton(label="默认", value="",
                                  variable=self.skin_var,
                                  command=lambda: self.set_skin(""))
        for s in self.load_skins():
            skin_menu.add_radiobutton(
                label=s["name"], value=s["file"], variable=self.skin_var,
                command=lambda f=s["file"]: self.set_skin(f))
        self.menu.add_separator()
        self.autostart_item = tk.BooleanVar(value=autostart_enabled())
        self.menu.add_checkbutton(label="开机自启",
                                  variable=self.autostart_item,
                                  command=self.toggle_autostart)
        self.menu.add_command(label="检查更新", command=self.check_update)
        self.menu.add_command(label="退出", command=self.on_close)

        # Ctrl+滚轮调节悬浮条大小
        self.bind("<Control-MouseWheel>", self.on_zoom)
        self.apply_ui_size()

    def apply_ui_size(self):
        """按字号与待办数量计算窗口尺寸（画布内容随之重绘）。

        宽度用 tkfont 实测分类文本宽，列再窄也不会挤压错位。"""
        size = self.settings.get("font_size", 13)
        s4 = max(9, size - 4)
        f = tkfont.Font(family=FAMILY, size=size, root=self)
        text_w = f.measure(f"○ {CATEGORIES[0]} 00:00:00") + 20
        left_mode = SKIN_IMAGE and SKIN_IMAGE_POS == "left"
        img_w = max(40, min(SKIN_IMAGE_W, 300)) if left_mode else 0
        w = max(360, int(img_w + 12 + (text_w + 8) * 3 + 22))
        self.update_idletasks()
        L = self._layout(w, size)
        x, y = self.winfo_x(), self.winfo_y()
        self.geometry(f"{w}x{L['H']}+{x}+{y}")
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
        # 钳制在屏幕内，至少留 60px 可见，避免拖丢找不回
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        nx = max(0, min(event.x_root - self.drag_off[0], sw - 60))
        ny = max(0, min(event.y_root - self.drag_off[1], sh - 20))
        self.geometry(f"+{nx}+{ny}")

    def drag_release(self, event):
        try:
            self.grab_release()
        except tk.TclError:
            pass
        if getattr(self, "drag_moved", False):
            self.data["geometry"] = [self.winfo_x(), self.winfo_y()]
            self.save()

    def click_or_drag_end(self, cat):
        if self.locked or getattr(self, "drag_moved", False):
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
        self.handle_wakeup_gap()
        self.check_external_change()
        self.process_sync_queue()
        self.check_idle()
        self.check_pomodoro()
        self.check_continuous_alert()
        self.refresh()
        self.after(TICK_MS, self.update_loop)

    def handle_wakeup_gap(self):
        """锁屏睡眠唤醒：tick 间隔异常大说明系统刚从睡眠恢复。

        睡眠期间进程被冻结、无键鼠输入，这段时间不应计入任何计时。
        """
        now = datetime.now().timestamp()
        gap = now - getattr(self, "_last_tick", now)
        self._last_tick = now
        if gap < 60:
            return
        if self.running_cat and self.start_ts:
            self.start_ts += gap          # 把睡眠时长从会话中剔除
            self.last_remind_ts = 0.0
        if self.pom_state:
            self.pom_end_ts += gap        # 番茄钟倒计时顺延
        if self.running_cat or self.pom_state:
            self.toast("系统唤醒",
                       "电脑刚从睡眠中唤醒，睡眠时间未计入计时，"
                       "当前计时已自动延续。")

    def check_idle(self):
        if not self.idle_var.get():
            return
        # 本机空闲且没有任何客户端报告过用户活动，才算真正离开
        client_recently_active = (datetime.now().timestamp()
                                  - self.last_client_active_ts
                                  < IDLE_PAUSE_SECONDS)
        if (self.running_cat and idle_seconds() > IDLE_PAUSE_SECONDS
                and not client_recently_active):
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

    def _apply_popup_topmost(self):
        """应用新弹出的窗口一次性打上置顶，之后不再动它们的层级。

        弹窗进入置顶窗口组后天然压住悬浮条（组内后插入者在先），
        无需每秒抬升；已处理过的窗口跳过，避免层级抖动。
        """
        for w in list(self.dlg.winfo_children()) + list(self.winfo_children()):
            if isinstance(w, tk.Toplevel) and w.winfo_viewable() \
                    and id(w) not in self._topmost_done:
                w.attributes("-topmost", True)
                self._topmost_done.add(id(w))

    WS_EX_TOPMOST = 0x00000008
    GWL_EXSTYLE = -20

    def _is_topmost(self):
        """读窗口的 WS_EX_TOPMOST 样式位，判断是否仍在置顶层。"""
        hwnd = user32.GetAncestor(self.winfo_id(), 2)  # GA_ROOT
        style = user32.GetWindowLongW(hwnd, self.GWL_EXSTYLE)
        return bool(style & self.WS_EX_TOPMOST)

    def refresh(self):
        # 悬浮条置顶只用于压住其他应用。重申置顶会把悬浮条顶到
        # 置顶窗口组最上层，盖住主菜单/弹窗，因此不能每秒无脑申明；
        # 只有置顶样式位真的丢失（如锁屏解锁后）时才补一次。
        self._apply_popup_topmost()
        if not self._is_topmost():
            self.attributes("-topmost", True)
        self._render()
        if self.settings.get("mini"):
            self._draw_mini()

    # ---------------- 画布渲染 ----------------

    def _bg_natural(self):
        """皮肤背景图原始尺寸（含缓存）。"""
        if not SKIN_IMAGE:
            return None
        nat = getattr(self, "_bg_nat", None)
        if nat and nat[0] == SKIN_IMAGE:
            return nat[1], nat[2]
        try:
            img = tk.PhotoImage(file=SKIN_IMAGE)
        except (tk.TclError, OSError):
            return None
        self._bg_nat = (SKIN_IMAGE, img.width(), img.height())
        return img.width(), img.height()

    def _scaled_bg(self, W, H):
        """把皮肤背景图缩放到 W×H（整数 zoom/subsample 链，带缓存）。"""
        if not SKIN_IMAGE:
            return None
        key = (SKIN_IMAGE, W, H)
        cached = self._img_cache.get(key)
        if cached:
            return cached
        nat = self._bg_natural()
        if not nat:
            return None

        def plan(target, src):
            f = target / src
            if f >= 1:
                z = min(4, max(1, round(f)))
                d = max(1, round(z / f))
            else:
                d = min(4, max(1, int(1 / f)))
                z = 1
            return z, d

        zx, dx = plan(W, nat[0])
        zy, dy = plan(H, nat[1])
        img = tk.PhotoImage(file=SKIN_IMAGE)
        if (zx, zy) != (1, 1):
            img = img.zoom(zx, zy)
        if (dx, dy) != (1, 1):
            img = img.subsample(dx, dy)
        self._img_cache.clear()  # 只保留当前尺寸，防止内存累积
        self._img_cache[key] = img
        return img

    def _layout(self, W, size):
        """计算画布布局几何与窗口总高（行高 + 固定间距模型）。"""
        left_mode = SKIN_IMAGE and SKIN_IMAGE_POS == "left"
        nat = self._bg_natural()
        banner_h = 0
        if nat and not left_mode:
            if SKIN_IMAGE_H:
                banner_h = min(SKIN_IMAGE_H, 400)
            else:
                banner_h = max(24, min(int(nat[1] * W / nat[0]), 200))
        img_w = 0
        if left_mode:
            img_w = max(40, min(SKIN_IMAGE_W, 300))
        s4 = max(9, size - 4)
        gap = 6
        pad_x = (img_w + 12) if left_mode else 10
        row_cat = int(size * 2.0)
        bar_h = max(4, size // 3)
        row_st = int(s4 * 1.9)
        y = banner_h + 8
        cat_cy = y + row_cat / 2
        bar_y = y + row_cat + gap
        st_cy = bar_y + bar_h + gap + row_st / 2
        th_y = st_cy + row_st / 2 + gap
        hh = int(s4 * 1.7) + 4
        items = self.today_todos()
        visible = self.settings.get("todo_visible", True)
        n = min(5, len(items)) if visible else 0
        more = bool(visible and len(items) > 5)
        row_h = int(s4 * 1.8)
        area_h = hh
        if visible:
            area_h += n * row_h + (int(s4 * 1.4) + 6 if more else 0)
        H = int(th_y + area_h + 8)
        x0 = pad_x
        colw = (W - 12 - x0) / 3
        return dict(banner_h=banner_h, cat_cy=int(cat_cy), row_cat=row_cat,
                    bar_h=bar_h, bar_y=int(bar_y), st_cy=int(st_cy),
                    th_y=int(th_y), hh=hh, row_h=row_h, n=n, more=more,
                    visible=visible, pad_x=pad_x, x0=x0, colw=colw, W=W, H=H,
                    img_w=img_w)

    def _render(self):
        """全量重绘画布（内容项少，1Hz 全绘无压力）。"""
        cv = self.cv
        if not cv.winfo_exists():
            return
        W = max(360, cv.winfo_width())
        size = self.settings.get("font_size", 13)
        s4 = max(9, size - 4)
        L = self._layout(W, size)
        cv.delete("all")
        self._regions = []

        # 背景：图片 either 左侧立绘（搜狗式）or 顶部横幅带 + 可选压暗层
        if SKIN_IMAGE and SKIN_IMAGE_POS == "left" and L["img_w"]:
            photo = self._scaled_bg(L["img_w"], L["H"])
            if photo:
                ox = (L["img_w"] - photo.width()) // 2   # 居中裁切
                oy = (L["H"] - photo.height()) // 2
                cv.create_image(ox, oy, image=photo, anchor="nw")
            if SKIN_DIM > 0:
                stip = ("gray12" if SKIN_DIM <= 0.33 else
                        "gray25" if SKIN_DIM <= 0.66 else
                        "gray50" if SKIN_DIM <= 0.85 else "gray75")
                cv.create_rectangle(0, 0, L["img_w"], L["H"], fill="#000000",
                                    stipple=stip, width=0)
            cv.create_rectangle(L["img_w"], 0, W, L["H"], fill=BG, width=0)
        elif L["banner_h"]:
            photo = self._scaled_bg(W, L["banner_h"])
            if photo:
                cv.create_image(0, 0, image=photo, anchor="nw")
            if SKIN_DIM > 0:
                stip = ("gray12" if SKIN_DIM <= 0.33 else
                        "gray25" if SKIN_DIM <= 0.66 else
                        "gray50" if SKIN_DIM <= 0.85 else "gray75")
                cv.create_rectangle(0, 0, W, L["banner_h"], fill="#000000",
                                    stipple=stip, width=0)
            cv.create_rectangle(0, L["banner_h"], W, L["H"], fill=BG, width=0)

        pad_x, x0, colw = L["pad_x"], L["x0"], L["colw"]
        cat_cy, row_cat = L["cat_cy"], L["row_cat"]

        # 分类行（模式切换挪到状态行右端，避免左侧孤字）
        for i, cat in enumerate(CATEGORIES):
            cx = x0 + i * colw
            total = self.display_seconds(cat)
            if self.running_cat == cat:
                fg, dot = COLORS[cat], "●"
            elif self.mirror and self.mirror["cat"] == cat \
                    and not self.running_cat:
                fg, dot = COLORS[cat], "◐"
            else:
                fg, dot = FG_DIM, "○"
            if not SKIN_SHOW_DOT:
                dot = ""
            hov = self._hover == ("cat", cat)
            ry = cat_cy - row_cat / 2
            if hov:
                self._round_rect(cv, cx + 2, ry + 2, cx + colw - 6,
                                 ry + row_cat - 2, 6,
                                 fill=HOVER, outline="")
            cv.create_text(cx + 9, cat_cy + 1, anchor="w",
                           text=f"{dot + ' ' if dot else ''}{cat} {self.fmt(total)}",
                           font=(FAMILY, size), fill="#000000")
            cv.create_text(cx + 8, cat_cy, anchor="w",
                           text=f"{dot + ' ' if dot else ''}{cat} {self.fmt(total)}",
                           font=(FAMILY, size), fill=TXT if hov else fg)
            self._regions.append((cx, ry, cx + colw - 4, ry + row_cat,
                                  "cat", cat))
            bx, bw = cx + 4, colw - 12
            goal = self.settings["goals"].get(cat, 0)
            self._round_rect(cv, bx, L["bar_y"], bx + bw,
                             L["bar_y"] + L["bar_h"], r=L["bar_h"] / 2,
                             fill=TRACK, outline="")
            if goal:
                done = self.data["daily"].get(today_str(), {}).get(cat, 0)
                if cat == self.running_cat:
                    done += self.elapsed()
                pct = max(0.0, min(1.0, done / goal))
                fw = max(L["bar_h"], bw * pct)
                self._round_rect(cv, bx, L["bar_y"], bx + fw,
                                 L["bar_y"] + L["bar_h"], r=L["bar_h"] / 2,
                                 fill=COLORS[cat], outline="")

        # 状态 / 名言行：左模式切换（累计/今日）、右名言（超长截断）
        st_text, st_color = self._status_info()
        st_font = tkfont.Font(family=FAMILY, size=s4, root=self)
        mode_txt = "今日" if self.show_mode == "today" else "累计"
        mode_hov = self._hover == ("mode", None)
        mode_x2 = pad_x + st_font.measure(mode_txt) + 12
        if mode_hov:
            self._round_rect(cv, pad_x - 2, L["st_cy"] - s4,
                             mode_x2, L["st_cy"] + s4, 5,
                             fill=HOVER, outline="")
        cv.create_text(pad_x, L["st_cy"], anchor="w", text=mode_txt,
                       font=(FAMILY, s4),
                       fill=TXT if mode_hov else "#9E9E9E")
        self._regions.append((pad_x - 2, L["st_cy"] - s4 - 2, mode_x2,
                              L["st_cy"] + s4 + 2, "mode", None))
        avail = W - mode_x2 - 12
        if st_font.measure(st_text) > avail:
            while st_text and st_font.measure(st_text + "…") > avail:
                st_text = st_text[:-1]
            st_text += "…"
        cv.create_text(mode_x2 + 6, L["st_cy"], anchor="w", text=st_text,
                       font=(FAMILY, s4), fill=st_color)

        # 待办区
        items = self.today_todos()
        visible = L["visible"]
        arrow = "▾" if visible else "▸"
        if items:
            undone = sum(1 for i in items if not i.get("done"))
            header = f"{arrow} 待办 {undone}/{len(items)}"
        else:
            header = f"{arrow} 待办（右键添加）"
        th_hov = self._hover == ("th", None)
        if th_hov:
            self._round_rect(cv, 4, L["th_y"], W - 4,
                             L["th_y"] + L["hh"], 6, fill=HOVER, outline="")
        cv.create_text(pad_x, L["th_y"] + L["hh"] / 2, anchor="w",
                       text=header, font=(FAMILY, s4),
                       fill=TXT if th_hov else "#9E9E9E")
        self._regions.append((0, L["th_y"], W, L["th_y"] + L["hh"],
                              "th", None))
        if not visible:
            return

        # 待办条目：圈选点 + 文本 + 完成删除线
        shown = items[:5]
        for i, item in enumerate(shown):
            ry = L["th_y"] + L["hh"] + i * L["row_h"]
            done = bool(item.get("done", False))
            hov = self._hover == ("todo", i)
            if hov:
                cv.create_rectangle(pad_x - 4, ry, W - 4, ry + L["row_h"],
                                    fill=HOVER, width=0)
            cy = ry + L["row_h"] / 2
            r = s4 / 2 + 1
            cv.create_oval(pad_x + 6, cy - r, pad_x + 6 + 2 * r, cy + r,
                           fill=FG_DIM if done else "",
                           outline=FG_DIM, width=2)
            tcolor = "#777777" if done else TXT
            tid = cv.create_text(pad_x + 16 + r, cy, anchor="w",
                                 text=item.get("text", ""),
                                 font=(FAMILY, s4), fill=tcolor)
            if done:
                bb = cv.bbox(tid)
                if bb:
                    cv.create_line(bb[0], cy, bb[2], cy,
                                   fill="#777777", width=1)
            self._regions.append((pad_x, ry, W, ry + L["row_h"], "todo", i))

        if len(items) > 5:
            cv.create_text(pad_x, L["th_y"] + L["hh"] + 5 * L["row_h"] + 4,
                           anchor="w",
                           text=f"…还有 {len(items) - 5} 条（右键管理）",
                           font=(FAMILY, s4 - 1), fill="#777777")

    def _status_info(self):
        """状态栏文字与颜色（画布渲染用）。"""
        now = datetime.now().timestamp()
        sync_tag = ""
        if self.sync_server:
            sync_tag = f"（主机 {lan_ip()}:{SYNC_PORT}）"
        elif self.settings.get("host_addr"):
            sync_tag = f"（同步自 {self.settings['host_addr']}）"
        if self.locked:
            return "已锁定：鼠标穿透，按 Ctrl+Alt+L 解锁", ACCENT
        if self.pom_state == "focus" and self.running_cat:
            remain = int(self.pom_end_ts - now)
            return (f"🍅 专注中，剩余 {self.fmt(max(0, remain))}{sync_tag}",
                    COLORS["工作"])
        if self.pom_state == "break":
            remain = int(self.pom_end_ts - now)
            return (f"☕ 休息中，剩余 {self.fmt(max(0, remain))}{sync_tag}",
                    "#888888")
        if self.mirror and not self.running_cat:
            return (f"主机正在计时：{self.mirror['cat']}{sync_tag}",
                    COLORS.get(self.mirror["cat"], "#888888"))
        if self.idle_paused:
            return "已自动暂停（检测到离开），点击分类继续" + sync_tag, ACCENT
        return (self.quote + sync_tag if sync_tag else self.quote, "#777777")

    # ---------------- 画布事件 ----------------

    def _region_at(self, x, y):
        for reg in reversed(self._regions):
            if reg[0] <= x <= reg[2] and reg[1] <= y <= reg[3]:
                return reg
        return None

    def _on_motion(self, event):
        reg = self._region_at(event.x, event.y)
        hoverable = {"mode", "cat", "todo", "th"}
        key = (reg[4], reg[5]) if reg and reg[4] in hoverable else None
        if key != self._hover:
            self._hover = key
            self.cv.config(cursor="hand2" if key else "")
            self._render()

    def _on_leave(self, event):
        if self._hover is not None:
            self._hover = None
            self.cv.config(cursor="")
            self._render()

    def _on_click(self, event):
        if self.locked or getattr(self, "drag_moved", False):
            return
        reg = self._region_at(event.x, event.y)
        if not reg:
            return
        kind, data = reg[4], reg[5]
        if kind == "mode":
            self.toggle_show_mode()
            self.refresh()
        elif kind == "cat":
            self.click_or_drag_end(data)
        elif kind == "todo":
            self.toggle_todo_done(data)
        elif kind == "th":
            self.toggle_todo_visible()

    def _on_double(self, event):
        if self.locked or getattr(self, "drag_moved", False):
            return
        reg = self._region_at(event.x, event.y)
        if reg and reg[4] == "todo":
            self.edit_todo_item(reg[5])

    def _on_right_click(self, event):
        reg = self._region_at(event.x, event.y)
        if reg and reg[4] == "todo":
            self.todo_menu_popup(event, reg[5])
        elif reg and reg[4] == "th":
            self.todo_area_menu(event)
        else:
            self.popup_menu(event)

    def _round_rect(self, canvas, x1, y1, x2, y2, r, **kw):
        """平滑圆角矩形（canvas polygon + smooth）。"""
        r = min(r, (x2 - x1) / 2, (y2 - y1) / 2)
        pts = [x1+r, y1, x2-r, y1, x2, y1, x2, y1+r, x2, y2-r, x2, y2,
               x2-r, y2, x1+r, y2, x1, y2, x1, y2-r, x1, y1+r, x1, y1]
        return canvas.create_polygon(pts, smooth=True, **kw)

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
        win = tk.Toplevel(self.dlg)  # 挂对话框宿主，脱离悬浮条的置顶组
        win.title("每日目标设置")
        win.attributes("-topmost", True)
        win.lift()
        win.focus_force()
        win.resizable(False, False)
        entries = {}
        for i, cat in enumerate(CATEGORIES):
            tk.Label(win, text=f"{cat} 每日目标（小时，0 = 不设目标）",
                     font=(FAMILY, 10)).grid(row=i, column=0,
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
            self.settings["goals_updated_ts"] = datetime.now().timestamp()
            self.save()
            self.refresh()
            win.destroy()

        tk.Button(win, text="保存", command=apply, width=10).grid(
            row=len(CATEGORIES), column=0, columnspan=2, pady=8)

    # ---------------- 迷你贴边小图标 ----------------

    MINI_SIZE = 26

    def _mini_dock_pos(self):
        """按停靠边计算小图标位置（沿边位置取自记忆，钳制在屏内）。"""
        size = self.MINI_SIZE
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        edge = self.settings.get("mini_edge", "right")
        along = self.settings.get("mini_pos") or [sh // 3]
        along = max(size, min(int(along[0]), sh - size - 2))
        if edge == "left":
            return 2, along
        if edge == "bottom":
            x = max(size, min(int((self.settings.get("mini_pos") or [sw // 2])[0]),
                              sw - size - 2))
            return x, sh - size - 2
        return sw - size - 2, along

    def minimize_to_mini(self):
        """把悬浮条缩成贴边小圆点。"""
        self.settings["mini"] = True
        if not getattr(self, "mini", None):
            size = self.MINI_SIZE
            self.mini = tk.Toplevel(self)
            self.mini._is_toast = True  # 不参与弹窗检测
            self.mini.overrideredirect(True)
            self.mini.attributes("-topmost", True)
            self.mini.configure(bg=BG)
            self.mini.geometry(f"{size}x{size}")
            self.mini_canvas = tk.Canvas(self.mini, width=size, height=size,
                                         bg=BG, highlightthickness=0)
            self.mini_canvas.pack(fill="both", expand=True)
            self.mini.bind("<Button-1>", self.mini_drag_start, add="+")
            self.mini.bind("<B1-Motion>", self.mini_drag_move, add="+")
            self.mini.bind("<ButtonRelease-1>", self.mini_click, add="+")
            self.mini.bind("<Button-3>", self.mini_menu)
            self.mini_canvas.bind("<Button-1>", self.mini_drag_start, add="+")
            self.mini_canvas.bind("<B1-Motion>", self.mini_drag_move, add="+")
            self.mini_canvas.bind("<ButtonRelease-1>", self.mini_click, add="+")
            self.mini_canvas.bind("<Button-3>", self.mini_menu)
        x, y = self._mini_dock_pos()
        self.mini.geometry(f"+{x}+{y}")
        self.withdraw()
        self.mini.deiconify()
        self._apply_mini_region()
        self._draw_mini()
        self.save()

    def _apply_mini_region(self):
        """把方形小图标窗口裁成圆形，去掉四角的深色底（黑框）。

        窗口刚 deiconify 尚未完成映射时应用区域会被丢弃，
        因此 _draw_mini 每秒都会检查并补上（一次系统调用，开销可忽略）。
        """
        try:
            hwnd = user32.GetAncestor(self.mini.winfo_id(), 2)  # GA_ROOT
            if not hwnd:
                return
            buf = ctypes.windll.gdi32.CreateRectRgn(0, 0, 0, 0)
            if user32.GetWindowRgn(hwnd, buf) in (2, 3):
                return  # 区域已在，无需重复
            rgn = ctypes.windll.gdi32.CreateEllipticRgn(
                0, 0, self.MINI_SIZE, self.MINI_SIZE)
            user32.SetWindowRgn(hwnd, rgn, True)
        except (OSError, AttributeError):
            pass

    def restore_from_mini(self):
        """小图标恢复成完整悬浮条，位置出现在小图标附近。"""
        self.settings["mini"] = False
        if getattr(self, "mini", None):
            x = min(self.mini.winfo_x(),
                    self.winfo_screenwidth() - self.winfo_width() - 4)
            y = min(self.mini.winfo_y(),
                    self.winfo_screenheight() - self.winfo_height() - 4)
            self.mini.withdraw()
        else:
            x, y = 460, 60
        self.geometry(f"+{max(0, x)}+{max(0, y)}")
        self.deiconify()
        self.apply_rounded_corners()
        self.save()

    def _draw_mini(self):
        """重画小圆点：外圈轨道色，内点为计时分类色。"""
        if not getattr(self, "mini", None) or not self.mini.winfo_exists():
            return
        cv = self.mini_canvas
        size = self.MINI_SIZE
        cv.config(bg=BG)
        self.mini.config(bg=BG)
        cv.delete("all")
        r = size / 2 - 2
        cx = cy = size / 2
        cv.create_oval(cx - r, cy - r, cx + r, cy + r,
                       fill=TRACK, outline="")
        cat = self.running_cat or (self.mirror or {}).get("cat")
        inner = COLORS.get(cat, FG_DIM)
        ri = r * (0.75 if cat else 0.45)
        cv.create_oval(cx - ri, cy - ri, cx + ri, cy + ri,
                       fill=inner, outline="")
        self._apply_mini_region()

    def mini_drag_start(self, event):
        self.mini_moved = False
        self.mini_off = (event.x_root - self.mini.winfo_x(),
                         event.y_root - self.mini.winfo_y())

    def mini_drag_move(self, event):
        self.mini_moved = True
        size = self.MINI_SIZE
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        edge = self.settings.get("mini_edge", "right")
        # 停靠轴钉死在边缘，自由轴跟随鼠标并钳制
        if edge == "left":
            x, y = 2, event.y_root - self.mini_off[1]
        elif edge == "bottom":
            x, y = event.x_root - self.mini_off[0], sh - size - 2
        else:
            x, y = sw - size - 2, event.y_root - self.mini_off[1]
        free_x = edge != "bottom"
        if free_x:
            x = max(2, min(x, sw - size - 2))
        else:
            x = max(2, min(x, sw - size - 2))
        y = max(2, min(y, sh - size - 2))
        self.mini.geometry(f"+{x}+{y}")
        self.settings["mini_pos"] = [x if edge == "bottom" else y]

    def mini_click(self, event):
        if not getattr(self, "mini_moved", False):
            self.restore_from_mini()

    def mini_menu(self, event):
        edge = self.settings.get("mini_edge", "right")
        menu = tk.Menu(self, tearoff=0)
        menu.add_command(label="恢复悬浮条", command=self.restore_from_mini)
        edge_menu = tk.Menu(menu, tearoff=0)
        for name, key in (("停靠左侧", "left"), ("停靠右侧", "right"),
                          ("停靠下边", "bottom")):
            edge_menu.add_radiobutton(
                label=name, value=key,
                command=lambda k=key: self.set_mini_edge(k))
        menu.add_cascade(label="停靠位置", menu=edge_menu)
        menu.add_separator()
        menu.add_command(label="退出", command=self.on_close)
        menu.tk_popup(event.x_root, event.y_root)

    def set_mini_edge(self, edge):
        self.settings["mini_edge"] = edge
        self.save()
        if getattr(self, "mini", None) and self.mini.winfo_viewable():
            x, y = self._mini_dock_pos()
            self.mini.geometry(f"+{x}+{y}")

    # ---------------- 提醒气泡 ----------------

    def toast(self, title, message, duration_ms=8000):
        win = tk.Toplevel(self)
        win._is_toast = True  # 不算弹窗，不阻止悬浮条重申置顶
        win.overrideredirect(True)
        win.attributes("-topmost", True)
        win.attributes("-alpha", 0.92)
        win.configure(bg="#2A2A30")
        accent = tk.Frame(win, bg=ACCENT, width=3)
        accent.pack(side="left", fill="y")
        pad = tk.Frame(win, bg="#2A2A30", padx=14, pady=10)
        pad.pack(fill="both", expand=True)
        tk.Label(pad, text=title, font=(FAMILY, 10, "bold"),
                 bg="#2A2A30", fg=ACCENT).pack(anchor="w")
        tk.Label(pad, text=message, font=(FAMILY, 10),
                 bg="#2A2A30", fg="#EEEEEE", wraplength=260,
                 justify="left").pack(anchor="w", pady=(2, 0))
        win.update_idletasks()
        w, h = win.winfo_reqwidth(), win.winfo_reqheight()
        x = self.winfo_screenwidth() - w - 30
        y = self.winfo_screenheight() - h - 60
        win.geometry(f"+{x}+{y}")
        win.after(duration_ms, win.destroy)
        return win

    # ---------------- 统计窗口 / 导出 ----------------

    def week_map(self):
        weeks = defaultdict(lambda: {c: 0 for c in CATEGORIES})
        for date_str, day in self.data["daily"].items():
            label = week_range(date_str)
            for c in CATEGORIES:
                weeks[label][c] += day.get(c, 0)
        return weeks

    def show_history(self):
        win = tk.Toplevel(self.dlg)  # 挂对话框宿主，脱离悬浮条的置顶组
        win.title(f"历史统计 · v{VERSION}")
        win.attributes("-topmost", True)
        win.lift()
        win.focus_force()

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
            messagebox.showinfo("导出报表", "还没有任何记录可导出。", parent=self.dlg)
            return
        path = filedialog.asksaveasfilename(
            parent=self.dlg, title="导出 CSV 报表",
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
            messagebox.showerror("导出报表", f"导出失败：{e}", parent=self.dlg)
            return
        messagebox.showinfo("导出报表", f"已导出到：\n{path}", parent=self.dlg)

    def export_html(self):
        if not self.data["daily"]:
            messagebox.showinfo("导出周报", "还没有任何记录可导出。", parent=self.dlg)
            return
        path = filedialog.asksaveasfilename(
            parent=self.dlg, title="导出 HTML 周报",
            initialfile=f"时长周报_{today_str()}.html",
            defaultextension=".html",
            filetypes=[("HTML 文件", "*.html")])
        if not path:
            return
        try:
            self.write_html(path)
        except OSError as e:
            messagebox.showerror("导出周报", f"导出失败：{e}", parent=self.dlg)
            return
        messagebox.showinfo("导出周报", f"已导出到：\n{path}", parent=self.dlg)

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
                                 f"主机服务启动失败（端口 {SYNC_PORT} 可能被占用）：\n{e}", parent=self.dlg)
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
                messagebox.showinfo("局域网同步", "已切换为主机模式。", parent=self.dlg)
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
            initialvalue=self.settings.get("host_addr", ""), parent=self.dlg)
        if not addr:
            return
        addr = addr.strip()
        if self.sync_server:
            messagebox.showwarning("连接主机", "本机是主机，不能连接其他主机。", parent=self.dlg)
            return
        # 试连一次确认可达
        try:
            self.fetch_state(addr)
        except OSError:
            messagebox.showerror("连接主机",
                                 f"无法连接 {addr}:{SYNC_PORT}，"
                                 "请确认对方已开启「作为主机共享」且防火墙放行。", parent=self.dlg)
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
        """客户端轮询：双向合并数据与每日目标 + 镜像主机计时状态。"""
        if self.settings.get("host_addr"):
            try:
                st = self.fetch_state()
                self.sync_warned = False
                changed = merge_daily(self.data["daily"], st.get("daily", {}))
                # 本地数据推回主机，形成双向合并（含每日目标、待办与本机活跃度）
                self.post("/merge", {"daily": self.data["daily"],
                                     "goals": self.settings.get("goals", {}),
                                     "goals_ts": self.settings.get("goals_updated_ts", 0),
                                     "todos": self.data.get("todos", {}),
                                     "todos_ts": self.settings.get("todos_updated_ts", 0),
                                     "user_idle": idle_seconds()})
                if self.apply_remote_goals(st.get("goals"), st.get("goals_ts")):
                    changed += 1
                if self.apply_remote_todos(st.get("todos"), st.get("todos_ts")):
                    changed += 1
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
                if changed:
                    self.refresh()
            except (OSError, json.JSONDecodeError, KeyError):
                self.mirror = None
        self.after(SYNC_POLL_MS, self.sync_loop)

    def apply_remote_goals(self, goals, ts):
        """每日目标同步：时间戳新者胜，返回本端是否被更新。"""
        if not isinstance(goals, dict) or not isinstance(ts, (int, float)):
            return False
        if ts <= self.settings.get("goals_updated_ts", 0):
            return False
        local = dict(self.settings.get("goals", {}))
        for c in CATEGORIES:
            if c in goals:
                local[c] = goals[c]
        self.settings["goals"] = local
        self.settings["goals_updated_ts"] = ts
        return True

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
                changed = merge_daily(self.data["daily"], payload.get("daily", {}))
                if self.apply_remote_goals(payload.get("goals"),
                                           payload.get("goals_ts")):
                    changed += 1
                if self.apply_remote_todos(payload.get("todos"),
                                           payload.get("todos_ts")):
                    changed += 1
                self.note_client_activity(payload.get("user_idle"))
                if changed:
                    self.save()
                    self.refresh()
            elif kind == "activity":
                self.note_client_activity(payload.get("idle"))
            elif kind == "control" and payload.get("action") == "toggle":
                cat = payload.get("cat")
                if cat in CATEGORIES:
                    self.toggle(cat)
            elif kind == "control" and payload.get("action") == "pomodoro":
                self.remote_set_pomodoro(bool(payload.get("on")))

    def note_client_activity(self, idle_seconds_value):
        """客户端报告了用户活跃：其空闲时长小于阈值即视为人在。"""
        if isinstance(idle_seconds_value, (int, float)) \
                and 0 <= idle_seconds_value < IDLE_PAUSE_SECONDS:
            self.last_client_active_ts = datetime.now().timestamp()

    def remote_set_pomodoro(self, on):
        """手机端远程开关番茄钟，行为与本地菜单开关保持一致。"""
        if on and not self.pom_var.get():
            self.pom_var.set(True)
            self.settings["pomodoro"] = True
            self.save()
            if not self.running_cat:
                self.toggle(CATEGORIES[0])
            if self.pom_state is None and self.running_cat:
                self.pom_state = "focus"
                self.pom_end_ts = datetime.now().timestamp() + POMODORO_FOCUS
            self.refresh()
            self.toast("番茄钟", "手机端已开启番茄钟：25 分钟专注 + 5 分钟休息。")
        elif not on and self.pom_var.get():
            self.pom_var.set(False)
            self.settings["pomodoro"] = False
            self.pom_state = None
            self.save()
            self.refresh()
            self.toast("番茄钟", "手机端已关闭番茄钟。")

    def sync_now(self):
        """手动双向同步一次。"""
        addr = self.settings.get("host_addr")
        if addr and not self.sync_server:
            try:
                st = self.fetch_state(addr)
                merge_daily(self.data["daily"], st.get("daily", {}))
                self.post("/merge", {"daily": self.data["daily"],
                                     "goals": self.settings.get("goals", {}),
                                     "goals_ts": self.settings.get("goals_updated_ts", 0)})
                self.apply_remote_goals(st.get("goals"), st.get("goals_ts"))
                self.apply_remote_todos(st.get("todos"), st.get("todos_ts"))
                self.save()
                self.refresh()
                messagebox.showinfo("立即同步", f"已与 {addr} 完成双向同步。", parent=self.dlg)
            except OSError:
                messagebox.showerror("立即同步", f"无法连接 {addr}。", parent=self.dlg)
        elif self.sync_server:
            messagebox.showinfo("立即同步",
                                "主机模式无需手动同步，客户端会自动推送合并。", parent=self.dlg)
        else:
            messagebox.showinfo("立即同步", "请先通过「连接主机…」指定主机 IP。", parent=self.dlg)

    def remote_toggle(self, cat):
        try:
            self.post("/control", {"action": "toggle", "cat": cat})
            self.toast("局域网同步", f"已通知主机切换「{cat}」。")
        except OSError:
            messagebox.showerror("局域网同步",
                                 "主机不可达，操作未执行。"
                                 "\n可用「立即同步」前先检查网络。", parent=self.dlg)

    # ---------------- 自动更新 ----------------

    def silent_update_check(self):
        """启动 15 秒后静默检查一次，有新版仅提示，由用户决定。"""
        self._update_worker(lambda res: self.toast(
            "发现新版本",
            f"当前 v{VERSION}，最新 v{res[0]}。\n"
            "右键 →「检查更新」可升级（自动重启）。")
            if res else None)

    def check_update(self):
        """手动检查：一次确认后更新并自动重启。"""
        def done(res):
            if res is None:
                messagebox.showinfo("检查更新", "检查失败：无法访问 GitHub，"
                                    "请检查网络。", parent=self.dlg)
            elif not res:
                messagebox.showinfo("检查更新",
                                    f"已是最新版本 v{VERSION}。", parent=self.dlg)
            else:
                ver, text = res
                if not messagebox.askyesno(
                        "检查更新",
                        f"发现新版本 v{ver}（当前 v{VERSION}）。\n"
                        "更新后程序将自动重启，是否继续？", parent=self.dlg):
                    return
                if apply_update(text):
                    self.toast("检查更新",
                               f"已更新到 v{ver}，程序即将自动重启…",
                               duration_ms=1500)
                    restart_app()
                    self.on_close()
                else:
                    messagebox.showerror("检查更新",
                                         "更新失败：文件写入被占用，"
                                         "请手动重启后重试。", parent=self.dlg)
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

    # ---------------- 每日待办 ----------------

    def today_todos(self):
        """今天的待办列表；新一天自动把昨天未完成项携入今天。"""
        todos = self.data["todos"]
        today = today_str()
        if today not in todos:
            yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
            pending = [dict(item) for item in todos.get(yesterday, [])
                       if not item.get("done")]
            todos[today] = pending
            self.save()
        return todos[today]

    def toggle_todo_visible(self):
        self.settings["todo_visible"] = not self.settings.get("todo_visible", True)
        self.save()
        self.apply_ui_size()

    def _todo_touch(self):
        self.settings["todos_updated_ts"] = datetime.now().timestamp()
        self.save()
        self.apply_ui_size()  # 尺寸随条数变化，内部触发重绘

    def toggle_todo_done(self, idx):
        items = self.today_todos()
        if idx < len(items):
            items[idx]["done"] = not items[idx]["done"]
            self._todo_touch()

    def todo_menu_popup(self, event, idx):
        menu = tk.Menu(self, tearoff=0)
        menu.add_command(label="添加待办…", command=self.add_todo_item)
        menu.add_command(label="编辑…", command=lambda: self.edit_todo_item(idx))
        menu.add_command(label="删除", command=lambda: self.delete_todo_item(idx))
        menu.add_command(label="清除已完成", command=self.clear_done_todos)
        menu.tk_popup(event.x_root, event.y_root)

    def todo_area_menu(self, event):
        """待办区/标题行的右键菜单（无条目时也能添加）。"""
        menu = tk.Menu(self, tearoff=0)
        menu.add_command(label="添加待办…", command=self.add_todo_item)
        menu.add_command(label="清除已完成", command=self.clear_done_todos)
        menu.add_separator()
        menu.add_command(label="展开/收起", command=self.toggle_todo_visible)
        menu.tk_popup(event.x_root, event.y_root)

    def add_todo_item(self):
        text = simpledialog.askstring("添加待办", "待办内容：", parent=self.dlg)
        if text and text.strip():
            self.today_todos().append({"text": text.strip(), "done": False})
            self._todo_touch()

    def edit_todo_item(self, idx):
        items = self.today_todos()
        if idx < len(items):
            text = simpledialog.askstring("编辑待办", "修改内容：",
                                          initialvalue=items[idx]["text"],
                                          parent=self.dlg)
            if text and text.strip():
                items[idx]["text"] = text.strip()
                self._todo_touch()

    def delete_todo_item(self, idx):
        items = self.today_todos()
        if idx < len(items):
            items.pop(idx)
            self._todo_touch()

    def clear_done_todos(self):
        items = self.today_todos()
        self.data["todos"][today_str()] = [i for i in items if not i.get("done")]
        self._todo_touch()

    def apply_remote_todos(self, remote, ts):
        """待办同步：时间戳新者胜，整表替换。"""
        if not isinstance(remote, dict) or not isinstance(ts, (int, float)):
            return False
        if ts <= self.settings.get("todos_updated_ts", 0):
            return False
        clean = {}
        for d, items in remote.items():
            if isinstance(items, list):
                clean[d] = [{"text": str(i.get("text", ""))[:80],
                             "done": bool(i.get("done", False))}
                            for i in items if isinstance(i, dict)]
        self.data["todos"] = clean
        self.settings["todos_updated_ts"] = ts
        return True

    # ---------------- 皮肤插件 ----------------

    def skins_dir(self):
        return os.path.join(os.path.dirname(os.path.abspath(__file__)), "skins")

    def load_skins(self):
        """扫描 skins/ 目录下的 *.json 皮肤文件，返回合法的皮肤列表。"""
        skins = []
        d = self.skins_dir()
        try:
            files = sorted(f for f in os.listdir(d) if f.lower().endswith(".json"))
        except OSError:
            return skins
        for fname in files:
            try:
                with open(os.path.join(d, fname), "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data.get("colors"), dict) or data.get("image"):
                    skins.append({"file": fname,
                                  "name": data.get("name") or fname[:-5],
                                  "dir": d,
                                  "data": data})
            except (OSError, json.JSONDecodeError, UnicodeDecodeError):
                continue  # 坏文件直接跳过
        return skins

    def apply_skin_by_name(self, filename):
        for s in self.load_skins():
            if s["file"] == filename:
                return apply_skin(s["data"], s["dir"])
        return False

    def set_skin(self, filename):
        if filename:
            self.apply_skin_by_name(filename)
        else:  # 恢复默认配色
            g = globals()
            for gname, v in _DEFAULTS.items():
                g[gname] = v
            COLORS.clear()
            COLORS.update(_DEFAULT_CATS)
        self.settings["skin"] = filename
        self.save()
        self.rebuild_ui()
        name = next((s["name"] for s in self.load_skins()
                     if s["file"] == filename), "默认")
        self.toast("皮肤", f"已切换到「{name}」。")

    def rebuild_ui(self):
        """换肤后重建悬浮条控件（self 上的拖动/缩放绑定先解绑防重复）。"""
        for seq in ("<Button-1>", "<B1-Motion>", "<ButtonRelease-1>",
                    "<Control-MouseWheel>"):
            self.unbind(seq)
        keep = {id(self.dlg), id(getattr(self, "mini", None))}
        for w in list(self.winfo_children()):
            if id(w) in keep or getattr(w, "_is_toast", False):
                continue
            w.destroy()
        try:
            self.menu.destroy()
        except (KeyError, tk.TclError):
            pass
        self._todo_rows = None   # 待办槽位随新配色重建
        self._topmost_done = set()
        self.configure(bg=BG)    # 根窗口底色不随 build_ui 重建，需单独刷新
        self.build_ui()
        self.apply_ui_size()
        self.refresh()

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
        if "goals" in ext_settings:
            # 外部改了目标：打上时间戳，让局域网同步把这次修改传播出去
            self.settings["goals_updated_ts"] = max(
                self.settings.get("goals_updated_ts", 0),
                ext_settings.get("goals_updated_ts", 0),
                datetime.now().timestamp() if changed else 0)
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
        if not self.locked and not self.settings.get("mini"):
            self.data["geometry"] = [self.winfo_x(), self.winfo_y()]
        self.settings["mini"] = False  # 下次启动恢复完整悬浮条
        if getattr(self, "mini", None):
            self.mini.destroy()
        self.save()
        self.destroy()


if __name__ == "__main__":
    app = TimeTracker()
    app.mainloop()
