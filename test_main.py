import sys
import unittest
from unittest import mock

import main
from main import (
    arch_from_platform,
    choose_amd64_python,
    interpreter_candidates,
    launch_executable,
    python_paths_from_py_list,
)

PY_LIST = """
 -V:3.13-arm64 *  C:\\Users\\satoshi\\AppData\\Local\\Programs\\Python\\Python313-arm64\\python.exe
 -V:3.13          C:\\Program Files\\Python313\\python.exe *
"""

ARM64 = r"C:\Users\satoshi\AppData\Local\Programs\Python\Python313-arm64\python.exe"
AMD64 = r"C:\Program Files\Python313\python.exe"


class InterpreterDiscoveryTests(unittest.TestCase):
    def test_py_list_keeps_paths_with_spaces(self):
        self.assertEqual(python_paths_from_py_list(PY_LIST), [ARM64, AMD64])

    def test_local_install_is_added_after_the_py_list(self):
        local = r"C:\Users\satoshi\AppData\Local\Programs\Python\Python312\python.exe"
        self.assertEqual(
            interpreter_candidates(PY_LIST, [ARM64, local]),
            [ARM64, AMD64, local],
        )

    def test_choose_amd64_skips_an_earlier_arm64_install(self):
        machines = {ARM64: "ARM64", AMD64: "amd64"}
        chosen = choose_amd64_python(python_paths_from_py_list(PY_LIST), machines.get)
        self.assertEqual(chosen, AMD64)

    def test_choose_amd64_is_empty_when_every_install_is_arm64(self):
        self.assertIsNone(choose_amd64_python([ARM64], lambda _path: "ARM64"))

    def test_win_amd64_build_is_chosen_and_win_arm64_is_skipped(self):
        platforms = {ARM64: "win-arm64", AMD64: "win-amd64"}
        chosen = choose_amd64_python(
            [ARM64, AMD64],
            lambda path: arch_from_platform(platforms[path]),
        )
        self.assertEqual(arch_from_platform("win-amd64"), "AMD64")
        self.assertEqual(arch_from_platform("win-arm64"), "ARM64")
        self.assertEqual(chosen, AMD64)

    def test_host_arm64_label_does_not_classify_the_build(self):
        self.assertEqual(arch_from_platform("ARM64"), "")
        self.assertIsNone(
            choose_amd64_python([AMD64], lambda _path: arch_from_platform("ARM64"))
        )

    def test_windowed_start_uses_pythonw_beside_the_64_bit_interpreter(self):
        self.assertEqual(
            launch_executable(
                AMD64,
                r"C:\Users\satoshi\AppData\Local\Programs\Python\Python313-arm64\pythonw.exe",
            ),
            r"C:\Program Files\Python313\pythonw.exe",
        )
        self.assertEqual(
            launch_executable(
                AMD64,
                r"C:\Users\satoshi\AppData\Local\Programs\Python\Python313-arm64\python.exe",
            ),
            AMD64,
        )


class RelaunchTests(unittest.TestCase):
    def test_arm64_process_relaunches_with_each_argument_kept_whole(self):
        script = r"C:\Users\First Last\groundhog-tuner\main.py"
        completed = mock.Mock(returncode=3)
        with (
            mock.patch.object(sys, "platform", "win32"),
            mock.patch.object(sys, "argv", [script]),
            mock.patch.object(sys, "executable", ARM64),
            mock.patch.object(main, "interpreter_arch", return_value="ARM64"),
            mock.patch.object(main, "discover_interpreters", return_value=[AMD64]),
            mock.patch.object(main, "machine_of", return_value="AMD64"),
            mock.patch.object(main.os.path, "isfile", return_value=True),
            mock.patch.object(main.subprocess, "run", return_value=completed) as run,
        ):
            with self.assertRaises(SystemExit) as stopped:
                main.ensure_amd64_python()
        run.assert_called_once_with([AMD64, script], check=False)
        self.assertEqual(stopped.exception.code, 3)

    def test_x64_windows_and_other_systems_start_directly(self):
        for platform, arch in (("win32", "AMD64"), ("darwin", ""), ("linux", "")):
            with (
                mock.patch.object(main.sys, "platform", platform),
                mock.patch.object(main, "interpreter_arch", return_value=arch),
                mock.patch.object(main, "discover_interpreters") as discover,
                mock.patch.object(main.subprocess, "run") as run,
            ):
                self.assertIsNone(main.ensure_amd64_python())
            discover.assert_not_called()
            run.assert_not_called()


