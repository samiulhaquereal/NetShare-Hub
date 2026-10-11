import subprocess
import os

def get_file_from_git(commit_file):
    return subprocess.check_output(['git', 'show', commit_file], text=True, encoding='utf-8', errors='ignore')

orig_html = get_file_from_git('d3fa0de^:templates/index.html')

# Write original HTML style block to scratch file so we can view it
style_start = orig_html.find('<style>')
style_end = orig_html.find('</style>')
orig_style = orig_html[style_start:style_end+8]

with open('scratch/orig_style.css', 'w', encoding='utf-8') as f:
    f.write(orig_style)

print("Wrote orig_style.css, length:", len(orig_style))
