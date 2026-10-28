"""The phone's screen as a stream of RGBA frames, and its sound, through adb and scrcpy's server.

The sound is the phone's whole output (raw 48 kHz stereo); while it's sent, the phone's
own speaker is quiet. It plays here through PipeWire, so it comes out of the headset."""
import os
import random
import socket
import struct
import subprocess
import threading
import time

import av

HERE = os.path.dirname(os.path.abspath(__file__))
SERVER = os.path.join(HERE, 'scrcpy-server-v4.1')
SERVER_VERSION = '4.1'
# The phone's adb serial; unset, adb uses the one device it's connected to
PHONE = os.environ.get('PHONE_SERIAL', '')
PORT = 27183


def adb_cmd(*args):
    """adb runs in the 'dev' distrobox (SteamOS has none of its own)."""
    return ['distrobox', 'enter', 'dev', '--', 'adb'] + (['-s', PHONE] if PHONE else []) + list(args)


def adb(*args, **kw):
    env = dict(os.environ, XDG_RUNTIME_DIR='/run/user/%d' % os.getuid())
    return subprocess.run(adb_cmd(*args),
                          env=env, capture_output=True, text=True, **kw)


def adb_bytes(*args):
    env = dict(os.environ, XDG_RUNTIME_DIR='/run/user/%d' % os.getuid())
    return subprocess.run(adb_cmd(*args),
                          env=env, capture_output=True).stdout


# Touch actions (Android MotionEvent)
DOWN, UP, MOVE = 0, 1, 2


