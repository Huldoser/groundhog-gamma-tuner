"""Start the Gamma 601 tuner window.

The window process has to be 64-bit Python. pywebview's .NET helper does
not load in the ARM64 build, so an ARM64 process restarts in the 64-bit one.
"""

import glob
import os
import subprocess
import sys
import sysconfig
import traceback

X64_PYTHON_MESSAGE = (
    "Groundhog Gamma Tuner needs the 64-bit Python from python.org "
    "(Windows installer (64-bit)).\n\n"
    "The ARM64 installer cannot open this window.\n\n"
    "Install the 64-bit Python, then install the packages with that interpreter:\n"
    "python.exe -m pip install -r requirements.txt"
)


def python_paths_from_py_list(text):
    """Return python.exe paths from `py -0p` output, in listed order.

    A path may contain spaces. A default-install star after the tag or the
    path is left out.
    """
    paths = []
    seen = set()
    marker = "python.exe"
    for line in text.splitlines():
        lower = line.lower()
        end_at = lower.rfind(marker)
        if end_at == -1:
            continue
        end = end_at + len(marker)
        start = _drive_path_start(line[:end_at])
        if start is None:
            continue
        path = line[start:end].strip().strip('"')
        key = os.path.normcase(path)
        if key in seen:
            continue
        seen.add(key)
        paths.append(path)
    return paths


def interpreter_candidates(py_list_output, local_paths):
    """List py launcher interpreters first, then any other local python.exe."""
    paths = python_paths_from_py_list(py_list_output)
    seen = {os.path.normcase(path) for path in paths}
    for path in local_paths:
        key = os.path.normcase(path)
        if key in seen:
            continue
        seen.add(key)
        paths.append(path)
    return paths


def choose_amd64_python(candidates, machine_for):
    """Return the first candidate whose machine is AMD64."""
    for path in candidates:
        machine = machine_for(path)
        if str(machine).strip().upper() == "AMD64":
            return path
    return None


def launch_executable(python_exe, current_executable):
    """Keep a windowed start on pythonw.exe beside the 64-bit interpreter."""
    if _executable_name(current_executable).startswith("pythonw"):
        return _parent_dir(python_exe) + "\\pythonw.exe"
    return python_exe


def arch_from_platform(value):
    """Map a sysconfig platform tag to the interpreter build.

    ``win-amd64`` and ``win-arm64`` name the Python build. A host CPU
    label such as ``ARM64`` stays unmapped.
    """
    text = str(value).strip().lower()
    if text == "win-amd64":
        return "AMD64"
    if text == "win-arm64":
        return "ARM64"
    return ""


def interpreter_arch():
    """Architecture of this Python build, not the tablet CPU."""
    return arch_from_platform(sysconfig.get_platform())


def ensure_amd64_python():
    """Restart under 64-bit Python when this process is the ARM64 build."""
    if sys.platform != "win32" or interpreter_arch() != "ARM64":
        return
    chosen = choose_amd64_python(discover_interpreters(), machine_of)
    launch = launch_executable(chosen, sys.executable) if chosen else ""
    if launch and not os.path.isfile(launch):
        launch = chosen
    if not launch or _same_file(launch, sys.executable):
        _missing_x64_python()
    try:
        os.execv(launch, [launch, *sys.argv])
    except OSError:
        _missing_x64_python()


def _missing_x64_python():
    print(X64_PYTHON_MESSAGE, file=sys.stderr)
    show_windows_dialog(X64_PYTHON_MESSAGE)
    raise SystemExit(1)


def discover_interpreters():
    return interpreter_candidates(read_py_list(), local_python_exes())


def read_py_list():
    try:
        completed = subprocess.run(
            ["py", "-0p"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
            **_quiet_subprocess(),
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return completed.stdout or ""


def local_python_exes():
    """Per-user installs, then an all-users install under Program Files."""
    patterns = []
    local = os.environ.get("LOCALAPPDATA", "")
    if local:
        patterns.append(os.path.join(local, "Programs", "Python", "Python*", "python.exe"))
    program_files = os.environ.get("ProgramFiles", "")
    if program_files:
        patterns.append(os.path.join(program_files, "Python*", "python.exe"))
        patterns.append(os.path.join(program_files, "Python", "Python*", "python.exe"))
    found = []
    seen = set()
    for pattern in patterns:
        for path in sorted(glob.glob(pattern)):
            key = os.path.normcase(path)
            if key in seen:
                continue
            seen.add(key)
            found.append(path)
    return found


def machine_of(executable):
    try:
        completed = subprocess.run(
            [executable, "-c", "import sysconfig; print(sysconfig.get_platform())"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
            **_quiet_subprocess(),
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    if completed.returncode != 0:
        return ""
    return arch_from_platform(completed.stdout)


def show_windows_dialog(message):
    """Show a message box. pythonw has no console, so a print never appears."""
    if sys.platform != "win32":
        return
    try:
        import ctypes

        message_box = ctypes.windll.user32.MessageBoxW
        message_box.argtypes = [
            ctypes.c_void_p,
            ctypes.c_wchar_p,
            ctypes.c_wchar_p,
            ctypes.c_uint,
        ]
        message_box.restype = ctypes.c_int
        message_box(None, str(message), "Groundhog Gamma Tuner", 0x10)
    except (AttributeError, OSError):
        return


def main():
    ensure_amd64_python()
    from dashboard import TunerDashboard

    try:
        TunerDashboard().run()
    except KeyboardInterrupt:
        print("\nProgram interrupted and exiting cleanly...")
    except SystemExit as exc:
        if isinstance(exc.code, str) and exc.code.strip():
            show_windows_dialog(exc.code)
        raise
    except Exception as exc:
        print(traceback.format_exc(), file=sys.stderr)
        show_windows_dialog(f"The dashboard window failed to open.\n\n{exc}")
        raise


def _executable_name(path):
    return path.replace("/", "\\").rsplit("\\", 1)[-1].lower()


def _parent_dir(path):
    normalized = path.replace("/", "\\")
    if "\\" not in normalized:
        return ""
    return normalized.rsplit("\\", 1)[0]


def _drive_path_start(prefix):
    for index in range(len(prefix) - 2, -1, -1):
        if prefix[index].isalpha() and prefix[index + 1 : index + 3] == ":\\":
            return index
    return None


def _same_file(left, right):
    return os.path.normcase(os.path.abspath(left)) == os.path.normcase(os.path.abspath(right))


def _quiet_subprocess():
    if sys.platform == "win32":
        return {"creationflags": subprocess.CREATE_NO_WINDOW}
    return {}


if __name__ == "__main__":
    main()
