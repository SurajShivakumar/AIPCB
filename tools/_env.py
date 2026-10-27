"""Where the board tools find KiCad. Windows defaults; override with environment variables (the Docker image
sets them):

    AUTOPCB_KPY    Python that can `import pcbnew`   (default: vendor/KiCAD-MCP-Server/venv/Scripts/python.exe)
    KICAD_CLI      kicad-cli executable               (default: C:\\Program Files\\KiCad\\9.0\\bin\\kicad-cli.exe)
    KICAD_SHARE    KiCad's share folder (footprints/, 3dmodels/)
"""

import os
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WINDOWS = os.name == "nt"

KPY = os.environ.get("AUTOPCB_KPY") or (
    str(ROOT / "vendor" / "KiCAD-MCP-Server" / "venv" / "Scripts" / "python.exe") if WINDOWS else "python3")
KICAD_CLI = os.environ.get("KICAD_CLI") or (
    r"C:\Program Files\KiCad\9.0\bin\kicad-cli.exe" if WINDOWS else (shutil.which("kicad-cli") or "kicad-cli"))
KICAD_SHARE = Path(os.environ.get("KICAD_SHARE") or (
    r"C:\Program Files\KiCad\9.0\share\kicad" if WINDOWS else "/usr/share/kicad"))
