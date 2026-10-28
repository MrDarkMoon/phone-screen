"""The link to Phone Screen's own phone app (android/): one connection for the tracking
pattern, the phone's motion sensors and its touches. It stands in for both
pattern.PatternServer and sensors.PhoneSensors.

The app connects to 127.0.0.1:8778 on the phone; adb reverse brings that here. Sensor
readings carry the phone's own clock (elapsedRealtimeNanos). Every half second we ping;
the reply with the shortest round trip says best where the phone's clock is against
ours, so readings are put onto our clock by their true measuring time.
"""
import base64
import collections
import socket
import threading
import time
from pathlib import Path

import sensors

PORT = 8778
SNAPS = Path.home() / '.local/state/phone-screen/snaps'    # the phone's selfie-camera pictures


class PhoneLink(sensors.PhoneSensors):
    synced = True               # sensor times come from a real clock match, not a guess

    def __init__(self, log=print, on_touch=None, on_accel=None):
        super().__init__(log=log, on_accel=on_accel, connect=False)
        self.on_touch = on_touch
        self.conn = None
        self.lock = threading.Lock()
        self.info = None
        self.tracking = False
        self.training = False       # the app's Training button (train.py)
        self.prox = None
        self.face = None            # facetrack.FaceTracker, fed the selfie pictures            # the proximity sensor's last reading, cm
        self.pings = collections.deque(maxlen=60)       # (round trip, offset) of the last ~30 s
        self.offset = None          # our time minus the phone's, seconds
        threading.Thread(target=self._serve, daemon=True).start()
        threading.Thread(target=self._ping, daemon=True).start()

    # ---- the pattern side (as pattern.PatternServer)

    @property
    def connected(self):
        return self.conn is not None

    def send(self, line):
        with self.lock:
            c = self.conn
            if c is None:
                return
            try:
                c.sendall((line + '\n').encode())
            except OSError:
                pass

    def snap(self, n=4):
        """Ask the app for n selfie-camera pictures (they land in SNAPS)."""
        self.send('snap %d' % n)

    def set_level(self, level):
        self.send('level %d' % int(level))

    # ---- the connection

    def _serve(self):
        srv = socket.socket()
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(('127.0.0.1', PORT))
        srv.listen(1)
        while True:
            c, _ = srv.accept()
            c.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            with self.lock:
                if self.conn is not None:
                    self.conn.close()
                self.conn = c
            self.pings.clear()
            self.offset = None
            self.log('phone app connected')
            try:
                self._read(c)
            except OSError:
                pass
            with self.lock:
                if self.conn is c:
                    self.conn = None
            self.tracking = self.training = False
            self.log('phone app disconnected')

    def _ping(self):
        while True:
            time.sleep(0.5)
            self.send('ping %d' % time.monotonic_ns())

    def _time(self, phone_ns):
        return int(phone_ns) / 1e9 + (self.offset or 0.0) - self.lag

    def _read(self, c):
        buf = b''
        while True:
            data = c.recv(65536)
            if not data:
                return
            buf += data
            *lines, buf = buf.split(b'\n')
            for line in lines:
                w = line.decode(errors='replace').split()
                if w:
                    self._line(w)

    def _line(self, w):
        kind = w[0]
        if kind == 'pong':
            sent, phone = int(w[1]), int(w[2])
            now = time.monotonic_ns()
            self.pings.append(((now - sent) / 1e9, (sent + now) / 2e9 - phone / 1e9))
            self.offset = min(self.pings)[1]
        elif kind == 'r' and self.offset is not None:     # until the first pong, readings can't be placed
            self.reading(sensors.ROTATION, self._time(w[1]), [float(v) for v in w[2:6]])
        elif kind == 'a' and self.offset is not None:
            self.reading(sensors.ACCEL, self._time(w[1]), [float(v) for v in w[2:5]])
        elif kind == 't':
            if self.on_touch:
                self.on_touch(w[1], [dict(id=int(w[2]), x=float(w[3]), y=float(w[4]), p=float(w[5]))])
        elif kind == 'p':
            self.prox = float(w[2])
        elif kind == 'hello':
            self.info = dict(w=int(w[1]), h=int(w[2]))
            self.tracking = True
            self.log('phone app tracking: screen %s x %s' % (w[1], w[2]))
        elif kind == 'caminfo' and self.face:
            self.face.caminfo(int(w[1]), int(w[2]), float(w[3]), float(w[4]), float(w[5]), float(w[6]), int(w[7]))
        elif kind == 'cam' and self.face and self.offset is not None:
            self.face.frame(self._time(w[1]), base64.b64decode(w[2]))
        elif kind == 'jpg':
            SNAPS.mkdir(parents=True, exist_ok=True)
            f = SNAPS / ('%s.jpg' % w[1])
            f.write_bytes(base64.b64decode(w[2]))
            self.log('phone camera picture: %s' % f)
        elif kind == 'snaperr':
            self.log('phone camera: %s' % ' '.join(w[1:]))
        elif kind == 'train':
            self.training = w[1] == '1'
            self.log('phone app training %s' % ('on' if self.training else 'off'))
        elif kind == 'stopped':
            self.tracking = self.training = False
            self.log('phone app stopped tracking')
