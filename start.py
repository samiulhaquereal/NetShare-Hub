"""Deprecated launcher — kept so old shortcuts keep working.

The separate ngrok launcher UI was merged into the main control panel
(see "Public Access (Ngrok)"). This simply starts NetShare Hub.
"""

from netshare.main import main

if __name__ == "__main__":
    main()
