"""Track a lit phone screen in 3D with the headset's tracking cameras (ft-camd's ring).

Each camera's bright blobs are fitted with a rectangle on a virtual pinhole aimed at
the blob (the lenses are fisheye). When two cameras see it, the four corners are
triangulated, which also measures the screen's size; with one camera, the pose comes
from solvePnP with the size measured so far. Poses are in the head frame (OpenVR's:
+x right, +y up, -z forward), in metres.
"""
import os
import sys
import time

import cv2
import numpy as np

sys.path.insert(0, os.path.join(os.environ.get('FRAMETOP', os.path.expanduser('~/frametop')), 'hands/tools'))
import calib  # noqa: E402
from ring import Ring  # noqa: E402

RING_NAMES = {'4-0036': 'slam_right', '4-0060': 'slam_left',
              '5-0060': 'upper_left', '5-003e': 'upper_right'}
NEAR, FAR = 0.18, 1.0          # where a held phone can be, metres from the head


def _basis(axis):
    z = axis / np.linalg.norm(axis)
    x = np.cross([0.0, 1.0, 0.0], z)
    if np.linalg.norm(x) < 1e-6:
        x = np.cross([1.0, 0.0, 0.0], z)
    x /= np.linalg.norm(x)
    return np.stack([x, np.cross(z, x), z])     # rows: virtual camera axes in the head frame


class View:
    """One screen candidate in one camera: corners as head-frame rays."""

    def __init__(self, cam, rays, score, area_px):
        self.cam, self.rays, self.score, self.area_px = cam, rays, score, area_px


_masks = {}


def lens_mask(cam):
    """Pixels within 80 degrees of the optical axis. In the dark the headset lights the room
    in IR, and the lens rim glows bright enough to look like a screen."""
    m = _masks.get(cam.name)
    if m is None:
        v, u = np.mgrid[0:cam.height:4, 0:cam.width:4]
        ang = cam.angle_from_axis(np.stack([u.ravel(), v.ravel()], axis=1).astype(float))
        small = (ang.reshape(u.shape) < 80).astype(np.uint8) * 255
        m = _masks[cam.name] = cv2.resize(small, (cam.width, cam.height), interpolation=cv2.INTER_NEAREST)
    return m


