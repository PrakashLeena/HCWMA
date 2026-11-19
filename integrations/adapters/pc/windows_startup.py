"""Start HCWMA with Windows (build step 18; owner toggle in the tray, off by default).

"On" puts one shortcut, `HCWMA.lnk`, in the owner's own Startup folder
(`%APPDATA%\\Microsoft\\Windows\\Start Menu\\Programs\\Startup`): it runs this repository's
`pythonw.exe -m app.desktop` with the repository as working folder. No registry value, no
scheduled task, no service, nothing for other users, no admin rights.

"Off" moves that one shortcut to the Recycle Bin (rule 9). Only a file named exactly
`HCWMA.lnk` in that folder is ever touched.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from send2trash import send2trash

SHORTCUT_NAME = "HCWMA.lnk"
REPO_ROOT = Path(__file__).resolve().parents[3]


def startup_folder() -> Path:
    appdata = os.environ.get("APPDATA")
    if not appdata:
        raise RuntimeError("the Windows APPDATA folder is not set")
    return Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"


def pythonw() -> Path:
    """The windowless Python next to the one running HCWMA (the repository's .venv)."""
    exe = Path(sys.executable)
    windowless = exe.with_name("pythonw.exe")
    return windowless if windowless.is_file() else exe


class WindowsAutostart:
    def __init__(self, folder: Path | None = None, repo_root: Path = REPO_ROOT) -> None:
        self.folder = folder or startup_folder()
        self.repo_root = repo_root

    @property
    def shortcut(self) -> Path:
        return self.folder / SHORTCUT_NAME

    def is_enabled(self) -> bool:
        return self.shortcut.is_file()

    def target(self) -> tuple[str, str, str]:
        """(program, arguments, working folder) the shortcut runs."""
        return str(pythonw()), "-m app.desktop", str(self.repo_root)

    def enable(self) -> Path:
        import comtypes
        import comtypes.client

        program, arguments, workdir = self.target()
        self.folder.mkdir(parents=True, exist_ok=True)
        comtypes.CoInitialize()
        try:
            shell = comtypes.client.CreateObject("WScript.Shell", dynamic=True)
            link = shell.CreateShortcut(str(self.shortcut))
            link.TargetPath = program
            link.Arguments = arguments
            link.WorkingDirectory = workdir
            link.Description = "HCWMA private assistant (owner turned on Start with Windows)"
            link.WindowStyle = 7  # minimized: HCWMA opens in the tray
            link.Save()
        finally:
            comtypes.CoUninitialize()
        return self.shortcut

    def disable(self) -> bool:
        """True if a shortcut was moved to the Recycle Bin."""
        if not self.is_enabled():
            return False
        send2trash(str(self.shortcut))
        return True

    def read_target(self) -> tuple[str, str, str] | None:
        """What the shortcut on disk actually runs (for the live check)."""
        if not self.is_enabled():
            return None
        import comtypes
        import comtypes.client

        comtypes.CoInitialize()
        try:
            shell = comtypes.client.CreateObject("WScript.Shell", dynamic=True)
            link = shell.CreateShortcut(str(self.shortcut))
            return str(link.TargetPath), str(link.Arguments), str(link.WorkingDirectory)
        finally:
            comtypes.CoUninitialize()
