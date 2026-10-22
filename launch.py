"""Entry point for the packaged build (and for `python launch.py`).

Double-clicking the executable opens the window. Passing arguments instead
routes to the command line interface, which is what the window is a front end
for; that also makes the packaged build scriptable, and keeps one code path
for both.
"""

import sys

if len(sys.argv) > 1:
    from pathlib import Path

    # A windowed build has no console: sys.stdout/stderr are None, and any
    # print() inside the runtime would crash it. Send them to the log folder.
    if sys.stdout is None or sys.stderr is None:
        from gt.config import LOG_DIR

        LOG_DIR.mkdir(parents=True, exist_ok=True)
        handle = open(Path(LOG_DIR) / "console.log", "w", encoding="utf-8", buffering=1)
        sys.stdout = sys.stdout or handle
        sys.stderr = sys.stderr or handle
    from gt.cli import main

    # A windowed build would otherwise pop a blocking error dialog for a
    # failure that belongs in the log: nobody is watching a scripted run, so
    # the process would sit there forever instead of exiting non-zero.
    try:
        code = main(sys.argv[1:])
    except SystemExit:
        raise
    except Exception:
        import traceback

        traceback.print_exc()
        code = 1
    raise SystemExit(code)

from gt.gui import main as gui_main

raise SystemExit(gui_main())
