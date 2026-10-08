"""What differs between Windows, macOS, and Linux for the dashboard window.

Notifications, the clock format, and placing the fullscreen window. The
tuner itself does not depend on any of this.
"""

import platform
import shutil
import subprocess
from datetime import datetime
from xml.sax.saxutils import escape as xml_escape

APP_NAME = "Groundhog Gamma Tuner"


def notify(title, message, system=None):
    """A local desktop notification. Nothing happens where none is available.

    Windows shows a toast, macOS a Notification Center banner through
    osascript, and Linux whatever notify-send reaches (libnotify).
    """
    system = system or platform.system()
    title = str(title or APP_NAME).replace("\r", " ").replace("\n", " ")
    message = str(message or "").replace("\r", " ").replace("\n", " ")
    try:
        if system == "Windows":
            _windows_toast(title, message)
        elif system == "Darwin":
            _quiet(["osascript", "-e", _apple_notification(title, message)])
        elif shutil.which("notify-send"):
            _quiet(["notify-send", "--app-name", APP_NAME, title, message])
    except OSError:
        return


def _apple_notification(title, message):
    """AppleScript for one banner. Quotes and backslashes are escaped."""

    def quoted(text):
        return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'

    return f"display notification {quoted(message)} with title {quoted(title)}"


def _quiet(command):
    subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _windows_toast(title, message):
    """A local Windows toast through PowerShell."""
    heading = xml_escape(
        str(title or "Groundhog Gamma Tuner").replace("\r", " ").replace("\n", " ")
    )
    body = xml_escape(str(message or "").replace("\r", " ").replace("\n", " "))
    toast_xml = (
        '<toast><visual><binding template="ToastGeneric">'
        f"<text>{heading}</text><text>{body}</text>"
        "</binding></visual></toast>"
    )
    script = (
        """
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null
$xml = New-Object Windows.Data.Xml.Dom.XmlDocument
$xml.LoadXml(@'
%s
'@)
$toast = [Windows.UI.Notifications.ToastNotification]::new($xml)
$app = '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\\WindowsPowerShell\\v1.0\\powershell.exe'
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($app).Show($toast)
"""
        % toast_xml
    )
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    subprocess.Popen(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=flags,
    )


def format_local_time(moment=None):
    """Short time in the Windows clock format. Other systems keep HH:MM:SS."""
    moment = moment or datetime.now()
    if platform.system() != "Windows":
        return moment.strftime("%H:%M:%S")
    try:
        formatted = _windows_short_time(moment)
    except (OSError, AttributeError):
        formatted = ""
    return formatted or moment.strftime("%H:%M:%S")


def _windows_short_time(moment):
    """User short time via GetTimeFormatEx. TIME_NOSECONDS matches the taskbar clock."""
    import ctypes
    from ctypes import wintypes

    class SystemTime(ctypes.Structure):
        _fields_ = [
            ("wYear", wintypes.WORD),
            ("wMonth", wintypes.WORD),
            ("wDayOfWeek", wintypes.WORD),
            ("wDay", wintypes.WORD),
            ("wHour", wintypes.WORD),
            ("wMinute", wintypes.WORD),
            ("wSecond", wintypes.WORD),
            ("wMilliseconds", wintypes.WORD),
        ]

    system_time = SystemTime(
        moment.year,
        moment.month,
        (moment.weekday() + 1) % 7,
        moment.day,
        moment.hour,
        moment.minute,
        moment.second,
        moment.microsecond // 1000,
    )
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    get_time = kernel.GetTimeFormatEx
    get_time.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.POINTER(SystemTime),
        wintypes.LPCWSTR,
        wintypes.LPWSTR,
        ctypes.c_int,
    ]
    get_time.restype = ctypes.c_int
    buffer = ctypes.create_unicode_buffer(64)
    # TIME_NOSECONDS: the taskbar short time, without a forced seconds field.
    written = get_time(
        None, 0x00000002, ctypes.byref(system_time), None, buffer, len(buffer)
    )
    if written <= 0:
        return ""
    return buffer.value


def rect_covering_monitor(monitor, inset):
    """(x, y, width, height) whose visible frame covers the monitor.

    ``monitor`` is (left, top, right, bottom). ``inset`` is the invisible
    frame on (left, top, right, bottom).
    """
    left, top, right, bottom = monitor
    inset_left, inset_top, inset_right, inset_bottom = inset
    return (
        left - inset_left,
        top - inset_top,
        (right - left) + inset_left + inset_right,
        (bottom - top) + inset_top + inset_bottom,
    )


def frame_inset(window_rect, visible_rect):
    """Invisible frame on each edge, as (left, top, right, bottom)."""
    left, top, right, bottom = window_rect
    visible_left, visible_top, visible_right, visible_bottom = visible_rect
    return (
        visible_left - left,
        visible_top - top,
        right - visible_right,
        bottom - visible_bottom,
    )


def snap_fullscreen_window(window):
    """Size the borderless window to the monitor. A failure leaves fullscreen on."""
    try:
        native = window.native

        def place():
            _place_on_monitor(native)

        _run_on_window_thread(native, place)
    except Exception:
        return


