"""Double-click to open Monitor Lights without a console window.

Also the entry point of the ready-made "Monitor Lights.exe".
"""

import os
import sys
import traceback

FROZEN = getattr(sys, "frozen", False)
HERE = os.path.dirname(sys.executable if FROZEN else os.path.abspath(__file__))
if not FROZEN:
    sys.path.insert(0, HERE)
os.chdir(HERE)

try:
    from hue_controller import main

    sys.exit(main())
except SystemExit:
    raise
except BaseException:
    details = traceback.format_exc()
    if "No module named" in details and not FROZEN:
        details = ("A required package is missing. Run install.bat once (or: py -m pip install -r "
                   "requirements.txt).\n\n" + details)
    try:
        log_dir = os.path.join(os.environ.get("APPDATA", HERE), "Monitor Lights", "logs")
        os.makedirs(log_dir, exist_ok=True)
        with open(os.path.join(log_dir, "startup-error.txt"), "w", encoding="utf-8") as handle:
            handle.write(details)
    except OSError:
        pass
    import ctypes

    ctypes.windll.user32.MessageBoxW(None, "Monitor Lights could not start:\n\n" + details[-1500:],
                                     "Monitor Lights", 0x10)
