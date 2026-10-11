import re
import os

with open('server.py', 'r', encoding='utf-8') as f:
    server_code = f.read()

# Find all inline event handler calls in server.py
# e.g., onclick="handlePreviewClick(this)"
inline_calls = re.findall(r'on\w+=\\?["\']([^"\'\\]+)\\?["\']', server_code)

print("Found inline handler calls in server.py:")
funcs_used = set()
for ic in inline_calls:
    # extract function name before (
    m = re.match(r'([a-zA-Z0-9_]+)\s*\(', ic.strip())
    if m:
        funcs_used.add(m.group(1))
    print("  -", ic)

print("\nExtracted function names used in server.py:")
for f in sorted(funcs_used):
    print("  *", f)

templates = ['index.html', 'screen.html', 'files.html', 'clipboard.html', 'adb.html', 'messenger.html']
for t in templates:
    path = os.path.join('templates', t)
    with open(path, 'r', encoding='utf-8') as f:
        html = f.read()
    print(f"\nChecking functions in templates/{t}:")
    for func in sorted(funcs_used):
        pattern = rf'function\s+{func}\b'
        if re.search(pattern, html):
            print(f"  [OK] {func}")
        else:
            print(f"  [MISSING] {func}")