def _completed(stdout="", returncode=0):
    return mock.Mock(stdout=stdout, returncode=returncode)


class PyListEdgeTests(unittest.TestCase):
    def test_lines_without_a_drive_path_and_repeats_are_skipped(self):
        text = "\n".join(
            [
                " -V:3.13  python.exe",
                f" -V:3.13  {AMD64}",
                f" -V:3.13  {AMD64} *",
            ]
        )
        self.assertEqual(python_paths_from_py_list(text), [AMD64])

    def test_this_builds_architecture_comes_from_sysconfig(self):
        with mock.patch.object(
            main.sysconfig, "get_platform", return_value="win-arm64"
        ):
            self.assertEqual(main.interpreter_arch(), "ARM64")

    def test_path_helpers_handle_bare_names(self):
        self.assertEqual(main._parent_dir("python.exe"), "")
        self.assertIsNone(main._drive_path_start("no drive here"))


class RelaunchEdgeTests(unittest.TestCase):
    def relaunch(self, chosen, isfile=True, executable=ARM64, run=None):
        with (
            mock.patch.object(main.sys, "platform", "win32"),
            mock.patch.object(main.sys, "argv", ["main.py"]),
            mock.patch.object(main.sys, "executable", executable),
            mock.patch.object(main, "interpreter_arch", return_value="ARM64"),
            mock.patch.object(main, "discover_interpreters", return_value=[]),
            mock.patch.object(main, "choose_amd64_python", return_value=chosen),
            mock.patch.object(main.os.path, "isfile", return_value=isfile),
            mock.patch.object(
                main.subprocess, "run", **(run or {"return_value": _completed()})
            ) as ran,
            mock.patch.object(main, "show_windows_dialog") as dialog,
            mock.patch("builtins.print"),
            self.assertRaises(SystemExit) as stopped,
        ):
            main.ensure_amd64_python()
        return stopped.exception.code, ran, dialog

    def test_no_64_bit_python_explains_what_to_install(self):
        code, ran, dialog = self.relaunch(None)
        self.assertEqual(code, 1)
        ran.assert_not_called()
        dialog.assert_called_once_with(main.X64_PYTHON_MESSAGE)

    def test_the_same_interpreter_is_never_relaunched(self):
        code, ran, _dialog = self.relaunch(ARM64)
        self.assertEqual(code, 1)
        ran.assert_not_called()

    def test_a_missing_pythonw_falls_back_to_python(self):
        with mock.patch.object(
            main.sys, "executable", ARM64.replace("python.exe", "pythonw.exe")
        ):
            code, ran, _dialog = self.relaunch(
                AMD64,
                isfile=False,
                executable=ARM64.replace("python.exe", "pythonw.exe"),
            )
        self.assertEqual(code, 0)
        self.assertEqual(ran.call_args.args[0][0], AMD64)

    def test_a_relaunch_that_cannot_start_explains_what_to_install(self):
        code, _ran, dialog = self.relaunch(AMD64, run={"side_effect": OSError})
        self.assertEqual(code, 1)
        dialog.assert_called_once()


