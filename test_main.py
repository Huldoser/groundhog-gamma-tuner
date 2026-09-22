import unittest

from main import (
    choose_amd64_python,
    interpreter_candidates,
    launch_executable,
    python_paths_from_py_list,
)

PY_LIST = """
 -V:3.13-arm64 *  C:\\Users\\huldo\\AppData\\Local\\Programs\\Python\\Python313-arm64\\python.exe
 -V:3.13          C:\\Program Files\\Python313\\python.exe *
"""

ARM64 = r"C:\Users\huldo\AppData\Local\Programs\Python\Python313-arm64\python.exe"
AMD64 = r"C:\Program Files\Python313\python.exe"


class InterpreterDiscoveryTests(unittest.TestCase):
    def test_py_list_keeps_paths_with_spaces(self):
        self.assertEqual(python_paths_from_py_list(PY_LIST), [ARM64, AMD64])

    def test_local_install_is_added_after_the_py_list(self):
        local = r"C:\Users\huldo\AppData\Local\Programs\Python\Python312\python.exe"
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

    def test_windowed_start_uses_pythonw_beside_the_64_bit_interpreter(self):
        self.assertEqual(
            launch_executable(AMD64, r"C:\Users\huldo\AppData\Local\Programs\Python\Python313-arm64\pythonw.exe"),
            r"C:\Program Files\Python313\pythonw.exe",
        )
        self.assertEqual(
            launch_executable(AMD64, r"C:\Users\huldo\AppData\Local\Programs\Python\Python313-arm64\python.exe"),
            AMD64,
        )


if __name__ == "__main__":
    unittest.main()
