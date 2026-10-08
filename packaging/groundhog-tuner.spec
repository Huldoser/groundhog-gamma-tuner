# PyInstaller build of the dashboard window.
#   pyinstaller --noconfirm packaging/groundhog-tuner.spec
# Windows and Linux get a folder with the app inside; macOS gets a .app.
# A packaged app keeps config.json and history.db in the user's data folder
# (config.data_dir), because it cannot write inside itself.
import sys
from pathlib import Path

root = Path(SPECPATH).parent
name = "Groundhog Tuner"
icon = root / "assets" / ("app_icon.ico" if sys.platform == "win32" else "app_icon.png")

analysis = Analysis(
    [str(root / "main.py")],
    pathex=[str(root)],
    datas=[(str(root / "web"), "web"), (str(root / "assets"), "assets")],
    excludes=["tkinter"],
)
pyz = PYZ(analysis.pure)
exe = EXE(
    pyz,
    analysis.scripts,
    [],
    exclude_binaries=True,
    name=name,
    console=False,
    icon=str(icon),
)
bundle = COLLECT(exe, analysis.binaries, analysis.datas, name=name)
if sys.platform == "darwin":
    app = BUNDLE(
        bundle,
        name=f"{name}.app",
        icon=str(icon),
        bundle_identifier="io.github.huldoser.groundhog-tuner",
    )
