"""What Phone Screen learns while it runs, and remembers between runs
(~/.config/phone-screen/learned.json):

- the phone sensor's time lag, from how well its turns line up with the cameras' sightings
  at different time shifts;
- how noisy one-camera and several-camera sightings are, from how far each lands from
  where the tracker expected it (that sets how much each sighting is trusted);
- the pattern brightness (Brightness in phone_screen.py does the trying);
- how much the lit screen's glow widens its outline in each camera ('glow_<camera>'),
  from frames where the markers say exactly where the screen is;
- how noisy outline-only sightings are ('sigma_o');
- how far ahead to draw the screen ('lead', seconds): the drawn screen is compared with
  where the cameras later saw the phone, and the lead grows until it no longer trails;
- each camera's delay ('delay_<camera>', seconds from the moment a picture was taken to
  its time stamp): while the phone's own sensors say it lies still, the cameras must see
  it in one place however the head moves; if it seems to move with the head, the head's
  pose is being taken at the wrong moment, and the delay that keeps it still is learned.
"""
import collections
import json
import threading
import time
from pathlib import Path

import numpy as np

import sensors

FILE = Path.home() / '.config/phone-screen/learned.json'
DEFAULTS = {'lag': 0.0, 'lag_app': 0.0, 'sigma1': 0.008, 'sigma2': 0.004, 'level': 210,
            'scale': 1.0, 'sigma_o': 0.012, 'lead': 0.06}


def load():
    d = dict(DEFAULTS)
    try:
        d.update(json.loads(FILE.read_text()))
    except (OSError, ValueError):
        pass
    return d


