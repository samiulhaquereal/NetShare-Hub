import sys
import subprocess

CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000) if sys.platform == "win32" else 0

if sys.platform == "win32":
    _original_Popen = subprocess.Popen
    class SilentPopen(_original_Popen):
        def __init__(self, *args, **kwargs):
            if "creationflags" not in kwargs:
                kwargs["creationflags"] = CREATE_NO_WINDOW
            super().__init__(*args, **kwargs)
    subprocess.Popen = SilentPopen

print("Testing subclassing SilentPopen...")
import asyncio.windows_utils
print("asyncio.windows_utils imported successfully!")

proc = subprocess.run(["cmd", "/c", "echo", "hello"], capture_output=True, text=True)
print("Return code:", proc.returncode)
print("Output:", proc.stdout.strip())
