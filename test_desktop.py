import sys
import types
import unittest
from datetime import datetime
from unittest import mock

import config
import desktop


class NotifyTests(unittest.TestCase):
    def test_windows_shows_a_toast(self):
        with mock.patch("desktop._windows_toast") as toast:
            desktop.notify("Title", "Alpha is offline.", system="Windows")
        toast.assert_called_once_with("Title", "Alpha is offline.")

    def test_macos_uses_osascript_with_quotes_escaped(self):
        with mock.patch("desktop.subprocess.Popen") as popen:
            desktop.notify('Say "hi"', "back\\slash", system="Darwin")
        command = popen.call_args.args[0]
        self.assertEqual(command[:2], ["osascript", "-e"])
        self.assertEqual(
            command[2],
            'display notification "back\\\\slash" with title "Say \\"hi\\""',
        )

    def test_linux_uses_notify_send_when_it_is_there(self):
        with (
            mock.patch("desktop.shutil.which", return_value="/usr/bin/notify-send"),
            mock.patch("desktop.subprocess.Popen") as popen,
        ):
            desktop.notify("Title", "line one\nline two", system="Linux")
        self.assertEqual(
            popen.call_args.args[0],
            [
                "notify-send",
                "--app-name",
                desktop.APP_NAME,
                "Title",
                "line one line two",
            ],
        )

    def test_nothing_happens_without_a_notifier(self):
        with (
            mock.patch("desktop.shutil.which", return_value=None),
            mock.patch("desktop.subprocess.Popen") as popen,
        ):
            desktop.notify("Title", "Body", system="Linux")
        popen.assert_not_called()

    def test_a_failed_launch_is_quiet(self):
        with mock.patch("desktop.subprocess.Popen", side_effect=OSError("gone")):
            desktop.notify("Title", "Body", system="Darwin")


class DataDirTests(unittest.TestCase):
    def test_source_checkout_keeps_files_beside_the_scripts(self):
        self.assertEqual(
            config.data_dir(frozen=False),
            config.os.path.dirname(config.os.path.abspath(config.__file__)),
        )

    def test_packaged_app_uses_the_user_data_folder(self):
        join = config.os.path.join
        home = "/home/satoshi"
        self.assertEqual(
            config.data_dir(True, "linux", {}, home),
            join(home, ".config", "groundhog-tuner"),
        )
        self.assertEqual(
            config.data_dir(True, "linux", {"XDG_CONFIG_HOME": "/xdg"}, home),
            join("/xdg", "groundhog-tuner"),
        )
        self.assertEqual(
            config.data_dir(True, "darwin", {}, "/Users/satoshi"),
            config.os.path.join(
                "/Users/satoshi",
                "Library",
                "Application Support",
                "Groundhog Tuner",
            ),
        )
        self.assertEqual(
            config.data_dir(
                True, "win32", {"APPDATA": "C:/Users/satoshi/AppData/Roaming"}
            ),
            config.os.path.join("C:/Users/satoshi/AppData/Roaming", "Groundhog Tuner"),
        )


if __name__ == "__main__":
    unittest.main()


class _FakeFunction:
    """A DLL function: callable, and takes argtypes and restype like ctypes."""

    def __init__(self, function):
        self.function = function

    def __call__(self, *args):
        return self.function(*args)


class _FakeDll:
    """A Windows DLL whose functions are plain Python callables. Others return 0."""

    def __init__(self, **functions):
        for name, function in functions.items():
            setattr(self, name, _FakeFunction(function))

    def __getattr__(self, name):
        function = _FakeFunction(lambda *args: 0)
        setattr(self, name, function)
        return function


def _windll(**dlls):
    return mock.patch(
        "ctypes.WinDLL", side_effect=lambda name, **_: dlls[name], create=True
    )


