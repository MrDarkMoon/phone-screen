"""The phone's pose from every marker corner every camera saw, in one fit.

Each corner seen is a ray from its camera's centre (head frame). The fit finds the
phone's rotation and position that put the corners on their rays, all cameras at once,
by Gauss-Newton on the angle between each ray and where its corner would be.

The phone's own sensors already know how it's turned (to a degree or so), and that's
the part a camera alone gets most wrong: tilting the phone a little looks almost the
same as moving it, so a wrong tilt drags the position. Given that turn as a prior, the
fit leans on it and the position comes out much steadier.

With two cameras the fit can also tell the pattern's real size (their distance apart
is known), so `scale` can be fitted too and learned over time.
"""
import numpy as np

RAY_SIGMA = 0.0015          # rad: a corner's noise (about half a pixel)


def rodrigues(w):
    a = np.linalg.norm(w)
    if a < 1e-12:
        return np.eye(3)
    k = w / a
    K = np.array([[0, -k[2], k[1]], [k[2], 0, -k[0]], [-k[1], k[0], 0]])
    return np.eye(3) + np.sin(a) * K + (1 - np.cos(a)) * K @ K


def log_rot(R):
    c = np.clip((np.trace(R) - 1) / 2, -1, 1)
    a = np.arccos(c)
    if a < 1e-9:
        return np.zeros(3)
    v = np.array([R[2, 1] - R[1, 2], R[0, 2] - R[2, 0], R[1, 0] - R[0, 1]])
    return v * a / (2 * np.sin(a))


def _tangent(u):
    """Two unit vectors at right angles to each ray (N,3) -> (N,2,3)."""
    a = np.where(np.abs(u[:, :1]) < 0.9, [[1.0, 0, 0]], [[0, 1.0, 0]])
    e1 = np.cross(u, a)
    e1 /= np.linalg.norm(e1, axis=1, keepdims=True)
    e2 = np.cross(u, e1)
    return np.stack([e1, e2], axis=1)


def fit(points, rays, origins, R0, t0, R_prior=None, prior_sigma=0.03, scale=1.0,
        fit_scale=False, iters=8):
    """points (N,3) on the phone, metres; rays (N,3) unit, head frame; origins (N,3) the
    cameras' centres. Returns (R, t, scale, rms angle in rad)."""
    T = _tangent(rays)
    R, t, s = R0.copy(), t0.astype(float).copy(), float(scale)
    n_par = 7 if fit_scale else 6

    def residual(R, t, s):
        p = (s * points) @ R.T + t - origins
        v = p / np.linalg.norm(p, axis=1, keepdims=True)
        r = np.einsum('nij,nj->ni', T, v).ravel() / RAY_SIGMA
        if R_prior is not None:
            r = np.r_[r, log_rot(R_prior.T @ R) / prior_sigma]
        return r

    for _ in range(iters):
        r0 = residual(R, t, s)
        J = np.empty((r0.size, n_par))
        h = 1e-6
        for k in range(n_par):
            if k < 3:
                d = np.zeros(3)
                d[k] = h
                J[:, k] = (residual(rodrigues(d) @ R, t, s) - r0) / h
            elif k < 6:
                d = np.zeros(3)
                d[k - 3] = h
                J[:, k] = (residual(R, t + d, s) - r0) / h
            else:
                J[:, k] = (residual(R, t, s + h) - r0) / h
        step = np.linalg.lstsq(J, -r0, rcond=None)[0]
        R = rodrigues(step[:3]) @ R
        t = t + step[3:6]
        if fit_scale:
            s += step[6]
        if np.linalg.norm(step[:6]) < 1e-7:
            break
    r = residual(R, t, s)[:2 * len(points)]
    return R, t, s, float(np.sqrt(np.mean(r ** 2)) * RAY_SIGMA)


# ---- the lit screen's outline, for when the markers are too small to read

def _gnomonic_basis(u):
    z = u / np.linalg.norm(u)
    x = np.cross([0.0, 1.0, 0.0], z)
    if np.linalg.norm(x) < 1e-6:
        x = np.cross([1.0, 0.0, 0.0], z)
    x /= np.linalg.norm(x)
    return np.stack([x, np.cross(z, x), z])


class Outline:
    """The rays along a lit screen's edge in one camera. With the screen's turn known (the
    phone's sensors), only its position is unknown: the place where the screen's rectangle,
    seen from the camera, lies along those rays. Distances are measured on a flat plane
    tangent to the view (about radians)."""

    def __init__(self, rays, origin, size):
        self.origin = origin
        self.B = _gnomonic_basis(rays.mean(axis=0))
        q = rays @ self.B.T
        self.q = q[:, :2] / q[:, 2:3]
        w, h = size
        self.corners = np.array([[-w / 2, -h / 2, 0], [w / 2, -h / 2, 0], [w / 2, h / 2, 0], [-w / 2, h / 2, 0]])
        self.size = size

    def _poly(self, R, t):
        L = (self.corners @ R.T + t - self.origin) @ self.B.T
        return L[:, :2] / L[:, 2:3]

    def residual(self, R, t):
        """Signed distance of each edge point from the rectangle's edge (outside positive)."""
        P = self._poly(R, t)
        q = self.q
        d = np.full(len(q), np.inf)
        side = []
        for k in range(4):
            a, b = P[k], P[(k + 1) % 4]
            ab = b - a
            s = np.clip(((q - a) @ ab) / (ab @ ab), 0, 1)
            d = np.minimum(d, np.linalg.norm(q - (a + s[:, None] * ab), axis=1))
            side.append(ab[0] * (q[:, 1] - a[1]) - ab[1] * (q[:, 0] - a[0]))
        side = np.array(side)
        turn = np.sign(np.sum(P[:, 0] * np.roll(P[:, 1], -1) - np.roll(P[:, 0], -1) * P[:, 1]))
        inside = np.all(side * turn >= 0, axis=0)
        return np.where(inside, -d, d)

    def start(self, R):
        """A first guess at the position: towards the outline's middle, as far as its size says."""
        q = self.q
        area = 0.5 * abs(np.sum(q[:, 0] * np.roll(q[:, 1], -1) - np.roll(q[:, 0], -1) * q[:, 1]))
        c = q.mean(axis=0)
        u = self.B.T @ np.r_[c, 1.0]
        u /= np.linalg.norm(u)
        facing = max(abs(R[:, 2] @ u), 0.2)
        d = np.sqrt(self.size[0] * self.size[1] * facing / max(area, 1e-6))
        return self.origin + u * d

    def fit(self, R, t0=None, margin=0.0, iters=10):
        """Position t (head frame) for turn R. margin: how much the glow widens the outline.
        Returns (t, rms in rad)."""
        t = self.start(R) if t0 is None else t0.astype(float).copy()
        for _ in range(iters):
            r0 = self.residual(R, t) - margin
            J = np.empty((r0.size, 3))
            for k in range(3):
                d = np.zeros(3)
                d[k] = 1e-5
                J[:, k] = (self.residual(R, t + d) - margin - r0) / 1e-5
            step = np.linalg.lstsq(J, -r0, rcond=None)[0]
            t = t + step
            if np.linalg.norm(step) < 1e-6:
                break
        r = self.residual(R, t) - margin
        return t, float(np.sqrt(np.mean(r ** 2)))
