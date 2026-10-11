import urllib.request

urls = [
    'http://127.0.0.1:5000/',
    'http://127.0.0.1:5000/screen',
    'http://127.0.0.1:5000/files-page',
    'http://127.0.0.1:5000/files',
    'http://127.0.0.1:5000/clipboard-page',
    'http://127.0.0.1:5000/adb-hub',
    'http://127.0.0.1:5000/messenger',
    'http://127.0.0.1:5000/static/style.css'
]

for url in urls:
    try:
        req = urllib.request.urlopen(url)
        content = req.read()
        print(f"OK {url} => HTTP {req.getcode()} (Length: {len(content)} bytes)")
    except Exception as e:
        print(f"FAIL {url} => {e}")
