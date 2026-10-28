"""Where the headset was, at any moment of the last 30 seconds.

A camera picture is a little old by the time it's searched. Asking SteamVR for the head
pose "N ms ago" makes it guess backwards from now; instead the pose is sampled 200 times
a second here, and a picture's moment is looked up and interpolated.
"""
import bisect
import threading
import time

import numpy as np
import openvr

import solve


def _from34(m):
    M = np.eye(4)
    for r in range(3):
        for c in range(4):
            M[r, c] = m.m[r][c]
    return M


class HeadHistory:
    RATE = 200
    KEEP = 30           # seconds (the timing learner looks back this far)

    def __init__(self, vrsystem):
        self.sys = vrsystem
        self.lock = threading.Lock()
        self.t, self.M = [], []
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        while True:
            now = time.monotonic()
            pose = self.sys.getDeviceToAbsoluteTrackingPose(openvr.TrackingUniverseStanding, 0, 1)[0]
            if pose.bPoseIsValid:
                with self.lock:
                    self.t.append(now)
                    self.M.append(_from34(pose.mDeviceToAbsoluteTracking))
                    if len(self.t) > self.KEEP * self.RATE * 1.2:
                        cut = len(self.t) - self.KEEP * self.RATE
                        del self.t[:cut], self.M[:cut]
            time.sleep(max(0.0, 1 / self.RATE - (time.monotonic() - now)))

    def at(self, t):
        """The head's pose (4x4, world) at time t (our clock), or None if not in the history."""
        with self.lock:
            ts = self.t
            i = bisect.bisect(ts, t)
            if i == 0 or i >= len(ts) or ts[i] - ts[i - 1] > 0.05:
                if ts and 0 <= t - ts[-1] < 0.02:
                    return self.M[-1]   # a moment newer than the last sample: close enough
                return None
            t0, t1, A, B = ts[i - 1], ts[i], self.M[i - 1], self.M[i]
        a = (t - t0) / (t1 - t0)
        M = np.eye(4)
        M[:3, :3] = A[:3, :3] @ solve.rodrigues(a * solve.log_rot(A[:3, :3].T @ B[:3, :3]))
        M[:3, 3] = (1 - a) * A[:3, 3] + a * B[:3, 3]
        return M

    def turn_rate(self, t, dt=0.02):
        """How fast the head was turning at time t, deg/s (None if unknown)."""
        A, B = self.at(t - dt / 2), self.at(t + dt / 2)
        if A is None or B is None:
            return None
        return float(np.degrees(np.linalg.norm(solve.log_rot(A[:3, :3].T @ B[:3, :3]))) / dt)
