"""Find the phone from the ArUco markers its real screen shows (pattern.py).

Each tracking camera's frame is searched for the markers; their corners go to rays
through the camera's fisheye calibration, then onto a virtual pinhole aimed at them,
where solvePnP gives the phone's pose from the markers' known layout. Several cameras'
poses are averaged, weighted by how many corners each saw. Head frame, metres.
"""
import os
import sys
import time

import cv2
import numpy as np

import pattern
import tracker

sys.path.insert(0, os.path.join(os.environ.get('FRAMETOP', os.path.expanduser('~/frametop')), 'hands/tools'))
import calib  # noqa: E402
from ring import Ring  # noqa: E402

OBJ = pattern.marker_corners()


def _detector():
    p = cv2.aruco.DetectorParameters()
    p.cornerRefinementMethod = cv2.aruco.CORNER_REFINE_SUBPIX
    p.adaptiveThreshWinSizeMin = 5
    p.adaptiveThreshWinSizeMax = 35
    p.adaptiveThreshWinSizeStep = 10
    p.minMarkerPerimeterRate = 0.03
    return cv2.aruco.ArucoDetector(cv2.aruco.getPredefinedDictionary(pattern.DICT), p)


def bright_box(img, mask):
    """Bounding box (padded) of the biggest bright patch: the lit screen, quickly."""
    small = cv2.resize(img, (img.shape[1] // 4, img.shape[0] // 4), interpolation=cv2.INTER_AREA)
    m = cv2.resize(mask, (small.shape[1], small.shape[0]), interpolation=cv2.INTER_NEAREST)
    level = max(60, 0.7 * float(small[m > 0].max()))     # the screen is the brightest thing about
    n, _, stats, _ = cv2.connectedComponentsWithStats(((small > level) & (m > 0)).astype(np.uint8))
    if n < 2:
        return None
    k = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))
    x, y, w, h, area = stats[k]
    if area < 15:
        return None
    pad = max(w, h) // 2 + 6
    H, W = img.shape
    return (max(0, (x - pad) * 4), max(0, (y - pad) * 4), min(W, (x + w + pad) * 4), min(H, (y + h + pad) * 4))


