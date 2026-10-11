import re

with open('scratch/orig_style.css', 'r', encoding='utf-8') as f:
    orig_css = f.read()

pattern = re.compile(r'([^{}]+)\{([^}]*)\}', re.DOTALL)
file_blocks = []
for match in pattern.finditer(orig_css):
    sel = match.group(1).strip()
    body = match.group(2).strip()
    if sel.startswith('@'): continue
    sel_lower = sel.lower()
    if any(k in sel_lower for k in ['.file', '#file', 'drop-zone', 'upload', 'dz-', 'preview', 'dl-multi', 'del-multi', 'action-btn', 'prev-btn', 'dl-btn', 'del-btn', 'play-overlay']):
        file_blocks.append(f"{sel} {{\n  {body}\n}}")

with open('scratch/file_css_blocks.css', 'w', encoding='utf-8') as f:
    f.write('\n\n'.join(file_blocks))

print("Wrote scratch/file_css_blocks.css with", len(file_blocks), "blocks.")
