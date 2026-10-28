"""Tracking from the other side: the phone's selfie camera finds the user's face.

The phone app streams small selfie pictures (cam on). OpenCV's YuNet face detector finds
the face even under the headset; its nose point is used, since the nose is fixed to the
head (the mouth moves when talking). Each picture gives the direction from the phone's
camera to the nose, in the phone's own frame, and how big the face looks.

Everything about the user's face is learned, not given:
- where the nose is in the headset's frame: while the headset's cameras see the phone,
  each nose direction is a ray from a known place; rays from many places meet at the nose;
- how big the face looks at a distance ('face_k'), from the same moments;
- whether the camera's picture is mirrored: both are tried, the one whose rays meet wins.
Once learned, a picture alone says where the phone is: the camera sits on the line back
from the nose, at the distance the face's size says (sightings with their own, much
looser, depth noise).
"""
import collections
import threading
from pathlib import Path

import cv2
import numpy as np

MODEL = str(Path(__file__).resolve().parent / 'models/face_detection_yunet_2023mar.onnx')
CAM_IN_PHONE = np.array([0.0, 0.071, -0.001])   # the selfie camera from the screen's centre, m (Pixel 9 Pro XL)


class FaceTracker:
    def __init__(self, log=print):
        self.log = log
        self.det = cv2.FaceDetectorYN.create(MODEL, '', (320, 320), 0.6)
        self.info = None            # (W, H, fx, fy, cx, cy, orientation)
        self.latest = None          # (time, jpeg bytes)
        self.cond = threading.Condition()
        self.results = collections.deque(maxlen=50)    # (time, ray in phone frame [unmirrored, mirrored], width px, f)
        self.count = 0
        self.on_info = None
        threading.Thread(target=self._run, daemon=True).start()

    def caminfo(self, w, h, fx, fy, cx, cy, orient):
        if fx <= 0:     # no calibration from the phone: a guess (the Pixel's selfie camera sees ~95 deg across)
            fx = fy = (w / 2) / np.tan(np.radians(47.5))
        self.info = (w, h, fx, fy, cx, cy, orient if orient >= 0 else 270)
        if self.on_info:
            self.on_info((w, h, fx, fy, cx, cy, orient))
        self.log('face tracking: camera %dx%d, f %.0f px, turned %d deg' % (w, h, fx, orient))

    def frame(self, t, jpeg):
        with self.cond:
            self.latest = (t, jpeg)
            self.cond.notify()

    def _upright(self):
        """Intrinsics of the picture turned upright (the phone held normally)."""
        w, h, fx, fy, cx, cy, orient = self.info
        k = (orient // 90) % 4                      # quarter turns clockwise
        rot = [None, cv2.ROTATE_90_CLOCKWISE, cv2.ROTATE_180, cv2.ROTATE_90_COUNTERCLOCKWISE][k]
        if k == 1:
            return rot, (fy, fx, h - 1 - cy, cx)
        if k == 2:
            return rot, (fx, fy, w - 1 - cx, h - 1 - cy)
        if k == 3:
            return rot, (fy, fx, cy, w - 1 - cx)
        return rot, (fx, fy, cx, cy)

    def _run(self):
        while True:
            with self.cond:
                while self.latest is None:
                    self.cond.wait()
                t, jpeg = self.latest
                self.latest = None
            if self.info is None:
                continue
            img = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
            if img is None:
                continue
            rot, (fx, fy, cx, cy) = self._upright()
            if rot is not None:
                img = cv2.rotate(img, rot)
            h, w = img.shape[:2]
            self.det.setInputSize((w, h))
            _, faces = self.det.detect(img)
            if faces is None or not len(faces):
                continue
            f = faces[int(np.argmax(faces[:, 14]))]
            u, v = f[8], f[9]                       # the nose tip
            # upright camera axes: x right, y down, z out of the screen; the phone's: x right,
            # y up, z out. Unmirrored, the camera's right is the phone's left.
            x, y = (u - cx) / fx, (v - cy) / fy
            rays = [np.array([-x, -y, 1.0]), np.array([x, -y, 1.0])]     # unmirrored, mirrored
            rays = [r / np.linalg.norm(r) for r in rays]
            self.results.append((t, rays, float(f[2]), fx))
            self.count += 1


class FaceLearner:
    """Where the nose sits in the headset's frame, and how big the face looks (see above).
    Least squares on rays: each ray (origin c, direction r, head frame) adds (I - r r^T)."""
    DECAY = 0.998

    def __init__(self, store, log=print):
        self.v, self.log = store, log
        self.A = [np.array(self.v.get('nose_A%d' % m, np.zeros((3, 3)))) for m in (0, 1)]
        self.b = [np.array(self.v.get('nose_b%d' % m, np.zeros(3))) for m in (0, 1)]
        self.c = [float(self.v.get('nose_c%d' % m, 0.0)) for m in (0, 1)]
        self.n = int(self.v.get('nose_n', 0))

    def add(self, head, C_world, r_world_both):
        Hr, Hp = head[:3, :3], head[:3, 3]
        c = Hr.T @ (C_world - Hp)
        for m in (0, 1):
            r = Hr.T @ r_world_both[m]
            P = np.eye(3) - np.outer(r, r)
            self.A[m] = self.DECAY * self.A[m] + P
            self.b[m] = self.DECAY * self.b[m] + P @ c
            self.c[m] = self.DECAY * self.c[m] + c @ P @ c
        self.n += 1
        if self.n % 50 == 0:
            self._save()

    def _solve(self, m):
        if np.linalg.eigvalsh(self.A[m])[0] < 0.5:     # the rays haven't come from enough directions
            return None, None
        N = np.linalg.solve(self.A[m], self.b[m])
        rss = N @ self.A[m] @ N - 2 * self.b[m] @ N + self.c[m]
        return N, float(np.sqrt(max(rss, 0) / max(np.trace(self.A[m]) / 2, 1)))

    def nose(self):
        """(nose in the head frame, mirrored?, rms miss in m) or None until learned."""
        best = None
        for m in (0, 1):
            N, rms = self._solve(m)
            if N is not None and (best is None or rms < best[2]):
                best = (N, m, rms)
        if best is None or self.n < 100 or best[2] > 0.02:
            return None
        return best

    def _save(self):
        for m in (0, 1):
            self.v['nose_A%d' % m] = self.A[m].tolist()
            self.v['nose_b%d' % m] = self.b[m].tolist()
            self.v['nose_c%d' % m] = self.c[m]
        self.v['nose_n'] = self.n
        nb = self.nose()
        if nb is not None and self.n % 500 == 0:
            self.log('learned: nose at %s mm in the headset frame (%s, rays miss by %.1f mm, %d pictures)'
                     % (np.round(nb[0] * 1e3, 0), 'mirrored' if nb[1] else 'not mirrored', nb[2] * 1e3, self.n))

    def face_k(self):
        return self.v.get('face_k')

    def size_seen(self, dist, width_px, f):
        k = dist * width_px / f
        if 0.05 < k < 0.3:
            old = self.v.get('face_k', k)
            self.v['face_k'] = float(0.98 * old + 0.02 * k)
