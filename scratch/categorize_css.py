import os
import re

with open('scratch/orig_style.css', 'r', encoding='utf-8') as f:
    orig_css = f.read()

# Parse all CSS rule blocks in orig_css
# A simple regex parser for CSS blocks:
blocks = []
pattern = re.compile(r'([^{}]+)\{([^}]*)\}', re.DOTALL)
for match in pattern.finditer(orig_css):
    selectors_str = match.group(1).strip()
    body = match.group(2).strip()
    if selectors_str.startswith('@'):
        continue
    blocks.append((selectors_str, body))

print(f"Total CSS rule blocks parsed from orig_style.css: {len(blocks)}")

# Let's categorized blocks by keywords in selectors:
file_related = []
clipboard_related = []
screen_related = []
adb_related = []
messenger_related = []
general_related = []

for sel, body in blocks:
    sel_lower = sel.lower()
    if any(k in sel_lower for k in ['.file', '#file', 'drop-zone', 'upload', 'dz-', 'preview', '#dl-multi', '#del-multi']):
        file_related.append((sel, body))
    elif any(k in sel_lower for k in ['clip', '#clip', 'badge-clip']):
        clipboard_related.append((sel, body))
    elif any(k in sel_lower for k in ['screen', 'mirror', 'hud', 'fps', 'quality', 'orientation', 'region', 'source']):
        screen_related.append((sel, body))
    elif any(k in sel_lower for k in ['adb', 'scrcpy', 'device-card', 'dc-']):
        adb_related.append((sel, body))
    elif any(k in sel_lower for k in ['msg', 'messenger', 'chat']):
        messenger_related.append((sel, body))
    else:
        general_related.append((sel, body))

print(f"File related: {len(file_related)}")
print(f"Clipboard related: {len(clipboard_related)}")
print(f"Screen related: {len(screen_related)}")
print(f"ADB related: {len(adb_related)}")
print(f"Messenger related: {len(messenger_related)}")
print(f"General/Other: {len(general_related)}")
