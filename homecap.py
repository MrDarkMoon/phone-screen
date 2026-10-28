"""Copy the phone's real home screen: a picture of each page and each folder, and where
every app is on them, for the home screen Phone Screen draws in VR (home.py).

Android shows its normal home screen only on the real display, and the real display
shows the tracking pattern, so the hidden display's home is a copy. This takes over the
real screen for a few seconds: home, each page, each folder; then back to the pattern.
"""
import json
import re
import sys
import time
from pathlib import Path

import video

OUT = Path.home() / '.local/share/phone-screen/home'
NODE = re.compile(r'<node [^>]*?text="([^"]*)"[^>]*?package="([^"]*)"[^>]*?content-desc="([^"]*)"'
                  r'[^>]*?clickable="true"[^>]*?bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"')
LAUNCHER = 'com.google.android.apps.nexuslauncher'


def sh(*args):
    return video.adb('shell', *args)


def snap(name):
    """Screenshot of the real display and its clickable items."""
    raw = video.adb_bytes('exec-out', 'screencap', '-p')
    (OUT / (name + '.png')).write_bytes(raw)
    sh('uiautomator', 'dump', '/sdcard/ps-home.xml')
    xml = video.adb('exec-out', 'cat', '/sdcard/ps-home.xml').stdout
    items = []
    for text, pkg, desc, x0, y0, x1, y1 in NODE.findall(xml):
        box = [int(x0), int(y0), int(x1), int(y1)]
        label = text or desc
        if desc.startswith('Folder:'):
            items.append(dict(kind='folder', label=desc, box=box))
        elif pkg == LAUNCHER and text:
            items.append(dict(kind='app', label=label, box=box))
        elif pkg != LAUNCHER and pkg != 'com.android.systemui':
            items.append(dict(kind='package', package=pkg, label=label, box=box))
    return items


def app_list():
    """{label: package} from scrcpy's server (list_apps)."""
    video.adb('push', video.SERVER, '/data/local/tmp/ps-list.jar')
    out = sh('CLASSPATH=/data/local/tmp/ps-list.jar app_process / com.genymobile.scrcpy.Server '
             + video.SERVER_VERSION + ' list_apps=true').stdout
    apps = {}
    for line in out.splitlines():
        m = re.match(r'\s*[*-] (.+?)\s{2,}(\S+)\s*$', line)
        if m:
            apps[m.group(1)] = m.group(2)
    return apps


def resolve(items, apps):
    norm = {k.replace('\xad', '').lower(): v for k, v in apps.items()}
    for it in items:
        if it['kind'] != 'app':
            continue
        label = it['label'].replace('\xad', '').lower()
        pkg = norm.get(label)
        if pkg is None:         # "Play Store" is "Google Play Store", "Files" is "Files by Google"...
            near = sorted((k for k in norm if label in k or k in label), key=len)
            pkg = norm[near[0]] if near else None
        it['package'] = pkg


def capture(log=print):
    OUT.mkdir(parents=True, exist_ok=True)
    apps = app_list()
    log('home copy: %d apps known' % len(apps))
    sh('input', 'keyevent', 'HOME')
    time.sleep(0.6)
    sh('input', 'keyevent', 'HOME')         # twice: back to the first page
    time.sleep(1.2)
    pages, last = [], None
    for n in range(6):
        items = snap('page%d' % n)
        sig = [(i['label'], i['box']) for i in items]
        if sig == last:
            (OUT / ('page%d.png' % n)).unlink()
            break
        last = sig
        resolve(items, apps)
        for k, it in enumerate(items):
            if it['kind'] != 'folder':
                continue
            x0, y0, x1, y1 = it['box']
            sh('input', 'tap', str((x0 + x1) // 2), str((y0 + y1) // 2))
            time.sleep(1.0)
            name = 'page%d-folder%d' % (n, k)
            inside = [i for i in snap(name) if i['kind'] == 'app']
            resolve(inside, apps)
            it['image'], it['items'] = name + '.png', inside
            sh('input', 'keyevent', 'BACK')
            time.sleep(0.8)
        pages.append(dict(image='page%d.png' % n, items=items))
        log('home copy: page %d, %d items' % (n + 1, len(items)))
        sh('input', 'swipe', '900', '1300', '100', '1300', '250')
        time.sleep(1.0)
    sh('input', 'keyevent', 'HOME')
    (OUT / 'home.json').write_text(json.dumps(dict(pages=pages, when=time.time()), indent=1))
    return pages


if __name__ == '__main__':
    pages = capture()
    for p in pages:
        for it in p['items']:
            print(it['kind'], it['label'][:30], it.get('package'), it['box'])
    sys.exit(0)
