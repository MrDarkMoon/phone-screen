"""The phone's motion sensors via the Sensor Server app (WebSocket): its orientation (game
rotation vector) and its acceleration without gravity (linear acceleration).

The game rotation vector is gravity-aligned but its heading is arbitrary and drifts
slowly, so the caller supplies the heading offset (learned from the cameras).

Readings carry the phone's own timestamps. The phone's clock is put onto ours by the
smallest delay seen (arrival minus phone time): Wi-Fi only ever adds delay, so the
smallest is the truest, and it keeps learning as readings come in.
"""
import asyncio
import collections
import json
import os
import threading
import time
import urllib.parse

import numpy as np
import websockets

PHONE_IP = os.environ.get('PHONE_IP', '')     # only for the old Sensor Server way (PHONE_WEB=1)
ROTATION = 'android.sensor.game_rotation_vector'
ACCEL = 'android.sensor.linear_acceleration'
URL = 'ws://{}:8080/sensors/connect?types=' + urllib.parse.quote(json.dumps([ROTATION, ACCEL]))

# Android's world frame (x east-ish, y north-ish, z up) -> SteamVR's (x right, y up, z back)
VR_FROM_ANDROID = np.array([[1.0, 0, 0], [0, 0, 1.0], [0, -1.0, 0]])


def quat_to_matrix(x, y, z, w):
    return np.array([
        [1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
        [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
        [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)]])


def _exp_rot(w):
    a = np.linalg.norm(w)
    if a < 1e-12:
        return np.eye(3)
    k = w / a
    K = np.array([[0, -k[2], k[1]], [k[2], 0, -k[0]], [-k[1], k[0], 0]])
    return np.eye(3) + np.sin(a) * K + (1 - np.cos(a)) * K @ K


def _log_rot(R):
    a = np.arccos(np.clip((np.trace(R) - 1) / 2, -1, 1))
    if a < 1e-9:
        return np.zeros(3)
    return np.array([R[2, 1] - R[1, 2], R[0, 2] - R[2, 0], R[1, 0] - R[0, 1]]) * a / (2 * np.sin(a))


def yaw_matrix(a):
    c, s = np.cos(a), np.sin(a)
    return np.array([[c, 0, s], [0, 1.0, 0], [-s, 0, c]])


class PhoneSensors:
    def __init__(self, ip=PHONE_IP, log=print, on_accel=None, connect=True):
        self.url = URL.format(ip)
        self.log = log
        self.on_accel = on_accel    # called with (time, acceleration in the phone's sensor world)
        self.R = None               # phone axes (x screen-right, y screen-up, z out of screen)
        self.when = 0.0             # in a gravity-aligned VR-style frame of unknown heading
        self.count = 0
        self.clock = None           # our time minus the phone's, seconds (smallest seen)
        self.lag = 0.0              # how much older readings are than that says (learned)
        self.clock_set = 0.0
        self.history = collections.deque(maxlen=400)    # (time, R) for the last ~2 s
        self.accel_hist = collections.deque(maxlen=400) # (time, |acceleration|)
        if connect:
            threading.Thread(target=lambda: asyncio.run(self._run()), daemon=True).start()

    def _time(self, phone_ns):
        now = time.monotonic()
        d = now - phone_ns / 1e9
        # follow the smallest delay; let it creep up slowly in case the clocks drift apart
        if self.clock is None or d < self.clock or now - self.clock_set > 30:
            self.clock = d if self.clock is None or d < self.clock else self.clock + 0.0005
            self.clock_set = now
        return phone_ns / 1e9 + self.clock - self.lag

    async def _run(self):
        while True:
            try:
                async with websockets.connect(self.url, open_timeout=5) as ws:
                    self.log('sensors: connected')
                    self.clock = None
                    async for msg in ws:
                        m = json.loads(msg)
                        self.reading(m.get('type'), self._time(m['timestamp']), m['values'])
            except Exception as e:      # app stopped, Wi-Fi dropped...
                self.log('sensors: %s' % e)
            await asyncio.sleep(2)

    def reading(self, kind, t, v):
        """One reading at time t (our clock)."""
        if kind == ROTATION:
            x, y, z = v[:3]
            w = v[3] if len(v) > 3 and v[3] >= 0 else np.sqrt(max(0.0, 1 - x * x - y * y - z * z))
            self.R = VR_FROM_ANDROID @ quat_to_matrix(x, y, z, w)
            self.when = time.monotonic()
            self.history.append((t, self.R))
            self.count += 1
        elif kind == ACCEL and self.R is not None:
            a = np.asarray(v[:3], float)
            self.accel_hist.append((t, float(np.linalg.norm(a))))
            if self.on_accel:
                self.on_accel(t, self.R @ a)

    def fresh(self, max_age=0.5):
        return self.R is not None and time.monotonic() - self.when < max_age

    def still(self, span=0.5):
        """True if the phone's own sensors say it hasn't moved for the last span seconds
        (turning under 2 deg/s, accelerating under 0.25 m/s^2)."""
        h = list(self.history)
        a = list(self.accel_hist)
        if len(h) < 10 or len(a) < 10 or not self.fresh():
            return False
        t_end = h[-1][0]
        h = [x for x in h if t_end - x[0] <= span]
        a = [x for x in a if t_end - x[0] <= span]
        if len(h) < 5 or h[-1][0] - h[0][0] < span * 0.8:
            return False
        turned = np.degrees(np.linalg.norm(_log_rot(h[0][1].T @ h[-1][1])))
        return turned < 2 * span and max(m for _, m in a) < 0.25

    def R_ahead(self, t):
        """Orientation carried forward to time t (our clock) at the rate it's turning now."""
        h = list(self.history)[-12:]
        if len(h) < 2:
            return self.R
        (t0, R0), (t1, R1) = h[0], h[-1]
        if t1 - t0 < 0.01:
            return R1
        w = _log_rot(R0.T @ R1) / (t1 - t0)          # turning rate, in the phone's own axes
        return R1 @ _exp_rot(w * max(0.0, min(t - t1, 0.2)))

    def R_at(self, t):
        """Orientation nearest to time t (our clock), or the latest."""
        best = None
        for tt, R in reversed(list(self.history)):
            if best is None or abs(tt - t) < abs(best[0] - t):
                best = (tt, R)
            if tt < t:
                break
        return best[1] if best else self.R
