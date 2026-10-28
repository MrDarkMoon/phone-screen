"""Where the phone is, from the cameras' sightings and the phone's accelerometer (a Kalman
filter on position and velocity, per axis).

The accelerometer moves the estimate between sightings, 100 times a second, so the
screen keeps up with the phone without waiting for the cameras, and a missed frame
or two doesn't show. Each sighting is from the past (the camera's exposure); it's
applied at its own time, and the accelerations since are replayed on top.
"""
import bisect

import numpy as np

ACCEL_NOISE = 1.5           # m/s^2: how far to trust the accelerometer
VEL_DAMP = 2.0              # 1/s: velocity fades without sightings (accelerometers drift)
HISTORY = 0.5               # seconds of states kept for late sightings


class PositionFusion:
    def __init__(self):
        self.reset()
        self.pred_var = 0.0

    def reset(self):
        self.far = 0                # sightings in a row far off the track
        self.t = None
        self.x = None               # (6,) position, velocity
        self.P = None
        self.accels = []            # (t, world acceleration), sorted by t
        self.states = []            # (t, x, P) after each step, for late sightings

    def _predict(self, x, P, dt, a):
        dt = max(0.0, min(dt, 0.1))
        F = np.eye(6)
        F[:3, 3:] = np.eye(3) * dt
        damp = np.exp(-VEL_DAMP * dt)
        F[3:, 3:] *= damp
        x = F @ x
        x[:3] += 0.5 * a * dt * dt
        x[3:] += a * dt * damp
        G = np.zeros((6, 3))
        G[:3] = np.eye(3) * 0.5 * dt * dt
        G[3:] = np.eye(3) * dt
        P = F @ P @ F.T + G @ G.T * ACCEL_NOISE ** 2
        return x, P

    def accel(self, t, a):
        """A world-frame acceleration measured at time t."""
        if self.x is None:
            return
        i = bisect.bisect(self.accels, t, key=lambda e: e[0])
        self.accels.insert(i, (t, a))
        if t >= self.t:
            self.x, self.P = self._predict(self.x, self.P, t - self.t, a)
            self.t = t
            self.states.append((t, self.x.copy(), self.P.copy()))
        self._trim(t)

    def sight(self, t, pos, sigma, cov=None):
        """A camera sighting of the phone's centre at time t, sigma metres of noise (or a
        3x3 covariance, for sightings surer across than in depth).
        Returns how far it was from the prediction (None if it restarted the track)."""
        if cov is None:
            cov = np.eye(3) * sigma ** 2
        sigma = float(np.sqrt(np.trace(cov) / 3))
        if self.x is None or t - self.t > 1.0:
            self.t, self.x = t, np.r_[pos, 0, 0, 0]
            self.P = np.diag([sigma ** 2] * 3 + [0.25] * 3)
            self.states = [(t, self.x.copy(), self.P.copy())]
            self.accels = []
            return None
        # the latest state at or before the sighting
        k = bisect.bisect([s[0] for s in self.states], t) - 1
        if k < 0:
            ts, x, P = self.states[0]
        else:
            ts, x, P = self.states[k]
        a_then = self._accel_before(ts)
        x, P = self._predict(x, P, t - ts, a_then)
        H = np.zeros((3, 6))
        H[:, :3] = np.eye(3)
        S = H @ P @ H.T + cov
        y = pos - x[:3]
        self.pred_var = float(np.trace(P[:3, :3]) / 3)     # how unsure the prediction was
        if y @ np.linalg.solve(S, y) > 50:     # far off the track: a bad sighting, or a jump
            self.far += 1
            if self.far < 4:
                return None                     # ignore the odd one out
            # several in a row: the phone really is somewhere else
            self.far = 0
            self.t, self.x = t, np.r_[pos, 0, 0, 0]
            self.P = np.diag([sigma ** 2] * 3 + [0.25] * 3)
            self.states = [(t, self.x.copy(), self.P.copy())]
            return None
        self.far = 0
        K = P @ H.T @ np.linalg.inv(S)
        x = x + K @ y
        P = (np.eye(6) - K @ H) @ P
        # replay the accelerations since the sighting
        self.states = [s for s in self.states if s[0] <= t] + [(t, x.copy(), P.copy())]
        tt = t
        for ta, a in self.accels:
            if ta <= t:
                continue
            x, P = self._predict(x, P, ta - tt, a)
            tt = ta
            self.states.append((tt, x.copy(), P.copy()))
        self.t, self.x, self.P = tt, x, P
        return y

    def _accel_before(self, t):
        i = bisect.bisect(self.accels, t, key=lambda e: e[0]) - 1
        return self.accels[i][1] if i >= 0 else np.zeros(3)

    def _trim(self, now):
        cut = now - HISTORY
        while self.accels and self.accels[0][0] < cut:
            self.accels.pop(0)
        while len(self.states) > 1 and self.states[0][0] < cut:
            self.states.pop(0)

    def position(self, now):
        """The estimate carried forward to now (which may be a little ahead: see LeadLearner)."""
        if self.x is None:
            return None
        dt = max(0.0, min(now - self.t, 0.25))
        return self.x[:3] + self.x[3:] * dt