class PhoneVideo:
    """With new_display ("WxH/dpi"), apps run on a virtual display that only VR shows, and
    the real screen stays free (for the tracking pattern); touches are sent to it."""

    def __init__(self, max_size=1280, fps=30, bitrate=8_000_000, log=print, new_display=None, audio=False):
        self.max_size, self.fps, self.bitrate, self.log = max_size, fps, bitrate, log
        self.audio = audio
        self.new_display = new_display
        self.control = None
        self.control_lock = threading.Lock()
        self.frame = None           # (rgba bytes, width, height)
        self.frame_no = 0
        self.stop = False
        self.proc = None
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        while not self.stop:
            try:
                self._session()
            except Exception as e:      # phone gone, screen off, Wi-Fi dropped...
                self.log('video: %s' % e)
            if self.proc:
                self.proc.kill()
                self.proc = None
            time.sleep(3)

    def _session(self):
        # a server left over from an earlier run (a crash, a restart) keeps capturing the
        # phone's sound, and then the sound stays on the phone: only ever run one
        adb('shell', 'pkill -f com.genymobile.scrcpy.Server')
        r = adb('push', SERVER, '/data/local/tmp/scrcpy-server.jar')
        if r.returncode:
            raise RuntimeError('push failed: ' + (r.stderr or r.stdout).strip())
        scid = '%08x' % random.getrandbits(31)
        adb('forward', '--remove', 'tcp:%d' % PORT)
        r = adb('forward', 'tcp:%d' % PORT, 'localabstract:scrcpy_' + scid)
        if r.returncode:
            raise RuntimeError('forward failed: ' + r.stderr.strip())
        env = dict(os.environ, XDG_RUNTIME_DIR='/run/user/%d' % os.getuid())
        self.proc = subprocess.Popen(
            adb_cmd('shell',
             'CLASSPATH=/data/local/tmp/scrcpy-server.jar', 'app_process', '/',
             'com.genymobile.scrcpy.Server', SERVER_VERSION, 'scid=' + scid, 'log_level=info',
             'tunnel_forward=true', 'raw_stream=true', 'send_dummy_byte=true') + (
                ['audio=true', 'audio_codec=raw', 'audio_source=output'] if self.audio else ['audio=false']) + [
             'video_codec=h264', 'max_size=%d' % self.max_size, 'max_fps=%d' % self.fps,
             'video_bit_rate=%d' % self.bitrate] + (
                ['control=true', 'new_display=' + self.new_display, 'display_ime_policy=local',
                 'vd_system_decorations=true'] if self.new_display else ['control=false']),
            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        # adb's forward accepts at once even before the server listens; then it closes.
        # Retry until the first byte (the server's dummy byte) arrives.
        sock, first = None, b''
        for _ in range(50):
            time.sleep(0.2)
            if self.proc.poll() is not None:
                raise RuntimeError('server exited')
            try:
                sock = socket.create_connection(('127.0.0.1', PORT), timeout=5)
                first = sock.recv(1)
            except OSError:
                first = b''
            if first:
                break
            if sock:
                sock.close()
                sock = None
        if not first:
            raise RuntimeError('no video socket')
        if self.audio:      # the sockets come in order: video, audio, control
            asock = socket.create_connection(('127.0.0.1', PORT), timeout=5)
            threading.Thread(target=self._play, args=(asock,), daemon=True).start()
        if self.new_display:
            self.control = socket.create_connection(('127.0.0.1', PORT), timeout=5)
            self.control.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self.log('video: connected')
        sock.settimeout(None)
        codec = av.CodecContext.create('h264', 'r')
        codec.thread_type = 'NONE'          # lowest latency
        data = None
        while not self.stop:
            if data is None:
                data = sock.recv(1 << 16)
            if not data:
                raise RuntimeError('stream ended')
            for packet in codec.parse(data):
                for f in codec.decode(packet):
                    img = f.to_ndarray(format='rgba')
                    self.frame = (img, f.width, f.height)
                    self.frame_no += 1
            data = None


    def _play(self, sock):
        sock.settimeout(None)
        player = subprocess.Popen(['pw-cat', '--playback', '--raw', '--format', 's16', '--rate', '48000', '--channels', '2',
                                   '--latency', '30ms', '--media-role', 'Game', '-'],
                                  stdin=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                  env=dict(os.environ, XDG_RUNTIME_DIR='/run/user/%d' % os.getuid()))
        self.log('audio: playing')
        try:
            while True:
                data = sock.recv(1 << 14)
                if not data:
                    break
                player.stdin.write(data)
                player.stdin.flush()
        except (OSError, BrokenPipeError) as e:
            self.log('audio: %s' % e)
        finally:
            player.kill()
            sock.close()
            self.log('audio: stopped')

    def touch(self, action, pointer, nx, ny, pressure=1.0):
        """A touch at (nx, ny), 0-1 across the shown picture."""
        f, c = self.frame, self.control
        if f is None or c is None:
            return
        w, h = f[1], f[2]
        x = int(min(max(nx, 0.0), 1.0) * (w - 1))
        y = int(min(max(ny, 0.0), 1.0) * (h - 1))
        p = 0 if action == UP else min(0xffff, int(pressure * 0x10000))
        msg = struct.pack('>BBqiiHHHII', 2, action, pointer, x, y, w, h, p, 0, 0)
        with self.control_lock:
            try:
                c.sendall(msg)
            except OSError as e:
                self.log('control: %s' % e)


    def _send(self, msg):
        c = self.control
        if c is None:
            return
        with self.control_lock:
            try:
                c.sendall(msg)
            except OSError as e:
                self.log('control: %s' % e)

    def key(self, keycode):
        """Press and release an Android key (4 = BACK) on the apps' display."""
        for action in (0, 1):
            self._send(struct.pack('>BBiii', 0, action, keycode, 0, 0))

    def start_app(self, package):
        """Open an app on the apps' display."""
        name = package.encode()
        self._send(struct.pack('>BB', 16, len(name)) + name)


if __name__ == '__main__':
    v = PhoneVideo()
    t0 = time.time()
    while time.time() - t0 < 15:
        time.sleep(1)
        print(v.frame_no, v.frame and v.frame[1:])
    if v.frame:
        import cv2
        cv2.imwrite('phone.png',
                    cv2.cvtColor(v.frame[0], cv2.COLOR_RGBA2BGR))
