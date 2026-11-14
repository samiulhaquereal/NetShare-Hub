"""RetCore OnLAN launcher (kept as the entry point for run.bat / PyInstaller).

All application code lives in the ``netshare`` package — see netshare/__init__.py.

Run:  python server.py [--public --token XXX] [--headless]
"""

from netshare.main import main

if __name__ == "__main__":
    main()
