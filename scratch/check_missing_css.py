import os
import re

with open('scratch/orig_style.css', 'r', encoding='utf-8') as f:
    orig_css = f.read()

templates_dir = 'templates'
template_files = ['index.html', 'screen.html', 'files.html', 'clipboard.html', 'adb.html', 'messenger.html']

current_css_by_file = {}
for tf in template_files:
    path = os.path.join(templates_dir, tf)
    if os.path.exists(path):
        with open(path, 'r', encoding='utf-8') as f:
            current_css_by_file[tf] = f.read()
    else:
        current_css_by_file[tf] = ""

# Find all CSS selectors in orig_css
selectors = re.findall(r'([^{}\n]+)\s*\{', orig_css)
print(f"Total selector blocks in orig_css: {len(selectors)}")

# Group selectors by category/keyword
missing = []
for sel in set(s.strip() for s in selectors if s.strip() and not s.strip().startswith('@')):
    # Check if this selector exists in any template file
    found = any(sel in css for css in current_css_by_file.values())
    if not found:
        missing.append(sel)

print(f"Total missing selectors: {len(missing)}")
print("Missing selectors sample:")
for m in sorted(missing):
    print("  -", m)
