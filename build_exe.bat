@echo off
echo Installing PyInstaller...
python -m pip install pyinstaller

echo.
echo Building RetCore OnLAN Executable...
python -m PyInstaller --noconfirm --onefile --noconsole --collect-data customtkinter --icon "icon.ico" --add-data "templates;templates" --add-data "static;static" --add-binary "ngrok.exe;." --name "RetCore OnLAN" server.py

echo.
echo ====================================================
echo Build Complete!
echo You can find your executable in the "dist" folder.
echo ====================================================
echo ====================================================
