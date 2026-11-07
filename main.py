#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""上班鼓励器 · 桌面挂件

HTML/CSS/SVG 负责界面与动画，pywebview 负责无边框窗口、置顶、拖动和本地配置。
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes
import json
import math
import os
import re
import sys
import threading
import time
from pathlib import Path
from typing import Any

import webview
from webview.window import FixPoint

APP_NAME = "上班鼓励器"
APP_ID = "WorkEncourager.Desktop"
WINDOW_WIDTH = 380
COLLAPSED_HEIGHT = 600
EXPANDED_HEIGHT = 1000
SCREEN_MARGIN = 24
BOTTOM_MARGIN = 56
TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")

DEFAULT_CONFIG: dict[str, Any] = {
    "normalRate": 100,
    "overtimeRate": 100,
    "defaultPayType": "normal",
    "weekdayOvertime": [6, 7],
    "payRules": [],
    "workIntervals": [
        {"start": "09:00", "end": "12:00"},
        {"start": "13:00", "end": "18:00"},
    ],
}
PAY_TYPES = {"normal", "overtime"}

DEFAULT_SETTINGS: dict[str, Any] = {
    "x": None,
    "y": None,
    "on_top": True,
}


# ---------------------------------------------------------------------------
# 路径与文件
# ---------------------------------------------------------------------------
def resource_path(filename: str) -> Path:
    """兼容源码运行和 PyInstaller 单文件打包。"""
    if getattr(sys, "frozen", False):
        base = Path(getattr(sys, "_MEIPASS", Path(sys.executable).resolve().parent))
    else:
        base = Path(__file__).resolve().parent
    return base / filename


def app_data_dir() -> Path:
    """配置存放目录；支持环境变量覆盖，方便测试和便携部署。"""
    override = os.environ.get("WORK_ENCOURAGER_DATA_DIR")
    if override:
        path = Path(override).expanduser()
    elif os.name == "nt":
        root = Path(os.environ.get("APPDATA") or Path.home())
        path = root / "WorkEncourager"
    elif sys.platform == "darwin":
        path = Path.home() / "Library" / "Application Support" / "WorkEncourager"
    else:
        root = Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config"))
        path = root / "work-encourager"

    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError:
        path = Path.home() / ".work_encourager"
        path.mkdir(parents=True, exist_ok=True)
    return path


APP_DATA_DIR = app_data_dir()
CONFIG_FILE = APP_DATA_DIR / "config.json"
SETTINGS_FILE = APP_DATA_DIR / "settings.json"


def read_json(path: Path) -> Any:
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError, ValueError):
        return None


