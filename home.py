"""The home screen Phone Screen shows in VR: a copy of the phone's real one (homecap.py).

Tap an app and it opens on the hidden display; tap a folder and it opens. While an app
is open, touches go to it, except the phone's usual gestures: swipe up from the bottom
edge to come home, swipe in from either side edge to go back.
"""
import json
import time
from pathlib import Path

import cv2
import numpy as np

import video

DIR = Path.home() / '.local/share/phone-screen/home'
W, H = 1008, 2244           # the real screen, pixels (the copy's coordinates)
EDGE = 0.04                 # side and bottom edge zones for the gestures, as a fraction
SWIPE = 0.08                # how far a gesture must go
HOLD_BACK = 0.25            # an edge touch that hasn't become a gesture by now is a touch


class Home:
    def __init__(self, phone, size=(404, 900), log=print):
        self.phone, self.size, self.log = phone, size, log
        try:
            self.data = json.loads((DIR / 'home.json').read_text())
        except OSError:     # no copy made yet (homecap.py): a plain screen
            self.data = {'pages': [{'image': None, 'items': []}]}
        self.images = {}
        self.page = 0
        self.folder = None          # the open folder's item
        self.mode = 'home'          # 'home', 'folder' or 'app'
        self.version = 0            # changes whenever the picture to show changes
        self.touches = {}           # pointer -> dict(start, last, t0, held)
        self.opened = 0.0

    def image(self):
        name = self.folder['image'] if self.mode == 'folder' else self.data['pages'][self.page]['image']
        img = self.images.get(name)
        if img is None and name is None:
            img = self.images[name] = np.full((self.size[1], self.size[0], 4), (24, 28, 36, 255), np.uint8)
        if img is None:
            bgr = cv2.imread(str(DIR / name))
            img = cv2.cvtColor(cv2.resize(bgr, self.size, interpolation=cv2.INTER_AREA), cv2.COLOR_BGR2RGBA)
            self.images[name] = img
        return img

    def _show(self, mode, folder=None):
        self.mode, self.folder = mode, folder
        self.version += 1

    def go_home(self):
        self._show('home')

    def _hit(self, items, x, y):
        px, py = x * W, y * H
        for it in items:
            x0, y0, x1, y1 = it['box']
            if x0 <= px <= x1 and y0 <= py <= y1:
                return it
        return None

    def _open(self, it):
        pkg = it.get('package')
        if not pkg:
            return
        self.log('home: opening %s (%s)' % (it['label'], pkg))
        self.phone.start_app(pkg)
        self.opened = time.monotonic()
        self._show('app')

    def _tap(self, x, y):
        if self.mode == 'folder':
            it = self._hit(self.folder.get('items', []), x, y)
            if it:
                self._open(it)
            else:
                self.go_home()
            return
        it = self._hit(self.data['pages'][self.page]['items'], x, y)
        if it is None:
            return
        if it['kind'] == 'folder':
            self._show('folder', it)
        else:
            self._open(it)

    def touch(self, action, pid, x, y, p):
        """A touch on the real screen. Returns True if it was used here, False if it goes to the app."""
        now = time.monotonic()
        if action == 'down':
            edge = x < EDGE or x > 1 - EDGE or y > 1 - EDGE
            self.touches[pid] = dict(start=(x, y), t0=now, edge=edge, held=[], forwarded=False)
        t = self.touches.get(pid)
        if t is None:
            return self.mode != 'app'
        dx, dy = x - t['start'][0], y - t['start'][1]
        if self.mode != 'app':
            if action == 'up':
                del self.touches[pid]
                if abs(dx) < 0.03 and abs(dy) < 0.03:
                    self._tap(x, y)
                elif abs(dx) > 0.2 and abs(dx) > abs(dy) and len(self.data['pages']) > 1:
                    self.page = (self.page + (1 if dx < 0 else -1)) % len(self.data['pages'])
                    self.version += 1
            return True
        # an app is open
        if t['edge'] and not t['forwarded']:
            sx, sy = t['start']
            if sy > 1 - EDGE and -dy > SWIPE:
                del self.touches[pid]
                self.go_home()
                return True
            if (sx < EDGE and dx > SWIPE) or (sx > 1 - EDGE and -dx > SWIPE):
                del self.touches[pid]
                self.phone.key(4)                       # BACK
                return True
            t['held'].append((action, x, y, p))
            if now - t['t0'] < HOLD_BACK and action != 'up':
                return True                             # still deciding
            for a, hx, hy, hp in t['held']:             # not a gesture: let the app have it
                self.phone.touch({'down': video.DOWN, 'move': video.MOVE, 'up': video.UP}[a], pid, hx, hy, hp)
            t['forwarded'] = True
            if action == 'up':
                del self.touches[pid]
            return True
        if action == 'up':
            del self.touches[pid]
        return False