def _run_on_window_thread(native, action):
    if getattr(native, "InvokeRequired", False):
        from System import Func, Type

        native.Invoke(Func[Type](action))
        return
    action()


def _window_handle(native):
    handle = native.Handle
    to_int64 = getattr(handle, "ToInt64", None)
    if callable(to_int64):
        return to_int64()
    return int(handle)


def _place_on_monitor(native):
    """Drop the maximized inset, then cover the monitor with square corners."""
    from System.Windows.Forms import FormWindowState

    native.WindowState = FormWindowState.Normal
    hwnd = _window_handle(native)
    user32, dwmapi, rect_type, monitor_info = _win32()
    monitor = _monitor_rect(user32, hwnd, monitor_info)
    _move_window(user32, hwnd, rect_covering_monitor(monitor, (0, 0, 0, 0)))
    _set_square_frame(dwmapi, hwnd)
    visible = _extended_frame(dwmapi, hwnd, rect_type)
    if visible is None:
        return
    window_rect = _window_rect(user32, hwnd, rect_type)
    inset = tuple(max(0, edge) for edge in frame_inset(window_rect, visible))
    if any(inset):
        _move_window(user32, hwnd, rect_covering_monitor(monitor, inset))
        _set_square_frame(dwmapi, hwnd)


def _win32():
    import ctypes
    from ctypes import wintypes

    class Rect(ctypes.Structure):
        _fields_ = [
            ("left", ctypes.c_long),
            ("top", ctypes.c_long),
            ("right", ctypes.c_long),
            ("bottom", ctypes.c_long),
        ]

    class MonitorInfo(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD),
            ("rcMonitor", Rect),
            ("rcWork", Rect),
            ("dwFlags", wintypes.DWORD),
        ]

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
    user32.MonitorFromWindow.restype = wintypes.HMONITOR
    user32.GetMonitorInfoW.argtypes = [wintypes.HMONITOR, ctypes.POINTER(MonitorInfo)]
    user32.GetMonitorInfoW.restype = wintypes.BOOL
    user32.SetWindowPos.argtypes = [
        wintypes.HWND,
        wintypes.HWND,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        wintypes.UINT,
    ]
    user32.SetWindowPos.restype = wintypes.BOOL
    user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(Rect)]
    user32.GetWindowRect.restype = wintypes.BOOL

    dwmapi = ctypes.WinDLL("dwmapi", use_last_error=True)
    attribute_args = [
        wintypes.HWND,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    dwmapi.DwmSetWindowAttribute.argtypes = attribute_args
    dwmapi.DwmSetWindowAttribute.restype = ctypes.c_long
    dwmapi.DwmGetWindowAttribute.argtypes = attribute_args
    dwmapi.DwmGetWindowAttribute.restype = ctypes.c_long
    return user32, dwmapi, Rect, MonitorInfo


def _monitor_rect(user32, hwnd, monitor_info):
    import ctypes

    monitor = user32.MonitorFromWindow(hwnd, 2)
    if not monitor:
        raise OSError("The fullscreen window has no monitor.")
    info = monitor_info()
    info.cbSize = ctypes.sizeof(info)
    if not user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
        raise OSError("GetMonitorInfoW failed.")
    rect = info.rcMonitor
    return (rect.left, rect.top, rect.right, rect.bottom)


def _move_window(user32, hwnd, placed):
    import ctypes

    x, y, width, height = placed
    # SWP_FRAMECHANGED | SWP_SHOWWINDOW. HWND_TOP is 0.
    moved = user32.SetWindowPos(
        hwnd, 0, int(x), int(y), int(width), int(height), 0x0060
    )
    if not moved:
        raise ctypes.WinError(ctypes.get_last_error())


def _set_square_frame(dwmapi, hwnd):
    # 33 is DWMWA_WINDOW_CORNER_PREFERENCE, 1 is DWMWCP_DONOTROUND.
    # 34 is DWMWA_BORDER_COLOR, 0xFFFFFFFE is DWMWA_COLOR_NONE.
    _set_dwm_dword(dwmapi, hwnd, 33, 1)
    _set_dwm_dword(dwmapi, hwnd, 34, 0xFFFFFFFE)


def _set_dwm_dword(dwmapi, hwnd, attribute, value):
    import ctypes

    packed = ctypes.c_uint(value)
    dwmapi.DwmSetWindowAttribute(
        hwnd, attribute, ctypes.byref(packed), ctypes.sizeof(packed)
    )


def _window_rect(user32, hwnd, rect_type):
    import ctypes

    rect = rect_type()
    if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        raise OSError("GetWindowRect failed.")
    return (rect.left, rect.top, rect.right, rect.bottom)


def _extended_frame(dwmapi, hwnd, rect_type):
    """Visible frame from DWM, or None when the attribute is unavailable."""
    import ctypes

    rect = rect_type()
    # DWMWA_EXTENDED_FRAME_BOUNDS
    code = dwmapi.DwmGetWindowAttribute(
        hwnd, 9, ctypes.byref(rect), ctypes.sizeof(rect)
    )
    if code != 0:
        return None
    return (rect.left, rect.top, rect.right, rect.bottom)
