"""Replay a recorded track (PHONE_REC=1 writes ~/.local/state/phone-screen/track.csv) through
the position filter offline: no headset, phone or SteamVR needed. Change fusion.py (or
its numbers with --set) and see how the result compares with what was recorded.

    venv/bin/python replay.py [track.csv] [--set ACCEL_NOISE=2.0 ...] [--out replayed.csv]

It feeds the fusion exactly what it got live, in the same order: accelerations (A rows),
sightings (F rows) and the main loop's questions (N rows: where is the phone, so far ahead).
It then reports:
- replay match: how far the replayed answers are from the recorded ones (about 0 with
  fusion.py unchanged; this says the replay is faithful);
- surprise: how far each sighting was from the filter's guess just before it;
- drawn error: where the screen was drawn against where the cameras (two or more) later
  saw the phone at that moment; the number that matters most;
- jitter: how much the drawn position shakes from frame to frame.

Older recordings have no A, F or N rows: then the S rows are used as sightings (with the
noise from learned.json), no accelerations, and the drawn times (D rows) as the questions.
"""
import argparse
import bisect
import csv
import sys
from pathlib import Path

import numpy as np

import fusion
import learn

DEFAULT = Path.home() / '.local/state/phone-screen/track.csv'


def read(path):
    rows = []
    with open(path, newline='') as f:
        for r in csv.reader(f):
            if r and r[0] in 'AFNSD':
                rows.append(r)
    return rows


def events(rows):
    """('a', t, a) | ('s', t, kind, pos, sigma, cov) | ('n', now, lead, recorded centre or None)."""
    new = any(r[0] in 'AFN' for r in rows)
    if new:
        for r in rows:
            if r[0] == 'A':
                yield 'a', float(r[1]), np.array([float(v) for v in r[2:5]])
            elif r[0] == 'F':
                pos = np.array([float(v) for v in r[3:6]])
                vals = [float(v) for v in r[6:]]
                cov = np.array(vals).reshape(3, 3) if len(vals) == 9 else None
                yield 's', float(r[1]), r[2], pos, (vals[0] if len(vals) == 1 else None), cov
            elif r[0] == 'N':
                yield 'n', float(r[1]), float(r[2]), (np.array([float(v) for v in r[3:6]]) if len(r) >= 6 else None)
        return
    v = learn.load()
    lead = v['lead']
    for r in rows:
        if r[0] == 'S':
            cams = int(r[3])
            kind = '1' if cams == 1 else '2'
            yield 's', float(r[2]), kind, np.array([float(x) for x in r[4:7]]), \
                v['sigma1'] if cams == 1 else v['sigma2'], None
        elif r[0] == 'D':
            yield 'n', float(r[1]) - lead, lead, None


def replay(evs):
    f = fusion.PositionFusion()
    drawn, surprise, match, truth, out = [], [], [], [], []
    for e in evs:
        if e[0] == 'a':
            f.accel(e[1], e[2])
        elif e[0] == 's':
            _, t, kind, pos, sigma, cov = e
            y = f.sight(t, pos, sigma, cov=cov)
            if y is not None:
                surprise.append(np.linalg.norm(y))
            if kind not in ('1', 'outline', 'face'):        # two or more cameras: the best truth there is
                truth.append((t, pos))
        else:
            _, now, lead, rec = e
            c = f.position(now + lead)
            if c is None:
                continue
            drawn.append((now + lead, c))
            out.append((now, c))
            if rec is not None:
                match.append(np.linalg.norm(c - rec))
    return drawn, surprise, match, truth, out


def drawn_error(drawn, truth):
    """For each true sighting, the drawn position meant for that moment (interpolated)."""
    if len(drawn) < 2:
        return []
    ts = [d[0] for d in drawn]
    err = []
    for t, pos in truth:
        i = bisect.bisect(ts, t)
        if i == 0 or i == len(ts) or ts[i] - ts[i - 1] > 0.05:
            continue
        a = (t - ts[i - 1]) / (ts[i] - ts[i - 1])
        c = (1 - a) * drawn[i - 1][1] + a * drawn[i][1]
        err.append(np.linalg.norm(c - pos))
    return err


def jitter(drawn):
    """RMS of the second difference of consecutive drawn positions (mm), gaps skipped."""
    d = []
    for (t0, a), (t1, b), (t2, c) in zip(drawn, drawn[1:], drawn[2:]):
        if t2 - t0 < 0.05:
            d.append(np.linalg.norm(a - 2 * b + c))
    return float(np.sqrt(np.mean(np.square(d)))) * 1e3 if d else float('nan')


def stats(name, v, unit='mm'):
    if not len(v):
        return '%-14s -' % name
    v = np.array(v) * 1e3
    return '%-14s median %.1f %s, 95%% %.1f %s, max %.1f %s (%d)' % (
        name, np.median(v), unit, np.percentile(v, 95), unit, v.max(), unit, len(v))


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    ap.add_argument('track', nargs='?', default=str(DEFAULT))
    ap.add_argument('--set', action='append', default=[], metavar='NAME=VALUE',
                    help="change one of fusion.py's numbers, e.g. ACCEL_NOISE=2.0")
    ap.add_argument('--out', help='write the replayed answers here (time, x, y, z)')
    a = ap.parse_args()
    for kv in a.set:
        k, _, v = kv.partition('=')
        if not hasattr(fusion, k):
            sys.exit('fusion.py has no %s' % k)
        setattr(fusion, k, float(v))
    rows = read(a.track)
    old = not any(r[0] in 'AFN' for r in rows)
    drawn, surprise, match, truth, out = replay(events(rows))
    print('%s: %d rows%s' % (a.track, len(rows), ' (older recording: sightings only, no accelerations)' if old else ''))
    if match:
        print(stats('replay match', match))
    print(stats('surprise', surprise))
    print(stats('drawn error', drawn_error(drawn, truth)))
    print('%-14s %.2f mm' % ('jitter', jitter(drawn)))
    if a.out:
        with open(a.out, 'w') as f:
            for t, c in out:
                f.write('%.5f,%.5f,%.5f,%.5f\n' % (t, *c))


if __name__ == '__main__':
    main()