def screen_outline(img, mask):
    """Pixels (N,2) along the edge of the biggest bright patch, if it's shaped like a lit
    screen (a solid convex patch), else None."""
    box = bright_box(img, mask)
    if box is None:
        return None
    x0, y0, x1, y1 = box
    roi = img[y0:y1, x0:x1]
    _, bw = cv2.threshold(roi, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    bw &= mask[y0:y1, x0:x1]
    cs, _ = cv2.findContours(bw, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
    if not cs:
        return None
    # the markers' black squares break the lit screen up: close the gaps into one solid shape
    _, _, bw_, bh_ = cv2.boundingRect(max(cs, key=cv2.contourArea))
    k = max(3, int(0.3 * min(bw_, bh_)) | 1)
    bw = cv2.morphologyEx(bw, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_RECT, (k, k)))
    cs, _ = cv2.findContours(bw, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
    c = max(cs, key=cv2.contourArea)
    area = cv2.contourArea(c)
    if area < 40 or area / max(cv2.contourArea(cv2.convexHull(c)), 1.0) < 0.85:
        return None
    pts = c.reshape(-1, 2)
    h, w = bw.shape
    if (pts[:, 0] <= 0).any() or (pts[:, 1] <= 0).any() or (pts[:, 0] >= w - 1).any() or (pts[:, 1] >= h - 1).any():
        return None             # cut off by the search box: not all of it
    pts = pts[::max(1, len(pts) // 120)]
    return pts.astype(float) + [x0, y0]


class MarkerTracker:
    def __init__(self):
        self.calib = calib.load()
        self.det = _detector()
        self.ring = None
        self.checked = 0.0
        self._open()

    def _open(self):
        """(Re)attach to ft-camd's ring; it makes a new one each time it starts."""
        try:
            self.ring = Ring()
        except (OSError, RuntimeError, ValueError):
            self.ring = None
            return
        self.cams = []
        for rc in self.ring.cams:
            if rc.name.endswith('-dark'):
                continue
            key = next((k for k in tracker.RING_NAMES if k in rc.name), None)
            if key:
                self.cams.append((rc, tracker.RING_NAMES[key]))
        self.swap = False
        self._reset()

    def _reset(self):
        self.roi = {}               # camera -> (x0, y0, x1, y1) where the markers were last
        self.full_scan = {}         # camera -> frames since the last full-frame search
        self.misses = {}            # camera -> frames in a row without the markers
        self.glow_every = {}        # camera -> marker frames since its outline was last measured
        self.last_image = {}

    def _cam(self, name):
        if self.swap and name in ('slam_left', 'slam_right'):
            name = 'slam_right' if name == 'slam_left' else 'slam_left'
        return self.calib[name]

    def _detect(self, rc, cam, img):
        """Corners (N,4,2) and ids of the phone's markers in one image."""
        roi = self.roi.get(rc.index)
        n = self.full_scan.get(rc.index, 0)
        if roi is None:
            # a full search takes up to 80 ms: while the markers can't be read (too far),
            # look for them only every 5th frame and track the outline in between
            miss = self.misses[rc.index] = self.misses.get(rc.index, 0) + 1
            if miss % 5:
                return [], []
        else:
            self.misses[rc.index] = 0
        if roi is not None and n < 15:
            x0, y0, x1, y1 = roi
            corners, ids, _ = self.det.detectMarkers(img[y0:y1, x0:x1])
            off = np.array([x0, y0], np.float32)
            self.full_scan[rc.index] = n + 1
        else:
            # a full-frame ArUco search takes ~100 ms; look only where something is bright
            self.full_scan[rc.index] = 0
            box = bright_box(img, tracker.lens_mask(cam))
            if box is None:
                return [], []
            x0, y0, x1, y1 = box
            corners, ids, _ = self.det.detectMarkers(img[y0:y1, x0:x1])
            off = np.array([x0, y0], np.float32)
        if ids is None:
            self.roi.pop(rc.index, None)
            return [], []
        keep = [(c.reshape(4, 2) + off, int(i)) for c, i in zip(corners, ids.ravel()) if int(i) in OBJ]
        if not keep:
            self.roi.pop(rc.index, None)
            return [], []
        pts = np.concatenate([c for c, _ in keep])
        x0, y0 = pts.min(axis=0) - 80
        x1, y1 = pts.max(axis=0) + 80
        h, w = img.shape
        self.roi[rc.index] = (int(max(0, x0)), int(max(0, y0)), int(min(w, x1)), int(min(h, y1)))
        return [c for c, _ in keep], [i for _, i in keep]

    def _pose(self, cam, corners, ids):
        """solvePnP on a virtual pinhole: (R, t) of the phone in the head frame, reprojection rms."""
        px = np.concatenate(corners).astype(float)
        obj = np.concatenate([OBJ[i] for i in ids])
        rays = cam.rays(px)
        B = tracker._basis(rays.mean(axis=0))
        local = rays @ B.T
        img = local[:, :2] / local[:, 2:3]
        flag = cv2.SOLVEPNP_IPPE if len(ids) == 1 else cv2.SOLVEPNP_SQPNP
        ok, rvec, tvec = cv2.solvePnP(obj, img, np.eye(3), None, flags=flag)
        if not ok:
            return None
        if len(ids) > 1:
            rvec, tvec = cv2.solvePnPRefineLM(obj, img, np.eye(3), None, rvec, tvec)
        R, _ = cv2.Rodrigues(rvec)
        proj = obj @ R.T + tvec.ravel()
        err = np.sqrt(np.mean(np.sum((proj[:, :2] / proj[:, 2:3] - img) ** 2, axis=1)))
        return B.T @ R, B.T @ tvec.ravel() + cam.origin, float(err) * cam.fx

    def step(self):
        """Returns (pose dict or None, dqbuf_ns of the frames used)."""
        now = time.monotonic()
        if now - self.checked > 2.0:
            self.checked = now
            if self.ring is None or not self.ring.alive(2.0):
                self._open()
        if self.ring is None:
            return None, 0
        found, outlines, stamp = [], [], 0
        for rc, name in self.cams:
            f = self.ring.read(rc)
            if f is None:
                continue
            if self.last_image.get(rc.index) == f.frame:
                continue
            self.last_image[rc.index] = f.frame
            cam = self._cam(name)
            corners, ids = self._detect(rc, cam, f.image)
            if len(ids) < 2:    # one marker's pose can flip to a mirror-image tilt, far off: both or nothing
                pts = screen_outline(f.image, tracker.lens_mask(cam))
                if pts is not None:
                    outlines.append(dict(name=name, stamp=f.dqbuf_ns, rays=cam.rays(pts), origin=cam.origin))
                continue
            r = self._pose(self._cam(name), corners, ids)
            if r is None:
                continue
            R, t, err = r
            if err > 3.0 or not (0.12 < np.linalg.norm(t) < 1.2):
                continue
            # now and then, the outline too: how much its glow widens it is learned from these
            k = self.glow_every[rc.index] = self.glow_every.get(rc.index, 0) + 1
            if k % 6 == 0:
                pts = screen_outline(f.image, tracker.lens_mask(cam))
                if pts is not None:
                    outlines.append(dict(name=name, stamp=f.dqbuf_ns, rays=cam.rays(pts), origin=cam.origin,
                                         learn=True))
            px = np.concatenate(corners).astype(float)
            obs = dict(points=np.concatenate([OBJ[i] for i in ids]), rays=cam.rays(px),
                       origins=np.repeat(cam.origin[None], len(px), axis=0), stamp=f.dqbuf_ns)
            found.append((len(ids) * 4, R, t, err, name, obs))
        if not found:
            if not outlines:
                return None, stamp
            stamp = max(o['stamp'] for o in outlines)
            outlines = [o for o in outlines if stamp - o['stamp'] < 20_000_000]
            return dict(kind='outline', outlines=outlines, cams=len(outlines), corners=0,
                        names=[o['name'] for o in outlines], err=0.0), stamp
        # only frames from the same moment go into one fit
        stamp = max(f[5]['stamp'] for f in found)
        found = [f for f in found if stamp - f[5]['stamp'] < 20_000_000]
        w = np.array([n / (1 + e) for n, _, _, e, *_ in found])
        t = np.sum([wi * f[2] for wi, f in zip(w, found)], axis=0) / w.sum()
        M = np.sum([wi * f[1] for wi, f in zip(w, found)], axis=0)
        u, _, vt = np.linalg.svd(M)
        R = u @ vt
        obs = {k: np.concatenate([f[5][k] for f in found]) for k in ('points', 'rays', 'origins')}
        return dict(kind='markers', outlines=[o for o in outlines if o.get('learn')], R=R, centre=t,
                    cams=len(found), err=float(min(f[3] for f in found)),
                    corners=sum(f[0] for f in found), size=pattern.SCREEN_MM, obs=obs,
                    names=[f[4] for f in found],
                    cam_obs=[dict(name=f[4], stamp=f[5]['stamp'], centre=f[2], **{k: f[5][k] for k in
                             ('points', 'rays', 'origins')}) for f in found],
                    percam=[(f[4], f[5]['stamp'], f[2], len(f[5]['points'])) for f in found]), stamp
