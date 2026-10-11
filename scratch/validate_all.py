import py_compile
import os
import re

print("Validating server.py...")
py_compile.compile('server.py', doraise=True)
print("server.py is valid Python!")

print("Validating template files...")
templates = ['index.html', 'screen.html', 'files.html', 'clipboard.html', 'adb.html', 'messenger.html']
for t in templates:
    path = os.path.join('templates', t)
    with open(path, 'r', encoding='utf-8') as f:
        content = f.read()
    assert '/static/style.css' in content, f"{t} missing static/style.css link!"
    print(f"  - {t}: OK (static/style.css linked)")

print("All validations passed successfully!")