class DiscoveryTests(unittest.TestCase):
    def test_discovery_joins_the_py_list_and_local_installs(self):
        with (
            mock.patch.object(main, "read_py_list", return_value=PY_LIST),
            mock.patch.object(main, "local_python_exes", return_value=[AMD64]),
        ):
            self.assertEqual(main.discover_interpreters(), [ARM64, AMD64])

    def test_the_py_launcher_list_or_nothing(self):
        with mock.patch.object(
            main.subprocess, "run", return_value=_completed(PY_LIST)
        ):
            self.assertEqual(main.read_py_list(), PY_LIST)
        with mock.patch.object(main.subprocess, "run", return_value=_completed(None)):
            self.assertEqual(main.read_py_list(), "")
        with mock.patch.object(main.subprocess, "run", side_effect=OSError):
            self.assertEqual(main.read_py_list(), "")

    def test_local_installs_are_found_once_each(self):
        environ = {"LOCALAPPDATA": "L", "ProgramFiles": "P"}

        def glob(pattern):
            return {
                main.os.path.join("L", "Programs", "Python", "Python*", "python.exe"): [
                    "L/b/python.exe",
                    "L/a/python.exe",
                ],
                main.os.path.join("P", "Python*", "python.exe"): ["L/a/python.exe"],
            }.get(pattern, [])

        with (
            mock.patch.dict(main.os.environ, environ, clear=True),
            mock.patch.object(main.glob, "glob", side_effect=glob),
        ):
            self.assertEqual(
                main.local_python_exes(), ["L/a/python.exe", "L/b/python.exe"]
            )
        with mock.patch.dict(main.os.environ, {}, clear=True):
            self.assertEqual(main.local_python_exes(), [])

    def test_each_interpreter_reports_its_own_build(self):
        with mock.patch.object(
            main.subprocess, "run", return_value=_completed("win-amd64\n")
        ):
            self.assertEqual(main.machine_of(AMD64), "AMD64")
        with mock.patch.object(
            main.subprocess, "run", return_value=_completed("", returncode=1)
        ):
            self.assertEqual(main.machine_of(AMD64), "")
        with mock.patch.object(
            main.subprocess, "run", side_effect=main.subprocess.TimeoutExpired("x", 30)
        ):
            self.assertEqual(main.machine_of(AMD64), "")

    def test_windows_hides_the_console_of_each_probe(self):
        with (
            mock.patch.object(main.sys, "platform", "win32"),
            mock.patch.object(
                main.subprocess, "CREATE_NO_WINDOW", 0x08000000, create=True
            ),
        ):
            self.assertEqual(main._quiet_subprocess(), {"creationflags": 0x08000000})
        with mock.patch.object(main.sys, "platform", "linux"):
            self.assertEqual(main._quiet_subprocess(), {})


class DialogTests(unittest.TestCase):
    def test_other_systems_show_no_dialog(self):
        with mock.patch.object(main.sys, "platform", "linux"):
            self.assertIsNone(main.show_windows_dialog("hello"))

    def test_windows_shows_a_message_box(self):
        box = mock.Mock()
        windll = mock.Mock()
        windll.user32.MessageBoxW = box
        with (
            mock.patch.object(main.sys, "platform", "win32"),
            mock.patch("ctypes.windll", windll, create=True),
        ):
            main.show_windows_dialog("hello")
        box.assert_called_once_with(None, "hello", "Groundhog Tuner", 0x10)

    def test_a_missing_message_box_is_quiet(self):
        windll = mock.Mock()
        windll.user32.MessageBoxW.side_effect = OSError
        with (
            mock.patch.object(main.sys, "platform", "win32"),
            mock.patch("ctypes.windll", windll, create=True),
        ):
            self.assertIsNone(main.show_windows_dialog("hello"))


class MainTests(unittest.TestCase):
    def run_main(self, outcome):
        app = mock.Mock()
        app.run.side_effect = outcome
        with (
            mock.patch.object(main, "ensure_amd64_python") as ensure,
            mock.patch("dashboard.TunerDashboard", return_value=app),
            mock.patch.object(main, "show_windows_dialog") as dialog,
            mock.patch("builtins.print") as printed,
        ):
            try:
                main.main()
            finally:
                ensure.assert_called_once()
        return dialog, printed

    def test_a_normal_close_and_ctrl_c_end_quietly(self):
        dialog, _printed = self.run_main(None)
        dialog.assert_not_called()
        dialog, printed = self.run_main(KeyboardInterrupt)
        dialog.assert_not_called()
        self.assertIn("interrupted", printed.call_args.args[0])

    def test_a_stop_with_a_message_shows_it(self):
        with self.assertRaises(SystemExit):
            self.run_main(SystemExit("pywebview is missing"))
        with self.assertRaises(SystemExit):
            self.run_main(SystemExit(0))

    def test_a_crash_is_shown_and_raised(self):
        with self.assertRaises(RuntimeError):
            self.run_main(RuntimeError("no WebView2"))


if __name__ == "__main__":
    unittest.main()
