"""Phone Screen: your Android phone's screen in VR, on top of the real phone.

The real phone shows a tracking pattern (pattern.py) that the headset's cameras find
(markers.py); the phone's motion sensor (sensors.py) keeps its turning smooth between
sightings. Its picture comes over Wi-Fi from scrcpy's server (video.py) and is drawn as
a SteamVR overlay at the phone's pose (sharedtex.py).
"""
import logging
import os
import sys
import collections
import threading
import time
from pathlib import Path

import numpy as np
import openvr

import facetrack
import fusion
import headhist
import home
import learn
import markers
import pattern
import phonelink
import sensors
import sharedtex
import solve
import train
import video

HOME = Path.home()
LOG_DIR = HOME / '.local/state/phone-screen'
log = logging.getLogger('phone-screen')

HOLD = 0.4          # keep showing the screen this long after the cameras lose it
HOLD_SENSORS = 2.0  # ...or this long while the phone's own sensors still say how it's turned
LEVEL = 210         # the pattern's starting brightness (Brightness learns a better one)
SCREEN = (pattern.SCREEN_MM[0] / 1000, pattern.SCREEN_MM[1] / 1000)


def mat34(M):
    m = openvr.HmdMatrix34_t()
    for r in range(3):
        for c in range(4):
            m.m[r][c] = float(M[r, c])
    return m


def from34(m):
    M = np.eye(4)
    for r in range(3):
        for c in range(4):
            M[r, c] = m.m[r][c]
    return M


class PositionFilter:
    """Median of the last 3 sightings (drops one-frame jumps), then a One Euro filter:
    heavy smoothing while the phone is still, little while it moves."""

    def __init__(self, min_cutoff=0.8, beta=3.0, d_cutoff=1.0):
        self.min_cutoff, self.beta, self.d_cutoff = min_cutoff, beta, d_cutoff
        self.recent = []
        self.x = self.dx = self.t = None

    @staticmethod
    def _alpha(cutoff, dt):
        tau = 1.0 / (2 * np.pi * cutoff)
        return 1.0 / (1.0 + tau / dt)

    def __call__(self, t, x):
        self.recent = (self.recent + [x])[-3:]
        x = np.median(np.array(self.recent), axis=0)
        if self.x is None:
            self.x, self.dx, self.t = x, np.zeros(3), t
            return x
        dt = max(t - self.t, 1e-3)
        self.t = t
        dx = (x - self.x) / dt
        self.dx = self.dx + self._alpha(self.d_cutoff, dt) * (dx - self.dx)
        cutoff = self.min_cutoff + self.beta * np.linalg.norm(self.dx)
        self.x = self.x + self._alpha(cutoff, dt) * (x - self.x)
        return self.x


class Brightness:
    """Learns the pattern brightness the cameras like best: every few seconds it tries a
    step brighter or dimmer, and keeps it if the markers were seen more (hill climbing)."""
    STEP, LOW, HIGH, TRIAL = 12, 190, 255, 3.0   # below ~150 the cameras lose the markers

    def __init__(self, level):
        self.level = min(self.HIGH, max(self.LOW, level))
        self.best = None            # (score, level) to beat
        self.trial_start = None
        self.score = 0.0
        self.direction = -1

    def sighting(self, p):
        self.score += min(p['cams'], 3) * (1 + p['corners'] / 8)

    def update(self, now, phone_seen):
        """Returns a new level to show, or None."""
        if self.trial_start is None:
            self.trial_start, self.score = now, 0.0
            return None
        if now - self.trial_start < self.TRIAL:
            return None
        score, self.trial_start, self.score = self.score, now, 0.0
        if not phone_seen:
            return None                 # nothing to judge by: keep it as it is
        if self.best is None or score >= self.best[0] * 0.95:
            self.best = (score, self.level)
        else:                           # worse: go back, and try the other way next
            self.level = self.best[1]
            self.direction = -self.direction
            self.best = (self.best[0] * 0.9, self.level)   # forget slowly: the room changes
            return self.level
        nxt = min(self.HIGH, max(self.LOW, self.level + self.direction * self.STEP))
        if nxt == self.level:
            self.direction = -self.direction
            return None
        self.level = nxt
        return nxt


def orthonormal(R):
    u, _, vt = np.linalg.svd(R)
    return u @ vt