def _fake_dotnet(invoked=None):
    """System and System.Windows.Forms as pythonnet would import them."""
    system = types.ModuleType("System")

    class Func:
        def __class_getitem__(cls, _item):
            return lambda action: ("func", action)

    system.Func = Func
    system.Type = object
    windows = types.ModuleType("System.Windows")
    forms = types.ModuleType("System.Windows.Forms")
    forms.FormWindowState = types.SimpleNamespace(Normal="normal")
    return mock.patch.dict(
        sys.modules,
        {"System": system, "System.Windows": windows, "System.Windows.Forms": forms},
    )


class WindowsToastTests(unittest.TestCase):
    def test_the_toast_escapes_its_text_and_hides_the_console(self):
        with mock.patch("desktop.subprocess.Popen") as popen:
            desktop._windows_toast("A & B", "line <one>\nline two")
        command = popen.call_args.args[0]
        self.assertEqual(command[0], "powershell.exe")
        self.assertIn("<text>A &amp; B</text>", command[-1])
        self.assertIn("<text>line &lt;one&gt; line two</text>", command[-1])
        self.assertIn("creationflags", popen.call_args.kwargs)


class WindowsClockTests(unittest.TestCase):
    def test_other_systems_keep_hours_minutes_seconds(self):
        moment = datetime(2026, 10, 8, 19, 41, 5)
        with mock.patch("desktop.platform.system", return_value="Linux"):
            self.assertEqual(desktop.format_local_time(moment), "19:41:05")

    def test_windows_uses_the_users_short_time(self):
        moment = datetime(2026, 10, 8, 19, 41, 5)
        with (
            mock.patch("desktop.platform.system", return_value="Windows"),
            mock.patch("desktop._windows_short_time", return_value="7:41 PM"),
        ):
            self.assertEqual(desktop.format_local_time(moment), "7:41 PM")

    def test_a_failed_or_empty_windows_answer_falls_back(self):
        moment = datetime(2026, 10, 8, 19, 41, 5)
        with mock.patch("desktop.platform.system", return_value="Windows"):
            with mock.patch("desktop._windows_short_time", side_effect=OSError):
                self.assertEqual(desktop.format_local_time(moment), "19:41:05")
            with mock.patch("desktop._windows_short_time", return_value=""):
                self.assertEqual(desktop.format_local_time(moment), "19:41:05")
            self.assertTrue(desktop.format_local_time())

    def test_get_time_format_fills_the_buffer(self):
        seen = {}

        def get_time(locale, flags, when, fmt, buffer, size):
            seen["flags"] = flags
            seen["hour"] = when._obj.wHour
            buffer.value = "7:41 PM"
            return len(buffer.value) + 1

        with _windll(kernel32=_FakeDll(GetTimeFormatEx=get_time)):
            text = desktop._windows_short_time(datetime(2026, 10, 8, 19, 41, 5))
        self.assertEqual(text, "7:41 PM")
        self.assertEqual(seen, {"flags": 0x00000002, "hour": 19})

    def test_a_refused_get_time_format_is_empty(self):
        refused = _FakeDll(GetTimeFormatEx=lambda *args: 0)
        with _windll(kernel32=refused):
            self.assertEqual(desktop._windows_short_time(datetime(2026, 1, 1)), "")