def find_views(cam, img, thresh=200):
    blur = cv2.GaussianBlur(img, (3, 3), 0)
    _, bw = cv2.threshold(blur, thresh, 255, cv2.THRESH_BINARY)
    bw &= lens_mask(cam)
    bw = cv2.morphologyEx(bw, cv2.MORPH_OPEN, np.ones((7, 7), np.uint8))
    bw = cv2.morphologyEx(bw, cv2.MORPH_CLOSE, np.ones((5, 5), np.uint8))
    contours, _ = cv2.findContours(bw, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
    views = []
    for c in contours:
        area = cv2.contourArea(c)
        if area < 120 or area > img.size * 0.3:
            continue
        pts = c.reshape(-1, 2).astype(float)
        rays = cam.rays(pts)
        B = _basis(rays.mean(axis=0))
        local = rays @ B.T
        if np.any(local[:, 2] < 0.2):
            continue
        plane = (local[:, :2] / local[:, 2:3]).astype(np.float32)
        hull = cv2.convexHull(plane)
        rect = cv2.minAreaRect(hull)
        rw, rh = rect[1]
        if min(rw, rh) < 1e-4:
            continue
        aspect = max(rw, rh) / min(rw, rh)
        fill = cv2.contourArea(hull) / (rw * rh)
        if not (1.2 < aspect < 3.2) or fill < 0.75:
            continue
        corners = cv2.boxPoints(rect)
        r = np.c_[corners, np.ones(4)] @ B
        r /= np.linalg.norm(r, axis=1, keepdims=True)
        views.append(View(cam, r, fill * area, area))
    views.sort(key=lambda v: -v.score)
    return views[:3]


def _pose_from_corners(P):
    """4 corner points (in order round the rectangle) -> (centre, R with x short side,
    y long side, z normal), and (width, height)."""
    c = P.mean(axis=0)
    e01, e12 = P[1] - P[0], P[2] - P[1]
    a = (e01 + (P[2] - P[3])) / 2
    b = (e12 + (P[3] - P[0])) / 2
    short, long_ = (a, b) if np.linalg.norm(a) < np.linalg.norm(b) else (b, a)
    y = long_ / np.linalg.norm(long_)
    x = short - y * (short @ y)
    x /= np.linalg.norm(x)
    z = np.cross(x, y)
    return c, np.stack([x, y, z], axis=1), (np.linalg.norm(short), np.linalg.norm(long_))


def stereo(v1, v2):
    best = None
    for shift in range(4):
        rays2 = np.roll(v2.rays, shift, axis=0)
        pts, errs = [], []
        for d1, d2 in zip(v1.rays, rays2):
            p, e = calib.triangulate([v1.cam.origin, v2.cam.origin], [d1, d2])
            pts.append(p)
            errs.append(e)
        err = float(np.sqrt(np.mean(np.square(errs))))
        if best is None or err < best[0]:
            best = (err, np.array(pts))
    return best


def mono(view, size):
    """solvePnP on the virtual pinhole with a known screen size (w, h)."""
    B = _basis(view.rays.mean(axis=0))
    local = view.rays @ B.T
    img = (local[:, :2] / local[:, 2:3]).astype(np.float64)
    w, h = size
    e01 = np.linalg.norm(img[1] - img[0])
    e12 = np.linalg.norm(img[2] - img[1])
    if e01 < e12:
        obj = np.array([[-w / 2, -h / 2, 0], [w / 2, -h / 2, 0], [w / 2, h / 2, 0], [-w / 2, h / 2, 0]])
    else:
        obj = np.array([[-w / 2, -h / 2, 0], [-w / 2, h / 2, 0], [w / 2, h / 2, 0], [w / 2, -h / 2, 0]])
    ok, rvec, tvec = cv2.solvePnP(obj, img, np.eye(3), None, flags=cv2.SOLVEPNP_IPPE)
    if not ok:
        return None
    R, _ = cv2.Rodrigues(rvec)
    pts_local = obj @ R.T + tvec.ravel()
    o = view.cam.origin
    return pts_local @ B + o


class Tracker:
    def __init__(self, size=(0.071, 0.158)):
        self.ring = Ring()
        self.calib = calib.load()
        self.cams = []
        for rc in self.ring.cams:
            if rc.name.endswith('-dark'):
                continue
            key = next((k for k in RING_NAMES if k in rc.name), None)
            if key:
                self.cams.append((rc, RING_NAMES[key]))
        self.swap = 0               # votes: >0 means ft-camd named the side cameras backwards
        self.size = size            # the screen's (w, h), metres
        self.last = None            # last centre, head frame
        self.last_stereo = None     # (centre, when) of the last two-camera sighting

    def _cam(self, name, swapped):
        if swapped and name in ('slam_left', 'slam_right'):
            name = 'slam_right' if name == 'slam_left' else 'slam_left'
        return self.calib[name]

    def _size_ok(self, size):
        return all(0.7 < m / k < 1.3 for m, k in zip(size, self.size))

    def step(self):
        """Look at the newest frames. Returns (pose dict or None, dqbuf_ns of the frames)."""
        found, stamp = [], 0
        swapped = self.swap > 0
        for rc, name in self.cams:
            f = self.ring.read(rc)
            if f is None:
                continue
            stamp = max(stamp, f.dqbuf_ns)
            for v in find_views(self._cam(name, swapped), f.image):
                v.name = name
                found.append(v)
        if not found:
            return None, stamp
        best = None
        for i in range(len(found)):
            for j in range(i + 1, len(found)):
                a, b = found[i], found[j]
                if a.name == b.name:
                    continue
                for flip in (False, True):
                    if flip:
                        ca, cb = self._cam(a.name, not swapped), self._cam(b.name, not swapped)
                        if ca is a.cam and cb is b.cam:
                            continue
                        va, vb = View(ca, a.rays_for(ca), 0, 0), View(cb, b.rays_for(cb), 0, 0)
                    else:
                        va, vb = a, b
                    err, P = stereo(va, vb)
                    c = P.mean(axis=0)
                    if err < 0.008 and NEAR < np.linalg.norm(c) < FAR and c[2] < 0.05 \
                            and self._size_ok(_pose_from_corners(P)[2]):
                        if best is None or err < best[0]:
                            best = (err, P, flip)
        if best:
            err, P, flip = best
            if flip:
                self.swap += -1 if swapped else 1
            c, R, size = _pose_from_corners(P)
            self.last = c
            self.last_stereo = (c, time.monotonic())
            return dict(centre=c, R=R, size=size, cams=2, err=err), stamp
        for v in found:
            P = mono(v, self.size)
            if P is None:
                continue
            c, R, _ = _pose_from_corners(P)
            if self.last_stereo is not None and time.monotonic() - self.last_stereo[1] < 5.0:
                # one camera judges distance badly (from the blob's size): keep the last
                # two-camera distance from this camera, and take only the direction from it
                d = np.linalg.norm(self.last_stereo[0] - v.cam.origin)
                ray = v.rays.mean(axis=0)
                c = v.cam.origin + ray / np.linalg.norm(ray) * d
            if not (NEAR < np.linalg.norm(c) < FAR and c[2] < 0.05):
                continue
            if self.last is None or np.linalg.norm(c - self.last) > 0.10:
                continue        # one camera alone can't tell a lamp from a phone: only follow a track
            self.last = c
            return dict(centre=c, R=R, size=self.size, cams=1, err=0.0), stamp
        return None, stamp


def _rays_for(self, cam):
    """This view's rays as another calibration would see the same pixels."""
    if cam is self.cam:
        return self.rays
    px = self.cam.project(self.cam.origin + self.rays)[0]
    return cam.rays(px)


View.rays_for = _rays_for
