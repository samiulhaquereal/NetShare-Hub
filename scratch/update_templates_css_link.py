import os

template_files = ['index.html', 'screen.html', 'files.html', 'clipboard.html', 'adb.html', 'messenger.html']
link_tag = '  <link rel="stylesheet" href="/static/style.css" />\n'

for tf in template_files:
    path = os.path.join('templates', tf)
    if not os.path.exists(path):
        continue
    with open(path, 'r', encoding='utf-8') as f:
        content = f.read()

    if '/static/style.css' not in content:
        # Insert link_tag before </head> or right after icon tags
        if '<link rel="shortcut icon"' in content:
            idx = content.find('<link rel="shortcut icon"')
            # Find end of that line
            eol = content.find('\n', idx)
            content = content[:eol+1] + link_tag + content[eol+1:]
        elif '</head>' in content:
            idx = content.find('</head>')
            content = content[:idx] + link_tag + content[idx:]
        
        with open(path, 'w', encoding='utf-8') as f:
            f.write(content)
        print(f"Updated {tf} with static/style.css link!")
    else:
        print(f"{tf} already has static/style.css link.")
