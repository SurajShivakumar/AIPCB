"""Import for its side effect: on Windows, every subprocess this process starts gets no console window.

Without it, each KiCad-Python / kicad-cli / git child launched from a background job pops a window in front of
the user. subprocess.run() and check_output() go through Popen, so patching Popen covers them all.
"""

import os
import subprocess

if os.name == "nt" and not getattr(subprocess.Popen, "_autopcb_quiet", False):
    _CREATE_NO_WINDOW = 0x08000000
    _orig_init = subprocess.Popen.__init__

    def _init(self, *args, **kwargs):
        kwargs["creationflags"] = kwargs.get("creationflags", 0) | _CREATE_NO_WINDOW
        _orig_init(self, *args, **kwargs)

    subprocess.Popen.__init__ = _init
    subprocess.Popen._autopcb_quiet = True
