"""Build the shared Windows engine with paths independent of the spec directory."""
from pathlib import Path
import PyInstaller.__main__

ROOT = Path(__file__).resolve().parent.parent

if __name__ == "__main__":
    PyInstaller.__main__.run([
        "--clean", "--noconfirm", "--onedir", "--windowed", "--name", "BailianHUD",
        "--icon", str(ROOT / "assets" / "BailianHUD.ico"),
        "--add-data", f"{ROOT / 'assets'};assets",
        "--distpath", str(ROOT / "dist"),
        "--workpath", str(ROOT / "build"),
        "--specpath", str(ROOT / "build"),
        str(ROOT / "BailianHUD.py"),
    ])
