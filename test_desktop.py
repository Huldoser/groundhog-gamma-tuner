import unittest
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
            join(home, ".config", "groundhog-gamma-tuner"),
        )
        self.assertEqual(
            config.data_dir(True, "linux", {"XDG_CONFIG_HOME": "/xdg"}, home),
            join("/xdg", "groundhog-gamma-tuner"),
        )
        self.assertEqual(
            config.data_dir(True, "darwin", {}, "/Users/satoshi"),
            config.os.path.join(
                "/Users/satoshi",
                "Library",
                "Application Support",
                "Groundhog Gamma Tuner",
            ),
        )
        self.assertEqual(
            config.data_dir(
                True, "win32", {"APPDATA": "C:/Users/satoshi/AppData/Roaming"}
            ),
            config.os.path.join(
                "C:/Users/satoshi/AppData/Roaming", "Groundhog Gamma Tuner"
            ),
        )


if __name__ == "__main__":
    unittest.main()