def write_json(path: Path, data: Any) -> bool:
    """原子写入，避免进程意外退出时留下半个 JSON。"""
    temp_path = path.with_name(path.name + ".tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with temp_path.open("w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
        os.replace(temp_path, path)
        return True
    except OSError:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass
        return False


# ---------------------------------------------------------------------------
# 配置校验
# ---------------------------------------------------------------------------
def parse_time(value: Any) -> int | None:
    if not isinstance(value, str):
        return None
    match = TIME_RE.fullmatch(value)
    if not match:
        return None
    return int(match.group(1)) * 3600 + int(match.group(2)) * 60


def seconds_to_time(seconds: int) -> str:
    seconds = max(0, min(23 * 3600 + 59 * 60, int(seconds)))
    return f"{seconds // 3600:02d}:{(seconds % 3600) // 60:02d}"


def validate_rate(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    rate = float(value)
    if not math.isfinite(rate) or rate <= 0:
        return None
    if abs(rate * 100 - round(rate * 100)) > 1e-7:
        return None
    return round(rate, 2)


def validate_time_ranges(raw: Any, allow_empty: bool = False) -> list[tuple[int, int]] | None:
    if raw is None:
        return [] if allow_empty else None
    if not isinstance(raw, list) or (not raw and not allow_empty):
        return None

    ranges: list[tuple[int, int]] = []
    for item in raw:
        if not isinstance(item, dict):
            return None
        start = parse_time(item.get("start"))
        end = parse_time(item.get("end"))
        if start is None or end is None or end <= start:
            return None
        ranges.append((start, end))

    ranges.sort(key=lambda pair: pair[0])
    for index in range(1, len(ranges)):
        if ranges[index][0] < ranges[index - 1][1]:
            return None
    return ranges


def validate_weekdays(raw: Any) -> list[int] | None:
    if raw is None:
        return DEFAULT_CONFIG["weekdayOvertime"].copy()
    if not isinstance(raw, list):
        return None
    days: set[int] = set()
    for item in raw:
        if isinstance(item, bool) or not isinstance(item, int):
            return None
        if item < 1 or item > 7:
            return None
        days.add(item)
    return sorted(days)


def validate_config(value: Any) -> dict[str, Any] | None:
    """后端二次校验，兼容旧版 rate + intervals 结构并规范化为新版结构。"""
    if not isinstance(value, dict):
        return None

    normal_rate = validate_rate(
        value.get("normalRate") if value.get("normalRate") is not None else value.get("rate")
    )
    overtime_source = value.get("overtimeRate")
    if overtime_source is None:
        overtime_source = value.get("normalRate")
    if overtime_source is None:
        overtime_source = value.get("rate")
    overtime_rate = validate_rate(overtime_source)
    if normal_rate is None or overtime_rate is None:
        return None

    work_intervals = validate_time_ranges(
        value.get("workIntervals") if value.get("workIntervals") is not None else value.get("intervals")
    )
    if work_intervals is None:
        return None

    raw_rules = value.get("payRules", [])
    if not isinstance(raw_rules, list):
        return None
    parsed_rules: list[tuple[int, int, str]] = []
    for item in raw_rules:
        if not isinstance(item, dict):
            return None
        start = parse_time(item.get("start"))
        end = parse_time(item.get("end"))
        pay_type = item.get("type")
        if start is None or end is None or end <= start or pay_type not in PAY_TYPES:
            return None
        parsed_rules.append((start, end, pay_type))

    parsed_rules.sort(key=lambda rule: rule[0])
    for index in range(1, len(parsed_rules)):
        if parsed_rules[index][0] < parsed_rules[index - 1][1]:
            return None

    weekday_overtime = validate_weekdays(value.get("weekdayOvertime"))
    if weekday_overtime is None:
        return None

    default_pay_type = value.get("defaultPayType", "normal")
    if default_pay_type not in PAY_TYPES:
        return None

    return {
        "normalRate": normal_rate,
        "overtimeRate": overtime_rate,
        "defaultPayType": default_pay_type,
        "weekdayOvertime": weekday_overtime,
        "payRules": [
            {"start": seconds_to_time(start), "end": seconds_to_time(end), "type": pay_type}
            for start, end, pay_type in parsed_rules
        ],
        "workIntervals": [
            {"start": seconds_to_time(start), "end": seconds_to_time(end)}
            for start, end in work_intervals
        ],
    }


def sanitize_settings(value: Any) -> dict[str, Any]:
    settings = dict(DEFAULT_SETTINGS)
    if not isinstance(value, dict):
        return settings

    for key in ("x", "y"):
        candidate = value.get(key)
        if isinstance(candidate, int) and not isinstance(candidate, bool):
            settings[key] = candidate

    on_top = value.get("on_top")
    if isinstance(on_top, bool):
        settings["on_top"] = on_top
    return settings


# ---------------------------------------------------------------------------
# 屏幕与窗口
# ---------------------------------------------------------------------------
def available_screens() -> list[Any]:
    try:
        return list(webview.screens)
    except Exception:
        return []


def screen_for_point(x: int, y: int) -> Any | None:
    screens = available_screens()
    for screen in screens:
        if screen.x <= x < screen.x + screen.width and screen.y <= y < screen.y + screen.height:
            return screen
    return screens[0] if screens else None


def initial_position(settings: dict[str, Any]) -> tuple[int | None, int | None]:
    screens = available_screens()
    if not screens:
        return None, None

    saved_x = settings.get("x")
    saved_y = settings.get("y")
    if isinstance(saved_x, int) and isinstance(saved_y, int):
        screen = screen_for_point(saved_x, saved_y)
        if screen is not None:
            max_x = screen.x + screen.width - WINDOW_WIDTH - 8
            max_y = screen.y + screen.height - COLLAPSED_HEIGHT - 40
            return (
                max(screen.x + 8, min(saved_x, max_x)),
                max(screen.y + 8, min(saved_y, max_y)),
            )

    screen = screens[0]
    x = screen.x + screen.width - WINDOW_WIDTH - SCREEN_MARGIN
    y = screen.y + screen.height - COLLAPSED_HEIGHT - BOTTOM_MARGIN
    return max(screen.x + 8, x), max(screen.y + 8, y)


# ---------------------------------------------------------------------------
# 桌面 API
# ---------------------------------------------------------------------------
class DesktopApi:
    def __init__(self) -> None:
        # 注意：pywebview 会递归遍历 js_api 的公开属性并暴露给 JS，
        # 所以窗口、设置等对象必须使用下划线前缀，避免深递归和 COM 跨线程报错。
        self._window: webview.Window | None = None
        self._settings = sanitize_settings(read_json(SETTINGS_FILE))
        self._file_lock = threading.RLock()
        self._settings_timer: threading.Timer | None = None
        self._shut_down = False
        self._dragging = False

    # ---------- 生命周期 ----------
    def _attach_window(self, window: webview.Window) -> None:
        self._window = window

    def _on_closing(self) -> None:
        self._shut_down = True
        if self._settings_timer is not None:
            self._settings_timer.cancel()
            self._settings_timer = None
        if self._window is not None:
            try:
                self._settings["x"] = int(self._window.x)
                self._settings["y"] = int(self._window.y)
            except Exception:
                pass
        with self._file_lock:
            write_json(SETTINGS_FILE, self._settings)

    def _schedule_settings_save(self) -> None:
        with self._file_lock:
            if self._settings_timer is not None:
                self._settings_timer.cancel()
            timer = threading.Timer(0.35, self._save_settings)
            timer.daemon = True
            self._settings_timer = timer
            timer.start()

    def _save_settings(self) -> None:
        with self._file_lock:
            self._settings_timer = None
            write_json(SETTINGS_FILE, self._settings)

    def _remember_position(self) -> None:
        if self._window is None:
            return
        try:
            with self._file_lock:
                self._settings["x"] = int(self._window.x)
                self._settings["y"] = int(self._window.y)
            self._schedule_settings_save()
        except Exception:
            pass

    def _set_topmost_native(self, on_top: bool) -> bool:
        """直接调用 Win32 SetWindowPos，避免从 JS API 线程操作 WinForms 控件导致卡死。"""
        if self._window is None:
            return False
        if os.name == 'nt':
            try:
                user32 = ctypes.windll.user32
                user32.SetWindowPos.argtypes = [
                    ctypes.wintypes.HWND,
                    ctypes.wintypes.HWND,
                    ctypes.c_int,
                    ctypes.c_int,
                    ctypes.c_int,
                    ctypes.c_int,
                    ctypes.c_uint,
                ]
                user32.SetWindowPos.restype = ctypes.wintypes.BOOL
                hwnd = ctypes.wintypes.HWND(self._window.native.Handle.ToInt32())
                hwnd_insert_after = ctypes.wintypes.HWND(-1 if on_top else -2)
                flags = 0x0002 | 0x0001 | 0x0010  # NOMOVE | NOSIZE | NOACTIVATE
                result = user32.SetWindowPos(hwnd, hwnd_insert_after, 0, 0, 0, 0, flags)
                return bool(result)
            except Exception:
                return False
        try:
            self._window.on_top = on_top
            return True
        except Exception:
            return False

    # ---------- JS API ----------
    def get_ui_state(self) -> dict[str, Any]:
        return {
            "on_top": bool(self._settings.get('on_top', True)),
            "width": WINDOW_WIDTH,
            "height": COLLAPSED_HEIGHT,
        }

    def toggle_topmost(self) -> dict[str, Any]:
        if self._window is None:
            return {"on_top": False}
        current = bool(self._settings.get('on_top', True))
        next_state = not current
        applied = self._set_topmost_native(next_state)
        if applied:
            with self._file_lock:
                self._settings['on_top'] = next_state
                write_json(SETTINGS_FILE, self._settings)
        return {"on_top": next_state if applied else current, "ok": applied}

    def start_window_drag(self) -> bool:
        """启动后台拖动循环，鼠标移出窗口范围后仍可持续拖动。"""
        if self._window is None or os.name != 'nt' or self._dragging:
            return False
        try:
            hwnd = int(self._window.native.Handle.ToInt32())
        except Exception:
            return False
        self._dragging = True
        thread = threading.Thread(target=self._drag_loop, args=(hwnd,), daemon=True)
        thread.start()
        return True

    def _drag_loop(self, hwnd: int) -> None:
        """不依赖 WebView 的 mousemove：轮询光标，拖出窗口边界也不中断。"""
        user32 = ctypes.windll.user32
        winmm = ctypes.windll.winmm
        point = ctypes.wintypes.POINT()
        rect = ctypes.wintypes.RECT()
        try:
            user32.GetCursorPos.argtypes = [ctypes.POINTER(ctypes.wintypes.POINT)]
            user32.GetCursorPos.restype = ctypes.wintypes.BOOL
            user32.GetWindowRect.argtypes = [ctypes.wintypes.HWND, ctypes.POINTER(ctypes.wintypes.RECT)]
            user32.GetWindowRect.restype = ctypes.wintypes.BOOL
            user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
            user32.GetAsyncKeyState.restype = ctypes.c_short
            user32.SetWindowPos.argtypes = [
                ctypes.wintypes.HWND,
                ctypes.wintypes.HWND,
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_uint,
            ]
            user32.SetWindowPos.restype = ctypes.wintypes.BOOL

            if not user32.GetCursorPos(ctypes.byref(point)):
                return
            start_cursor_x, start_cursor_y = point.x, point.y
            if not user32.GetWindowRect(ctypes.wintypes.HWND(hwnd), ctypes.byref(rect)):
                return
            start_window_x, start_window_y = rect.left, rect.top

            width = rect.right - rect.left
            height = rect.bottom - rect.top
            moved = False
            winmm.timeBeginPeriod(1)
            while not self._shut_down:
                if not (user32.GetAsyncKeyState(0x01) & 0x8000):
                    break
                if not user32.GetCursorPos(ctypes.byref(point)):
                    break
                dx = point.x - start_cursor_x
                dy = point.y - start_cursor_y
                if not moved and abs(dx) + abs(dy) < 2:
                    time.sleep(0.008)
                    continue
                moved = True
                user32.SetWindowPos(
                    ctypes.wintypes.HWND(hwnd),
                    None,
                    start_window_x + dx,
                    start_window_y + dy,
                    width,
                    height,
                    0x0001 | 0x0004 | 0x0010,  # NOSIZE | NOZORDER | NOACTIVATE
                )
                time.sleep(0.008)
        except Exception:
            pass
        finally:
            try:
                winmm.timeEndPeriod(1)
            except Exception:
                pass
            self._dragging = False
            self._remember_position()

    def minimize_window(self) -> bool:
        if self._window is None:
            return False
        self._window.minimize()
        return True

    def close_window(self) -> bool:
        if self._window is None:
            return False
        self._window.destroy()
        return True

    def load_config(self) -> dict[str, Any] | None:
        with self._file_lock:
            return validate_config(read_json(CONFIG_FILE))

    def save_config(self, config: Any) -> bool:
        cleaned = validate_config(config)
        if cleaned is None:
            return False
        with self._file_lock:
            return write_json(CONFIG_FILE, cleaned)

    def set_config_open(self, expanded: bool, content_height: float | None = None) -> dict[str, Any]:
        """展开 / 收起配置面板时自动调整窗口高度。"""
        if self._window is None:
            return {"height": COLLAPSED_HEIGHT}

        target_height = COLLAPSED_HEIGHT
        if expanded:
            if isinstance(content_height, (int, float)) and not isinstance(content_height, bool):
                target_height = int(max(COLLAPSED_HEIGHT, min(content_height, 2400)))
            else:
                target_height = EXPANDED_HEIGHT
        screen = screen_for_point(self._window.x + WINDOW_WIDTH // 2, self._window.y + 80)
        try:
            current_height = int(self._window.height)
            current_y = int(self._window.y)
        except Exception:
            try:
                self._window.resize(WINDOW_WIDTH, target_height)
            except Exception:
                pass
            return {"height": target_height}

        if screen is not None:
            screen_top = screen.y + 16
            screen_bottom = screen.y + screen.height - 40
            available_height = max(360, screen_bottom - screen_top)
            target_height = min(target_height, available_height)

            near_bottom = current_y + current_height / 2 > screen.y + screen.height * 0.56
            if near_bottom:
                fix_point = FixPoint.SOUTH | FixPoint.WEST
                max_by_position = current_y + current_height - screen_top
            else:
                fix_point = FixPoint.NORTH | FixPoint.WEST
                max_by_position = screen_bottom - current_y
            target_height = max(COLLAPSED_HEIGHT, min(target_height, max_by_position))
        else:
            fix_point = FixPoint.NORTH | FixPoint.WEST

        try:
            self._window.resize(WINDOW_WIDTH, target_height, fix_point)
        except Exception:
            try:
                self._window.resize(WINDOW_WIDTH, target_height)
            except Exception:
                pass
        self._remember_position()
        return {"height": target_height}


# ---------------------------------------------------------------------------
# 启动
# ---------------------------------------------------------------------------
def set_windows_app_id() -> None:
    if os.name != "nt":
        return
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)  # type: ignore[attr-defined]
    except Exception:
        pass


def main() -> None:
    index_file = resource_path("index.html")
    if not index_file.exists():
        raise FileNotFoundError(f"未找到界面文件：{index_file}")

    set_windows_app_id()
    api = DesktopApi()
    start_x, start_y = initial_position(api._settings)

    window = webview.create_window(
        APP_NAME,
        url=str(index_file),
        width=WINDOW_WIDTH,
        height=COLLAPSED_HEIGHT,
        x=start_x,
        y=start_y,
        resizable=False,
        frameless=True,
        easy_drag=False,
        min_size=(WINDOW_WIDTH, COLLAPSED_HEIGHT),
        on_top=bool(api._settings.get("on_top", True)),
        background_color="#07090d",
        text_select=False,
        zoomable=False,
        confirm_close=False,
        shadow=True,
        js_api=api,
    )
    api._attach_window(window)
    window.events.closing += api._on_closing

    webview.start(debug=False)


if __name__ == "__main__":
    main()
