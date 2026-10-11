import subprocess
import sys

print("Testing subprocess monkeypatch for window hiding...")

if sys.platform == "win32":
    _original_popen = subprocess.Popen
    def _silent_popen(*args, **kwargs):
        if "creationflags" not in kwargs:
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
        return _original_popen(*args, **kwargs)
    subprocess.Popen = _silent_popen

proc = subprocess.run(["cmd", "/c", "echo", "hello"], capture_output=True, text=True)
print("Return code:", proc.returncode)
print("Output:", proc.stdout.strip())