class FullscreenPlacementTests(unittest.TestCase):
    def setUp(self):
        user32 = _FakeDll(
            MonitorFromWindow=lambda hwnd, flags: 7,
            GetMonitorInfoW=self._monitor_info,
            SetWindowPos=self._set_window_pos,
            GetWindowRect=self._window_rect,
        )
        dwmapi = _FakeDll(
            DwmSetWindowAttribute=self._set_attribute,
            DwmGetWindowAttribute=self._get_attribute,
        )
        self.moves = []
        self.attributes = []
        self.window = (0, 0, 1920, 1080)
        self.visible = (7, 0, 1913, 1073)
        self.frame_code = 0
        patcher = _windll(user32=user32, dwmapi=dwmapi)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _monitor_info(self, monitor, info):
        rect = info._obj.rcMonitor
        rect.left, rect.top, rect.right, rect.bottom = 0, 0, 1920, 1080
        return 1

    def _set_window_pos(self, hwnd, after, x, y, width, height, flags):
        self.moves.append((x, y, width, height))
        return 1

    def _window_rect(self, hwnd, rect):
        (
            rect._obj.left,
            rect._obj.top,
            rect._obj.right,
            rect._obj.bottom,
        ) = self.window
        return 1

    def _set_attribute(self, hwnd, attribute, value, size):
        self.attributes.append((attribute, value._obj.value))
        return 0

    def _get_attribute(self, hwnd, attribute, rect, size):
        (
            rect._obj.left,
            rect._obj.top,
            rect._obj.right,
            rect._obj.bottom,
        ) = self.visible
        return self.frame_code

    def _native(self, handle=42, invoke_required=False):
        native = mock.Mock()
        native.Handle = handle
        native.InvokeRequired = invoke_required
        native.Invoke.side_effect = lambda func: func[1]()
        return native

    def test_the_window_covers_the_monitor_without_its_invisible_frame(self):
        native = self._native()
        with _fake_dotnet():
            desktop.snap_fullscreen_window(mock.Mock(native=native))
        self.assertEqual(native.WindowState, "normal")
        self.assertEqual(self.moves, [(0, 0, 1920, 1080), (-7, 0, 1934, 1087)])
        self.assertIn((33, 1), self.attributes)
        self.assertIn((34, 0xFFFFFFFE), self.attributes)

    def test_no_second_move_when_the_frame_has_no_inset(self):
        self.visible = self.window
        with _fake_dotnet():
            desktop._place_on_monitor(self._native())
        self.assertEqual(self.moves, [(0, 0, 1920, 1080)])

    def test_no_second_move_when_dwm_has_no_frame_bounds(self):
        self.frame_code = 1
        with _fake_dotnet():
            desktop._place_on_monitor(self._native())
        self.assertEqual(self.moves, [(0, 0, 1920, 1080)])

    def test_a_call_from_another_thread_runs_on_the_window_thread(self):
        native = self._native(invoke_required=True)
        with _fake_dotnet():
            desktop.snap_fullscreen_window(mock.Mock(native=native))
        native.Invoke.assert_called_once()
        self.assertEqual(len(self.moves), 2)

    def test_a_dotnet_handle_is_read_as_a_number(self):
        handle = mock.Mock()
        handle.ToInt64.return_value = 99
        self.assertEqual(desktop._window_handle(mock.Mock(Handle=handle)), 99)
        self.assertEqual(desktop._window_handle(types.SimpleNamespace(Handle=5)), 5)

    def test_a_failure_leaves_the_window_as_it_is(self):
        window = mock.Mock()
        type(window).native = mock.PropertyMock(side_effect=RuntimeError("closed"))
        desktop.snap_fullscreen_window(window)


class Win32ErrorTests(unittest.TestCase):
    def setUp(self):
        with _windll(user32=_FakeDll(), dwmapi=_FakeDll()):
            _user32, _dwmapi, self.rect, self.monitor_info = desktop._win32()

    def test_a_window_without_a_monitor_is_an_error(self):
        user32 = _FakeDll(MonitorFromWindow=lambda hwnd, flags: 0)
        with self.assertRaises(OSError):
            desktop._monitor_rect(user32, 1, self.monitor_info)
        user32 = _FakeDll(
            MonitorFromWindow=lambda hwnd, flags: 7,
            GetMonitorInfoW=lambda monitor, info: 0,
        )
        with self.assertRaises(OSError):
            desktop._monitor_rect(user32, 1, self.monitor_info)

    def test_a_refused_move_or_rect_is_an_error(self):
        user32 = _FakeDll(
            SetWindowPos=lambda *args: 0, GetWindowRect=lambda hwnd, rect: 0
        )
        with (
            mock.patch("ctypes.get_last_error", return_value=5, create=True),
            mock.patch("ctypes.WinError", side_effect=OSError, create=True),
            self.assertRaises(OSError),
        ):
            desktop._move_window(user32, 1, (0, 0, 10, 10))
        with self.assertRaises(OSError):
            desktop._window_rect(user32, 1, self.rect)
