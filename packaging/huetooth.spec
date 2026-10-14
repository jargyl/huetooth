# PyInstaller recipe for the ready-made Windows download.
#
#   py -m PyInstaller packaging/huetooth.spec --noconfirm
#
# Result: dist/Huetooth/ with "Huetooth.exe" (the app) and lights.exe
# (the command line), sharing one _internal folder.
import re
from pathlib import Path

from PyInstaller.utils.hooks import collect_dynamic_libs, collect_submodules
from PyInstaller.utils.win32 import versioninfo as vi

ROOT = Path(SPECPATH).parent
VERSION = re.search(r'^__version__ = "([^"]+)"', (ROOT / "hue_engine.py").read_text(encoding="utf-8"), re.M).group(1)
ICON = str(ROOT / "assets" / "app.ico")

# bleak picks its Windows backend at runtime, and the winrt packages have no hook.
HIDDEN = collect_submodules("bleak.backends.winrt") + collect_submodules("winrt")
# Not used: the app only reads its own PNGs and talks to nothing on the internet.
# (asyncio, random and ImageFont work fine without these.)
EXCLUDES = ["unittest", "pydoc", "doctest", "pdb", "lib2to3", "test", "numpy",
            "ssl", "_ssl", "_hashlib",
            "PIL._avif", "PIL.AvifImagePlugin", "PIL._webp", "PIL.WebPImagePlugin",
            "PIL._imagingcms", "PIL.ImageCms", "PIL._imagingft"]


def version_info(description, internal_name):
    numbers = tuple(int(part) for part in VERSION.split(".")) + (0,) * (4 - len(VERSION.split(".")))
    strings = [
        vi.StringStruct("CompanyName", "Huetooth"),
        vi.StringStruct("FileDescription", description),
        vi.StringStruct("FileVersion", VERSION),
        vi.StringStruct("InternalName", internal_name),
        vi.StringStruct("LegalCopyright", "MIT License"),
        vi.StringStruct("OriginalFilename", internal_name + ".exe"),
        vi.StringStruct("ProductName", "Huetooth"),
        vi.StringStruct("ProductVersion", VERSION),
    ]
    return vi.VSVersionInfo(
        ffi=vi.FixedFileInfo(filevers=numbers, prodvers=numbers),
        kids=[vi.StringFileInfo([vi.StringTable("040904B0", strings)]),
              vi.VarFileInfo([vi.VarStruct("Translation", [0x0409, 1200])])],
    )


def analysis(script, datas=()):
    return Analysis(
        [str(ROOT / script)],
        pathex=[str(ROOT)],
        binaries=collect_dynamic_libs("winrt"),
        datas=list(datas),
        hiddenimports=HIDDEN,
        excludes=EXCLUDES,
        noarchive=False,
    )


app = analysis("Huetooth.pyw", datas=[(str(ROOT / "assets"), "assets")])
cli = analysis("hue_cli.py")

app_exe = EXE(
    PYZ(app.pure), app.scripts, [],
    exclude_binaries=True, name="Huetooth", icon=ICON, console=False, upx=False,
    version=version_info("Huetooth", "Huetooth"),
)
cli_exe = EXE(
    PYZ(cli.pure), cli.scripts, [],
    exclude_binaries=True, name="lights", icon=ICON, console=True, upx=False,
    version=version_info("Huetooth command line", "lights"),
)
COLLECT(
    app_exe, app.binaries, app.datas,
    cli_exe, cli.binaries, cli.datas,
    name="Huetooth", upx=False,
)