class Learner:
    def __init__(self, phone_sensors, log=print):
        self.sens = phone_sensors
        self.log = log
        self.v = load()
        # with our phone app the clocks are really matched, so its lag is learned afresh
        self.lag_key = 'lag_app' if getattr(phone_sensors, 'synced', False) else 'lag'
        self.sens.lag = self.v[self.lag_key]
        self.sightings = collections.deque(maxlen=400)     # (t, world axes C)
        self.next_lag = time.monotonic() + 10
        self.drawn_hist = collections.deque(maxlen=2000)    # (time, position drawn)
        self.sight_hist = collections.deque(maxlen=400)     # (exposure time, position seen)
        self.next_lead = time.monotonic() + 10
        self.next_save = time.monotonic() + 30

    @staticmethod
    def _sigma_key(kind):
        """kind: how many cameras read the markers, or 'outline'."""
        return 'sigma_o' if kind == 'outline' else 'sigma2' if kind > 1 else 'sigma1'

    def sigma(self, kind):
        return self.v[self._sigma_key(kind)]

    def glow(self, cam):
        """How much wider (rad) the lit screen's outline looks in this camera than the screen is."""
        return self.v.get('glow_' + cam, 0.0)

    def glow_seen(self, cam, g):
        if abs(g) < 0.02:
            self.v['glow_' + cam] = float(0.97 * self.glow(cam) + 0.03 * g)

    def sighting(self, t, C, kind, innovation, pred_var=0.0):
        if C is not None:
            self.sightings.append((t, C))
        if innovation is not None:
            key = self._sigma_key(kind)
            # innovation = noise + the prediction's own uncertainty: take that off, then a slow average
            e2 = max(float(innovation @ innovation) / 3 - pred_var, 0.001 ** 2)
            s = np.sqrt(0.98 * self.v[key] ** 2 + 0.02 * min(e2, 0.03 ** 2))
            self.v[key] = float(np.clip(s, 0.002, 0.03 if key == 'sigma_o' else 0.012))

    def scale_seen(self, s, rms):
        """A two-camera fit's idea of the pattern's size (1 = as drawn): a slow average."""
        if rms < 0.004 and 0.9 < s < 1.1:
            self.v['scale'] = float(0.995 * self.v['scale'] + 0.005 * s)

    # ---- each camera's delay, from the phone lying still

    def delay(self, cam):
        return self.v.get('delay_' + cam, 0.008)

    def start_timing(self, heads):
        self.heads = heads
        self.still_group = 0
        self.was_still = False
        self.timing = collections.defaultdict(lambda: collections.deque(maxlen=900))
        threading.Thread(target=self._timing_loop, daemon=True).start()

    def timing_sample(self, cam, stamp, centre_head, still):
        """A camera's own fit of the phone (head frame) at its stamp; still: the phone's
        sensors say it isn't moving. Each spell of stillness is a group: one place."""
        if still and not self.was_still:
            self.still_group += 1
        self.was_still = still
        if still:
            self.timing[cam].append((self.still_group, stamp, np.asarray(centre_head, float)))

    def _spread(self, rows, d):
        """How much a still phone seems to move (m, robust) with the head taken d s before the stamps."""
        groups = collections.defaultdict(list)
        for g, st, c in rows:
            H = self.heads.at(st - d)
            if H is not None:
                groups[g].append(H[:3, :3] @ c + H[:3, 3])
        err = []
        for pts in groups.values():
            if len(pts) >= 10:
                P = np.array(pts)
                e = np.linalg.norm(P - np.median(P, axis=0), axis=1)
                err.extend(np.minimum(e, 0.05))
        return float(np.sqrt(np.mean(np.square(err)))) if len(err) >= 40 else None

    def _timing_loop(self):
        while True:
            time.sleep(10)
            now = time.monotonic()
            for cam in list(self.timing):
                rows = [r for r in self.timing[cam] if now - r[1] < 25]
                rates = [self.heads.turn_rate(st - self.delay(cam)) for _, st, _ in rows]
                if sum(1 for r in rates if r and r > 10) < 20:
                    continue        # the head must move for the timing to show
                d0 = self.delay(cam)
                shifts = d0 + np.arange(-0.04, 0.0401, 0.004)
                fits = [self._spread(rows, d) for d in shifts]
                if any(f is None for f in fits):
                    continue
                best = float(shifts[int(np.argmin(fits))])
                self.v['delay_' + cam] = float(np.clip(d0 + 0.4 * (best - d0), -0.05, 0.3))
                self.log('learned: %s pictures are %.0f ms older than stamped (best %.0f ms; a still phone '
                         'wanders %.1f mm, %.1f mm at the old delay)' % (cam, self.delay(cam) * 1e3, best * 1e3,
                         min(fits) * 1e3, fits[len(fits) // 2] * 1e3))

    def drawn(self, t, pos):
        self.drawn_hist.append((t, pos))

    def sighted(self, t, pos):
        self.sight_hist.append((t, pos))

    def _learn_lead(self, now):
        """How late the drawn screen still is, from the last 10 s while the phone moved."""
        D = np.array([(t, *p) for t, p in self.drawn_hist if now - t < 10])
        S = np.array([(t, *p) for t, p in self.sight_hist if now - t < 10])
        if len(D) < 200 or len(S) < 50:
            return
        speed = np.linalg.norm(np.diff(D[:, 1:], axis=0), axis=1) / np.maximum(np.diff(D[:, 0]), 1e-3)
        if np.percentile(speed, 80) < 0.08:
            return                  # hardly moving: nothing to judge by
        lags = np.arange(-0.1, 0.2001, 0.01)
        fits = []
        for L in lags:
            ok = (S[:, 0] + L > D[0, 0]) & (S[:, 0] + L < D[-1, 0])
            if ok.sum() < 30:
                fits.append(np.inf)
                continue
            e = np.stack([np.interp(S[ok, 0] + L, D[:, 0], D[:, 1 + k]) - S[ok, 1 + k] for k in range(3)], 1)
            fits.append(np.sqrt(np.mean(np.sum(e ** 2, 1))))
        best = float(lags[int(np.argmin(fits))])
        self.v['lead'] = float(np.clip(self.v['lead'] + 0.5 * best, 0.0, 0.2))
        self.log('learned: draw %.0f ms ahead (it trailed by %.0f ms; misfit %.1f mm)'
                 % (self.v['lead'] * 1e3, best * 1e3, min(fits) * 1e3))

    def _misfit(self, shift):
        """Mean angle (rad) between camera axes and sensor axes shifted in time, each fitted
        its own heading so only the timing is judged."""
        S = [self.sens.R_at(t + shift) for t, _ in self.sightings]
        C = [c for _, c in self.sightings]
        num = sum(np.sum(s[2] * c[0] - s[0] * c[2]) for s, c in zip(S, C))
        den = sum(np.sum(s[0] * c[0] + s[2] * c[2]) for s, c in zip(S, C))
        Y = sensors.yaw_matrix(np.arctan2(num, den))
        ang = [np.arccos(np.clip((np.trace(Y @ s @ c.T) - 1) / 2, -1, 1)) for s, c in zip(S, C)]
        return float(np.mean(ang))

    def update(self, now, level):
        self.v['level'] = int(level)
        if now > self.next_lag and self.lag_key == 'lag':     # our app's clocks are matched: nothing to learn
            self.next_lag = now + 10
            recent = [t for t, _ in self.sightings if now - t < 3]
            spread = np.ptp([np.arccos(np.clip(c[1, 1], -1, 1)) for _, c in self.sightings]) \
                if len(self.sightings) > 50 else 0
            if len(recent) > 30 and spread > 0.3:       # needs the phone to have been turned about
                shifts = np.arange(-0.06, 0.0601, 0.01)
                fits = [self._misfit(d) for d in shifts]
                best = float(shifts[int(np.argmin(fits))])
                k = self.lag_key
                self.v[k] = float(np.clip(self.v[k] + 0.5 * best, -0.1, 0.3))
                self.sens.lag = self.v[k]
                self.log('learned: sensor lag %.0f ms (best shift %+.0f ms, misfit %.1f deg); '
                         'sighting noise %.1f / %.1f mm' % (self.v[self.lag_key] * 1e3, best * 1e3,
                         np.degrees(min(fits)), self.v['sigma1'] * 1e3, self.v['sigma2'] * 1e3))
        if now > self.next_lead:
            self.next_lead = now + 10
            self._learn_lead(now)
        if now > self.next_save:
            self.next_save = now + 30
            self.save()

    def save(self):
        FILE.parent.mkdir(parents=True, exist_ok=True)
        FILE.write_text(json.dumps(self.v, indent=1))
