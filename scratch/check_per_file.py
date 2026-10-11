import os
import re

with open('scratch/orig_style.css', 'r', encoding='utf-8') as f:
    orig_css = f.read()

# Extract rule blocks
pattern = re.compile(r'([^{}]+)\{([^}]*)\}', re.DOTALL)
all_rules = []
for match in pattern.finditer(orig_css):
    sel = match.group(1).strip()
    body = match.group(2).strip()
    if sel.startswith('@'): continue
    all_rules.append((sel, body))

templates_dir = 'templates'
template_files = ['index.html', 'screen.html', 'files.html', 'clipboard.html', 'adb.html', 'messenger.html']

for tf in template_files:
    path = os.path.join(templates_dir, tf)
    if not os.path.exists(path):
        continue
    with open(path, 'r', encoding='utf-8') as f:
        content = f.read()
    
    missing_sels = []
    for sel, body in all_rules:
        # Normalize selector check
        # Check main selector identifier (e.g. .file-row or #file-search)
        first_sel = sel.split(',')[0].strip()
        if first_sel not in content:
            missing_sels.append(sel)
            
    print(f"=== {tf}: {len(missing_sels)} missing selectors out of {len(all_rules)} ===")
