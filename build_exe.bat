@echo off
echo Installing PyInstaller...
python -m pip install pyinstaller

echo.
echo Building NetShare Hub Executable...
python -m PyInstaller --noconfirm --onefile --noconsole --icon "icon.ico" --add-data "templates;templates" --add-data "static;static" --add-binary "ngrok.exe;." --name "NetShare Hub" server.py

echo.
echo ====================================================
echo Build Complete!
echo You can find your executable in the "dist" folder.
echo ====================================================
echo ====================================================
