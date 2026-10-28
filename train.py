"""Training mode: you line the real phone up with a frozen picture of it, and tap.

The phone app's Training button starts it. A see-through copy of the tracking pattern
hangs still in VR; you move the real phone (seen through passthrough) until it sits
exactly behind the copy, and tap the screen. At that moment the phone truly is where the
copy is drawn, so the tracker's own estimate then says how far off it is. Each tap is a
sample; their average is a fixed correction in the phone's own frame ('trim' in
learned.json), applied to every pose drawn from then on. After each tap the copy moves
to a new spot nearby, so the samples cover different places and tilts.
"""
import subprocess

import cv2
import numpy as np

import pattern
import sensors

MAX_SAMPLES = 60


def say(text):
    """Spoken feedback through clip-tts, if it is installed; never blocks."""
    try:
        subprocess.Popen(['clip-tts', 'say', text], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        pass


def ghost_image(size=(404, 900)):
    """The tracking pattern as the phone draws it, with a red edge and a cross to line up by."""
    w, h = size
    img = np.full((h, w, 4), 255, np.uint8)
    pxmm = w / pattern.SCREEN_MM[0]
    d = cv2.aruco.getPredefinedDictionary(pattern.DICT)
    s = int(round(pattern.MARKER_MM * pxmm))
    for mid, cy in zip(pattern.MARKER_IDS, pattern.MARKER_CY_MM):
        m = cv2.aruco.generateImageMarker(d, mid, 6)
        m = cv2.resize(m, (s, s), interpolation=cv2.INTER_NEAREST)
        x0 = w // 2 - s // 2
        y0 = int(round(h / 2 - cy * pxmm - s / 2))
        img[y0:y0 + s, x0:x0 + s, :3] = m[:, :, None]
    red = (255, 40, 40, 255)
    cv2.rectangle(img, (2, 2), (w - 3, h - 3), red, 4)
    cv2.line(img, (0, h // 2), (w, h // 2), red, 2)
    cv2.line(img, (w // 2, h // 2 - 20), (w // 2, h // 2 + 20), red, 2)
    return img


def _mean_pose(Ms):
    t = np.mean([M[:3, 3] for M in Ms], axis=0)
    u, _, vt = np.linalg.svd(np.sum([M[:3, :3] for M in Ms], axis=0))
    R = u @ vt
    if np.linalg.det(R) < 0:
        u[:, -1] *= -1
        R = u @ vt
    M = np.eye(4)
    M[:3, :3], M[:3, 3] = R, t
    return M


class Trainer:
    def __init__(self, learner, log=print):
        self.learner, self.log = learner, log
        self.active = False
        self.target = None          # where the copy hangs (world)
        self.image = ghost_image()
        self.rng = np.random.default_rng()
        self.samples = [np.array(m) for m in learner.v.get('trim_samples', [])]

    def trim(self):
        """The learned correction, in the phone's own frame (4x4): drawn = estimate @ trim."""
        return np.array(self.learner.v['trim']) if 'trim' in self.learner.v else np.eye(4)

    def start(self, estimate):
        self.active = True
        self._next(estimate)
        say('Training. Line your phone up with the see-through copy, then tap the screen.')
        self.log('training started (%d samples so far)' % len(self.samples))

    def stop(self):
        if self.active:
            self.active = False
            self.log('training stopped')

    def _next(self, estimate):
        """A new spot for the copy: near where the phone is now, shifted and turned a little."""
        M = estimate @ self.trim()
        shift = self.rng.uniform([-0.05, -0.04, -0.05], [0.05, 0.04, 0.05])
        if len(self.samples) == 0:
            shift *= 0.3            # the first one close by
        turn = sensors.yaw_matrix(np.radians(self.rng.uniform(-15, 15)))
        T = np.eye(4)
        T[:3, :3] = turn @ M[:3, :3]
        T[:3, 3] = M[:3, 3] + shift
        self.target = T

    def tap(self, estimate):
        """The real phone is behind the copy now: estimate (world, without trim) vs the truth."""
        if not self.active or self.target is None or estimate is None:
            return
        X = np.linalg.inv(estimate) @ self.target       # truth in the estimate's own frame
        off = np.linalg.norm(X[:3, 3])
        ang = np.degrees(np.arccos(np.clip((np.trace(X[:3, :3]) - 1) / 2, -1, 1)))
        if off > 0.08 or ang > 25:
            say('That one was too far off. Try again.')
            self.log('training: tap ignored, %.0f mm and %.0f deg off' % (off * 1e3, ang))
            return
        self.samples = (self.samples + [X])[-MAX_SAMPLES:]
        trim = _mean_pose(self.samples)
        spread = np.median([np.linalg.norm(S[:3, 3] - trim[:3, 3]) for S in self.samples]) * 1e3
        self.learner.v['trim'] = trim.tolist()
        self.learner.v['trim_samples'] = [S.tolist() for S in self.samples]
        self.log('training: sample %d was %.0f mm, %.1f deg off; correction now %s mm, %.1f deg '
                 '(samples agree within %.0f mm)' % (len(self.samples), off * 1e3, ang,
                 np.round(trim[:3, 3] * 1e3, 1),
                 np.degrees(np.arccos(np.clip((np.trace(trim[:3, :3]) - 1) / 2, -1, 1))), spread))
        say('Got it. %d.' % len(self.samples))
        self._next(estimate)

    def forget(self):
        self.samples = []
        self.learner.v.pop('trim', None)
        self.learner.v.pop('trim_samples', None)