class App:
    def __init__(self):
        self.ov = openvr.IVROverlay()
        self.sys = openvr.VRSystem()
        self.handle = self.ov.createOverlay('phonescreen.screen', 'Phone screen')
        self.ov.setOverlaySortOrder(self.handle, 5)
        self.tex = sharedtex.SharedTexture(self.ov, self.handle)
        self.tracker = markers.MarkerTracker()
        self.accq = collections.deque()
        if os.environ.get('PHONE_WEB'):     # the old way: pattern in the browser, Sensor Server app
            self.page = pattern.PatternServer(log=log.info, on_touch=self.on_touch)
            self.sensors = sensors.PhoneSensors(log=log.info, on_accel=lambda t, a: self.accq.append((t, a)))
            port = pattern.PORT
        else:                               # our phone app (android/) does both
            self.page = self.sensors = phonelink.PhoneLink(
                log=log.info, on_touch=self.on_touch, on_accel=lambda t, a: self.accq.append((t, a)))
            port = phonelink.PORT
        self.page_seen = False
        threading.Thread(target=self.keep_reverse, args=(port,), daemon=True).start()
        # apps run on a virtual display the size of the real screen; only VR shows it
        self.video = video.PhoneVideo(max_size=900, log=log.info, new_display='1008x2244/360',
                                     audio=True)
        self.home = home.Home(self.video, log=log.info)
        self.home_version = -1
        threading.Thread(target=self.watch_apps, daemon=True).start()
        self.fusion = fusion.PositionFusion()
        self.learner = learn.Learner(self.sensors, log=log.info)
        self.bright = Brightness(self.learner.v['level'])
        self.trainer = train.Trainer(self.learner, log=log.info)
        self.training_shown = False
        self.raw_pose = None        # the tracker's estimate before the trained correction
        self.taps = {}              # training: pointer -> (time, x, y) it went down
        self.offset = None          # heading of the sensors' frame in the VR world, radians
        self.centre = None
        self.cam_R = None           # the screen's axes from the last sighting, world
        self.pose = None
        self.seen = 0.0
        self.shown = False
        self.frame_no = -1
        self.uploaded = 0.0
        self.stats = [0, 0, 0, 0]   # sightings: markers in one camera, in two or more; outline only; face
        self.rec = open(LOG_DIR / 'track.csv', 'w', buffering=1) if os.environ.get('PHONE_REC') else None
        self.face = facetrack.FaceTracker(log=log.info)
        self.facelearn = facetrack.FaceLearner(self.learner.v, log=log.info)
        self.page.face = self.face
        # the phone sends it only when its stream starts; until then, the last one (or a guess)
        self.face.caminfo(*self.learner.v.get('caminfo', (640, 480, 380.0, 380.0, 320.0, 240.0, 270)))
        self.face.on_info = lambda info: self.learner.v.__setitem__('caminfo', list(info))
        self.cam_on = False
        self.face_done = 0          # face results handled so far
        self.marker_sights = collections.deque(maxlen=60)  # (time, centre, axes): the face learns from these
        self.sightq = collections.deque()
        self.drops = collections.Counter()  # what happened to camera finds, for the log
        self.heads = headhist.HeadHistory(self.sys)
        self.learner.start_timing(self.heads)
        threading.Thread(target=self.sight_loop, daemon=True).start()

    def on_touch(self, action, pts):
        """Touches on the real screen (the pattern page) go to the apps' virtual display;
        in training, a tap says the phone is lined up."""
        if self.trainer.active:
            for pt in pts:
                if action == 'down':
                    self.taps[pt['id']] = (time.monotonic(), pt['x'], pt['y'])
                elif action == 'up' and pt['id'] in self.taps:
                    t0, x0, y0 = self.taps.pop(pt['id'])
                    if time.monotonic() - t0 < 0.5 and abs(pt['x'] - x0) + abs(pt['y'] - y0) < 0.05:
                        self.trainer.tap(self.raw_pose)
            return
        a = {'down': video.DOWN, 'up': video.UP, 'move': video.MOVE}[action]
        for pt in pts:
            pid, p = int(pt['id']) & 0xffff, float(pt.get('p') or 1.0)
            if not self.home.touch(action, pid, pt['x'], pt['y'], p):
                self.video.touch(a, pid, pt['x'], pt['y'], p)

    def keep_reverse(self, port):
        """adb reverse brings the phone's 127.0.0.1:port here. It's lost when the phone's
        Wi-Fi debugging reconnects, so it's set again whenever the phone isn't connected."""
        while True:
            if not self.page.connected:
                video.adb('reverse', 'tcp:%d' % port, 'tcp:%d' % port)
            time.sleep(5)

    def watch_apps(self):
        """Back to our home screen when the open app closes (Android shows its own launcher)."""
        while True:
            time.sleep(1.5)
            if self.home.mode != 'app' or time.monotonic() - self.home.opened < 3:
                continue
            out = video.adb('shell', 'dumpsys activity activities | grep -A3 "^Display #[1-9]" | grep topResumed').stdout
            if 'SecondaryDisplayLauncher' in out:
                self.home.go_home()

    def cam_time(self, name, stamp_ns):
        """When a camera's picture was really taken (our clock): its stamp, less its delay."""
        return stamp_ns / 1e9 - self.learner.delay(name)

    def sight_loop(self):
        """The cameras' side, in its own thread: a full search can take 80 ms, and the
        screen mustn't wait for it. Each sighting goes to the main loop with its fitted pose.
        Every camera's picture gets the head's pose at its own moment (headhist.py)."""
        while True:
            try:
                p, stamp = self.tracker.step()
            except Exception:
                log.exception('camera search failed')
                time.sleep(1)
                continue
            if p is None:
                time.sleep(0.003)
                continue
            self.drops['found'] += 1
            items = p['cam_obs'] if p['kind'] == 'markers' else p['outlines']
            times = [self.cam_time(o['name'], o['stamp']) for o in items]
            heads = [self.heads.at(t) for t in times]
            if any(h is None for h in heads):
                self.drops['no head pose'] += 1
                continue
            tc = float(np.mean(times))
            try:
                fitted = self.refine(p, heads, tc)
            except (np.linalg.LinAlgError, ValueError) as e:
                self.drops[str(e)[:30]] += 1
                continue
            self.sightq.append((tc, heads[0], p, fitted))
            if p['kind'] == 'markers':     # the timing learner: is the phone lying still?
                still = self.sensors.still()
                for o in p['cam_obs']:
                    self.learner.timing_sample(o['name'], o['stamp'] / 1e9, o['centre'], still)

    def refine(self, p, heads, tc):
        """One fit over every corner every camera saw (solve.py), in the world: each camera's
        rays placed by the head's pose at that camera's moment. It leans on the phone's own
        sense of its turn; with no markers readable it uses the screen's lit outline.
        Returns the screen's centre and axes in the world, the fit's angle error, and the
        centre in the (first camera's) head frame."""
        prior = None
        if self.sensors.fresh() and self.offset is not None:
            prior = sensors.yaw_matrix(self.offset) @ self.sensors.R_at(tc)
        if p['kind'] == 'outline':
            return self.from_outlines(p, heads, prior)
        pts, rays, orgs = [], [], []
        for o, H in zip(p['cam_obs'], heads):
            pts.append(o['points'])
            rays.append(o['rays'] @ H[:3, :3].T)
            orgs.append(o['origins'] @ H[:3, :3].T + H[:3, 3])
        H0 = heads[0]
        R0 = prior if prior is not None else H0[:3, :3] @ p['R']
        t0 = H0[:3, :3] @ p['centre'] + H0[:3, 3]
        R, t, _, rms = solve.fit(np.concatenate(pts), np.concatenate(rays), np.concatenate(orgs), R0, t0,
                                 R_prior=prior)
        if rms > 0.003 or not (0.12 < np.linalg.norm(t - H0[:3, 3]) < 1.2):
            raise ValueError('bad fit')
        for ol in p['outlines']:        # the same frame's outline: how much does the glow widen it?
            H = self.heads.at(self.cam_time(ol['name'], ol['stamp']))
            if H is None:
                continue
            res = solve.Outline(ol['rays'], ol['origin'], SCREEN).residual(H[:3, :3].T @ R, H[:3, :3].T @ (t - H[:3, 3]))
            self.learner.glow_seen(ol['name'], float(np.median(res)))
        return t, R, rms, H0[:3, :3].T @ (t - H0[:3, 3])

    def from_outlines(self, p, heads, prior):
        """Where the screen is from its lit outline, its turn known from the phone's sensors."""
        if prior is None:
            raise ValueError('no turn to fit the outline with')
        fits = []
        for ol, H in zip(p['outlines'], heads):
            Hr = H[:3, :3]
            o = solve.Outline(ol['rays'], ol['origin'], SCREEN)
            t, rms = o.fit(Hr.T @ prior, margin=self.learner.glow(ol['name']))
            if rms < 0.004 and 0.15 < np.linalg.norm(t) < 2.0:
                fits.append((rms, Hr @ t + H[:3, 3]))
        if not fits:
            raise ValueError('no outline fitted')
        w = np.array([1 / (r + 5e-4) for r, _ in fits])
        t = np.sum([wi * f[1] for wi, f in zip(w, fits)], axis=0) / w.sum()
        H0 = heads[0]
        return t, prior, min(r for r, _ in fits), H0[:3, :3].T @ (t - H0[:3, 3])

    def learn_heading(self, C, t):
        """Line the phone's sensor frame up with the world, from axes C sighted at time t."""
        S = self.sensors.R_at(t)
        a = np.arctan2(np.sum(S[2] * C[0] - S[0] * C[2]), np.sum(S[0] * C[0] + S[2] * C[2]))
        err = np.linalg.norm(sensors.yaw_matrix(a) @ S - C)
        if err > 0.35:          # the cameras and the sensor disagree: a bad sighting
            return
        if self.offset is None:
            self.offset = a
            log.info('heading learned (fit %.2f)', err)
        else:
            d = (a - self.offset + np.pi) % (2 * np.pi) - np.pi
            self.offset += 0.05 * d

    def orientation(self):
        """The screen's axes in the world: x right, y up (portrait), z out of the screen."""
        if self.sensors.fresh() and self.offset is not None:
            R = sensors.yaw_matrix(self.offset) @ self.sensors.R_ahead(time.monotonic() + self.learner.v['lead'])
        elif self.cam_R is not None:
            R = self.cam_R
        else:
            return None
        return R

    @staticmethod
    def picture_axes(R, landscape):
        """Overlay axes: x right, y up as the picture shows (landscape: whichever side is higher)."""
        x, y, z = R[:, 0], R[:, 1], R[:, 2]
        if landscape:
            x, y = (-y, x) if x[1] > 0 else (y, -x)
        return np.stack([x, y, z], axis=1)

    def step(self):
        now = time.monotonic()
        req = LOG_DIR / 'snap-request'     # touch this file to get selfie-camera pictures from the phone
        if req.exists() and hasattr(self.page, 'snap'):
            req.unlink()
            self.page.snap(4)
        if self.rec:            # P: the head's pose now, for working out the cameras' timing later
            hp = self.sys.getDeviceToAbsoluteTrackingPose(openvr.TrackingUniverseStanding, 0, 1)[0]
            if hp.bPoseIsValid:
                self.rec.write('P,%.5f,%s\n' % (now, ','.join('%.6f' % v for v in
                                                              from34(hp.mDeviceToAbsoluteTracking)[:3].ravel())))
        if self.page.connected and not self.page_seen:
            self.page.set_level(self.bright.level)
        self.page_seen = self.page.connected
        self.learner.update(now, self.bright.level)
        level = self.bright.update(now, now - self.seen < 1.0)
        if level is not None:
            self.page.set_level(level)
        while self.accq:
            t, a = self.accq.popleft()
            if self.offset is not None:
                self.fusion.accel(t, sensors.yaw_matrix(self.offset) @ a)
        frame = self.video.frame
        landscape = bool(frame and frame[1] > frame[2]) and self.home.mode == 'app'
        while self.sightq:
            tc, head, p, (centre, R_fit, rms, t_head) = self.sightq.popleft()
            self.stats[2 if p['kind'] == 'outline' else min(p['cams'], 2) - 1] += 1
            outline = p['kind'] == 'outline'
            C = None if outline else head[:3, :3] @ p['R']   # the cameras' own idea of the turn: the heading learns from it
            self.cam_R = R_fit
            self.bright.sighting(p)
            if self.sensors.fresh() and C is not None:
                self.learn_heading(C, tc)
            if self.rec:        # H: the same sighting in the head's frame (exposure time, centre)
                self.rec.write('H,%.5f,%.5f,%.5f,%.5f\n' % (tc, *t_head))
                for name, st, tcam, n in p.get('percam', []):     # C: each camera's own fit (frame time, centre)
                    self.rec.write('C,%s,%.5f,%d,%.5f,%.5f,%.5f\n' % (name, st / 1e9, n, *tcam))
            if self.rec:        # S: a sighting (when, cameras, centre, fit error, screen axes)
                self.rec.write('S,%.4f,%.4f,%d,%.5f,%.5f,%.5f,%.5f,%s\n' % (
                    now, tc, p['cams'], *centre, rms, ','.join('%.5f' % v for v in R_fit.T.ravel())))
            kind = 'outline' if outline else p['cams']
            self.learner.sighted(tc, centre)
            if not outline:
                self.marker_sights.append((tc, centre, R_fit))
            y = self.fusion.sight(tc, centre, self.learner.sigma(kind))
            self.learner.sighting(tc, C, kind, y, self.fusion.pred_var)
            self.seen = now
        self.face_step(now)
        hold = HOLD_SENSORS if self.sensors.fresh() and self.offset is not None else HOLD
        self.centre = self.fusion.position(now + self.learner.v['lead'])
        if self.centre is not None and now - self.seen < hold:
            R0 = self.orientation()
            if R0 is not None:
                raw = np.eye(4)
                raw[:3, :3], raw[:3, 3] = R0, self.centre
                self.raw_pose = raw
                trimmed = raw @ self.trainer.trim()         # what training taught
                R = self.picture_axes(trimmed[:3, :3], landscape)
                M = np.eye(4)
                M[:3, :3] = R
                M[:3, 3] = trimmed[:3, 3] + R[:, 2] * 0.002
                self.pose = M
                self.learner.drawn(now, M[:3, 3])
                if self.rec:        # D: what's drawn (when, centre, screen axes)
                    self.rec.write('D,%.4f,%.5f,%.5f,%.5f,%s\n' % (
                        now, *M[:3, 3], ','.join('%.5f' % v for v in R.T.ravel())))
                if not self.trainer.active:
                    self.ov.setOverlayWidthInMeters(self.handle, float(SCREEN[1] if landscape else SCREEN[0]))
                    self.ov.setOverlayTransformAbsolute(self.handle, openvr.TrackingUniverseStanding, mat34(M))
        if self.training_step():
            return
        if self.home.mode != 'app':
            if self.home.version != self.home_version:
                self.home_version = self.home.version
                self.tex.show(self.home.image())
            self.frame_no = -1
        elif frame and self.video.frame_no != self.frame_no and now - self.uploaded > 0.03:
            self.frame_no = self.video.frame_no
            self.uploaded = now
            try:
                self.tex.show(frame[0])
            except Exception as e:
                log.info('picture %dx%d not shown: %s', frame[1], frame[2], e)
                self.uploaded = now + 2.0
        show = (frame is not None or self.home.mode != 'app') and self.pose is not None and now - self.seen < hold
        if show != self.shown:
            self.shown = show
            (self.ov.showOverlay if show else self.ov.hideOverlay)(self.handle)
            log.info('screen %s', 'shown' if show else 'hidden')

    def face_step(self, now):
        """The selfie camera's view (facetrack.py): learn the face while the markers say where
        the phone is; when they don't, the face says it."""
        tracking = getattr(self.page, 'tracking', False)
        if tracking and not self.cam_on:
            self.page.send('cam on')
        self.cam_on = tracking
        while self.face.count > self.face_done and self.face.results:
            self.face_done = self.face.count
            t, rays, width, f = self.face.results[-1]
            head = self.heads.at(t)
            if head is None:
                continue
            near = [m for m in self.marker_sights if abs(m[0] - t) < 0.03]
            if near:                    # the markers know where the phone was: learn from it
                _, centre, R = min(near, key=lambda m: abs(m[0] - t))
                C = centre + R @ facetrack.CAM_IN_PHONE
                self.facelearn.add(head, C, [R @ r for r in rays])
                nb = self.facelearn.nose()
                if nb is not None:
                    self.facelearn.size_seen(np.linalg.norm(head[:3, :3] @ nb[0] + head[:3, 3] - C), width, f)
                continue
            if self.marker_sights and t - self.marker_sights[-1][0] < 0.15:
                continue                # the markers will be back in a moment
            nb, k = self.facelearn.nose(), self.facelearn.face_k()
            if nb is None or k is None or self.offset is None or not self.sensors.fresh():
                continue
            N, mirrored, _ = nb
            R = sensors.yaw_matrix(self.offset) @ self.sensors.R_at(t)
            r = R @ rays[mirrored]
            s = k * f / width                               # how far the face is
            C = head[:3, :3] @ N + head[:3, 3] - s * r      # the camera, back along the ray from the nose
            centre = C - R @ facetrack.CAM_IN_PHONE
            lat, dep = 0.004 + 0.02 * s, 0.08 * s           # surer across than in depth
            cov = np.eye(3) * lat ** 2 + np.outer(r, r) * (dep ** 2 - lat ** 2)
            self.fusion.sight(t, centre, None, cov=cov)
            self.stats[3] += 1
            self.seen = now

    def training_step(self):
        """Training mode (train.py): a see-through copy held still. True while it's on."""
        want = getattr(self.page, 'training', False)
        if want and not self.trainer.active and self.raw_pose is not None:
            self.trainer.start(self.raw_pose)
        elif not want and self.trainer.active:
            self.trainer.stop()
        if not self.trainer.active:
            if self.training_shown:         # back to the normal picture
                self.training_shown = False
                self.ov.setOverlayAlpha(self.handle, 1.0)
                self.home_version = self.frame_no = -1
            return False
        if not self.training_shown:
            self.training_shown = True
            self.tex.show(self.trainer.image)
            self.ov.setOverlayAlpha(self.handle, 0.55)
            self.ov.setOverlayWidthInMeters(self.handle, float(SCREEN[0]))
        T = self.trainer.target.copy()
        T[:3, 3] += T[:3, 2] * 0.002
        self.ov.setOverlayTransformAbsolute(self.handle, openvr.TrackingUniverseStanding, mat34(T))
        if not self.shown:
            self.shown = True
            self.ov.showOverlay(self.handle)
        return True

    def run(self, exit_idle=None):
        """exit_idle: return once the phone app hasn't been tracking for this many seconds
        (the daemon runs it that way)."""
        last_log = time.monotonic()
        sensor_count = 0
        active = time.monotonic()
        while True:
            t0 = time.monotonic()
            if getattr(self.page, 'tracking', True):
                active = t0
            elif exit_idle and t0 - active > exit_idle:
                log.info('the phone stopped tracking: closing')
                self.close()
                return
            self.step()
            if t0 - last_log > 30:
                log.info('last 30 s: marker sightings by one camera %d, by more %d; outline only %d; face %d; '
                         'sensor readings %d; pattern level %d; glow %s', *self.stats,
                         self.sensors.count - sensor_count, self.bright.level,
                         {k[5:]: round(v * 1e3, 2) for k, v in self.learner.v.items() if k.startswith('glow_')})
                log.info('camera finds: %s; face pictures %d', dict(self.drops), self.face.count)
                self.drops.clear()
                sensor_count = self.sensors.count
                self.stats = [0, 0, 0, 0]
                last_log = t0
            time.sleep(max(0.0, 1 / 90 - (time.monotonic() - t0)))


    def close(self):
        """Put the headset back as it was: no overlay, no picture stream from the phone."""
        self.learner.save()
        self.ov.hideOverlay(self.handle)
        self.ov.destroyOverlay(self.handle)
        self.video.stop = True
        if self.video.proc:
            self.video.proc.kill()
        video.adb('shell', 'pkill -f com.genymobile.scrcpy')


def main():
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(message)s',
                        handlers=[logging.StreamHandler(),
                                  logging.FileHandler(LOG_DIR / 'phone-screen.log')])
    # Frametop's desktop session hides SteamVR's path registry behind its own config dir.
    os.environ['XDG_CONFIG_HOME'] = str(HOME / '.config')
    openvr.init(openvr.VRApplication_Background)
    openvr.shutdown()
    openvr.init(openvr.VRApplication_Overlay)
    log.info('started')
    idle = os.environ.get('PHONE_EXIT_IDLE')
    App().run(exit_idle=float(idle) if idle else None)


if __name__ == '__main__':
    sys.exit(main())
