"""Phone Screen's background service: it waits quietly for the phone app's Start tracking,
then runs the tracker (phone_screen.py) until tracking stops, and puts the headset back
to normal.

While waiting it keeps `adb reverse` set, so the phone app can reach it, and listens on
the app's port. When the app says hello (it's tracking), the service lets go of the port,
starts the camera feed (ft-camd) if it isn't running, and starts phone_screen.py, which
takes the port over (the app reconnects within a second). phone_screen.py closes itself
15 s after tracking stops; then the camera feed is stopped again if this started it.

Run by systemd: ~/.config/systemd/user/phone-screen.service (see install.sh).
"""
import logging
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import video

HERE = Path(__file__).resolve().parent
PORT = 8778
CAMD = Path(os.environ.get('FRAMETOP', Path.home() / 'frametop')) / 'hands/build/ft-camd'
LOG = Path.home() / '.local/state/phone-screen/daemon.log'
log = logging.getLogger('phone-screen-daemon')


def say(text):
    try:
        subprocess.Popen(['clip-tts', 'say', text], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        pass


def camd_running():
    return subprocess.run(['pgrep', '-x', 'ft-camd'], capture_output=True).returncode == 0


def steamvr_running():
    return subprocess.run(['pgrep', '-x', 'vrcompositor'], capture_output=True).returncode == 0


def wait_for_start():
    """Block until the phone app connects and says it's tracking."""
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(('127.0.0.1', PORT))
    srv.listen(1)
    srv.settimeout(10)
    conn, buf, last_reverse = None, b'', 0.0
    try:
        while True:
            if time.monotonic() - last_reverse > 10:    # lost whenever the phone's Wi-Fi debugging reconnects
                video.adb('reverse', 'tcp:%d' % PORT, 'tcp:%d' % PORT)
                last_reverse = time.monotonic()
            if conn is None:
                try:
                    conn, _ = srv.accept()
                    conn.settimeout(2)
                    buf = b''
                except socket.timeout:
                    continue
            try:
                data = conn.recv(65536)
            except socket.timeout:
                continue
            except OSError:
                data = b''
            if not data:
                conn.close()
                conn = None
                continue
            buf += data
            if any(line.startswith(b'hello') for line in buf.split(b'\n')):
                return
            buf = buf[-4096:]
    finally:
        if conn:
            conn.close()
        srv.close()


def run_tracking():
    started_camd = None
    if not camd_running():
        started_camd = subprocess.Popen([str(CAMD), '--status', '300'], stdout=subprocess.DEVNULL,
                                        stderr=subprocess.DEVNULL)
        log.info('camera feed started')
        time.sleep(2)
    say('Phone tracking on.')
    env = dict(os.environ, PHONE_EXIT_IDLE='15')
    code = subprocess.call([sys.executable, '-u', str(HERE / 'phone_screen.py')], cwd=HERE, env=env)
    log.info('tracker ended (%d)', code)
    if started_camd:
        started_camd.terminate()
        try:
            started_camd.wait(5)
        except subprocess.TimeoutExpired:
            started_camd.kill()
        log.info('camera feed stopped')
    say('Phone tracking off.')


def main():
    LOG.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(message)s',
                        handlers=[logging.StreamHandler(), logging.FileHandler(LOG)])
    os.environ.setdefault('XDG_RUNTIME_DIR', '/run/user/%d' % os.getuid())
    log.info('waiting for the phone app')
    while True:
        try:
            wait_for_start()
        except OSError as e:            # the port is busy: a tracker started by hand is running
            log.info('waiting: %s', e)
            time.sleep(10)
            continue
        if not steamvr_running():
            log.info('the phone wants tracking, but SteamVR is not running')
            time.sleep(5)
            continue
        log.info('the phone app started tracking')
        run_tracking()
        log.info('waiting for the phone app')


if __name__ == '__main__':
    main()
