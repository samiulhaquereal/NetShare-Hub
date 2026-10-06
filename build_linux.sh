#!/bin/bash
echo "Installing PyInstaller..."
python3 -m pip install pyinstaller

echo ""
echo "Building NetShare Hub Executable for Linux..."
# Note: On Linux, PyInstaller uses a colon (:) instead of a semicolon (;) to separate paths
python3 -m PyInstaller --noconfirm --onefile --noconsole --icon "static/icon.png" --add-data "templates:templates" --add-data "static:static" --add-binary "ngrok:." --name "NetShare Hub" server.py

echo ""
echo "===================================================="
echo "Build Complete!"
echo "You can find your executable in the 'dist' folder."
echo "===================================================="
echo "===================================================="


#chmod +x build_linux.sh
#./build_linux.sh
