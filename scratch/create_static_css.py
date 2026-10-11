import re

with open('scratch/orig_style.css', 'r', encoding='utf-8') as f:
    css_text = f.read()

# Remove <style> and </style> if present
css_clean = css_text.replace('<style>', '').replace('</style>', '').strip()

# Check open/close braces count
open_braces = css_clean.count('{')
close_braces = css_clean.count('}')

print(f"Open braces: {open_braces}, Close braces: {close_braces}")
if open_braces == close_braces:
    print("Braces match perfectly!")
else:
    print("WARNING: Braces mismatch!")

with open('static/style.css', 'w', encoding='utf-8') as f:
    f.write(css_clean)

print("Wrote static/style.css, bytes:", len(css_clean))
