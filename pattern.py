"""The phone: its screen, its selfie camera, and the tracking pattern its app draws.

The phone app (android/) tells us all of this in its hello line, so nothing here is fixed
to one phone. The last phone seen is kept in ~/.config/phone-screen/phone.json, so the
next start (before the app has said hello) already knows it. With nothing known yet, the
numbers are a Pixel 9 Pro XL's, the phone this was made on.

The pattern is two ArUco markers on white, one above and one below the screen's centre,
drawn in true millimetres (the panel's own pixel density).
"""
import json
from pathlib import Path

import cv2
import numpy as np

FILE = Path.home() / '.config/phone-screen/phone.json'
DICT = cv2.aruco.DICT_4X4_50

# Pixel 9 Pro XL, as the phone itself reports it (dumpsys display: 1008 x 2244 pixels at
# 365.76 x 365.369 per inch, so 70.00 x 156.00 mm); its selfie camera sits 71 mm above the
# screen's centre. Its body is 76.6 x 162.8 x 8.5 mm.
DEFAULT = dict(
    name='Pixel 9 Pro XL (default)',
    px=(1008, 2244),
    xdpi=365.76, ydpi=365.369,
    dpi=360,                                        # Android's density, for the apps' display
    cam=(0.0, 71.0, -1.0),                          # selfie camera from the screen's centre, mm
    markers=((0, 56.0, 36.0), (1, 56.0, -36.0)),    # (id, side, centre above the screen's centre), mm
)


def _num(s, kind=float):
    try:
        return kind(s)
    except ValueError:
        return None


class Phone:
    def __init__(self, d=None):
        d = dict(DEFAULT, **(d or {}))
        self.name = d['name']
        self.px = tuple(int(v) for v in d['px'])
        self.xdpi, self.ydpi = float(d['xdpi']), float(d['ydpi'])
        self.dpi = int(d['dpi'])
        self.cam_mm = tuple(float(v) for v in d['cam'])
        self.markers = tuple((int(i), float(s), float(c)) for i, s, c in d['markers'])

    def as_dict(self):
        return dict(name=self.name, px=list(self.px), xdpi=self.xdpi, ydpi=self.ydpi, dpi=self.dpi,
                    cam=list(self.cam_mm), markers=[list(m) for m in self.markers])

    def __eq__(self, other):
        return isinstance(other, Phone) and self.as_dict() == other.as_dict()

    # ---- what the rest of the code uses

    @property
    def screen_mm(self):
        return (self.px[0] / self.xdpi * 25.4, self.px[1] / self.ydpi * 25.4)

    @property
    def screen_m(self):
        w, h = self.screen_mm
        return (w / 1000, h / 1000)

    @property
    def cam(self):
        """The selfie camera from the screen's centre, metres (x right, y up, z out of the screen)."""
        return np.array(self.cam_mm) / 1000

    @property
    def display(self):
        """The apps' virtual display for scrcpy: the real screen's size and density."""
        return '%dx%d/%d' % (self.px[0], self.px[1], self.dpi)

    def marker_corners(self):
        """{id: 4x3 corners in the phone's frame, metres}: x right, y up, z out of the screen,
        in ArUco's order (top-left, top-right, bottom-right, bottom-left)."""
        out = {}
        for mid, side, cy in self.markers:
            s, c = side / 2000, cy / 1000
            out[mid] = np.array([[-s, c + s, 0], [s, c + s, 0], [s, c - s, 0], [-s, c - s, 0]])
        return out

    # ---- from the app

    @classmethod
    def from_hello(cls, words):
        """The app's hello line: hello W H XDPI YDPI [key=value ...]. An older app sends only
        the first four (the Pixel's layout, drawn at its own density). Returns (phone, notes),
        notes saying what was left at its default and why."""
        d, notes = {}, []
        if len(words) >= 5:
            w, h, xd, yd = _num(words[1], int), _num(words[2], int), _num(words[3]), _num(words[4])
            if w and h and xd and yd:
                d.update(px=(w, h), xdpi=xd, ydpi=yd)
        for kv in words[5:]:
            k, _, v = kv.partition('=')
            try:
                if k == 'px':
                    d['px'] = tuple(int(x) for x in v.split('x'))
                elif k == 'dpi':
                    d['dpi'] = int(v)
                elif k == 'cam':
                    d['cam'] = tuple(float(x) for x in v.split(','))
                elif k == 'markers':
                    d['markers'] = tuple(tuple(float(x) for x in m.split(':')) for m in v.split(','))
                elif k == 'name':
                    d['name'] = v.replace('_', ' ')
            except ValueError:
                notes.append('could not read %s' % kv)
        if 'name' not in d:
            d['name'] = 'phone'
        if 'markers' not in d:
            notes.append('an older app: its own fixed pattern')
        p = cls(d)
        w, h = p.screen_mm
        if not (35 < w < 130 and 70 < h < 260):     # a panel that reports a wrong density
            notes.append('screen %.0f x %.0f mm looks wrong: using the default size' % (w, h))
            p.xdpi, p.ydpi = p.px[0] / DEFAULT['px'][0] * DEFAULT['xdpi'], p.px[1] / DEFAULT['px'][1] * DEFAULT['ydpi']
        if 'cam' not in d and 'px' in d:
            # no position from the app: just below the top edge, in the middle
            p.cam_mm = (0.0, p.screen_mm[1] / 2 - 7.0, -1.0)
            notes.append('selfie camera guessed')
        return p, notes

    def describe(self):
        w, h = self.screen_mm
        return '%s: %dx%d px, %.1f x %.1f mm, selfie camera at %s mm, markers %s' % (
            self.name, self.px[0], self.px[1], w, h, ','.join('%g' % v for v in self.cam_mm),
            ' '.join('%d:%g@%+g' % m for m in self.markers))

    # ---- kept between runs

    @classmethod
    def load(cls):
        try:
            return cls(json.loads(FILE.read_text()))
        except (OSError, ValueError, KeyError, TypeError):
            return cls()

    def save(self):
        FILE.parent.mkdir(parents=True, exist_ok=True)
        FILE.write_text(json.dumps(self.as_dict(), indent=1))


current = Phone.load()          # the phone in use; phonelink.py replaces it when the app says hello
