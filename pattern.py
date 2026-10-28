"""The tracking pattern the real phone shows, and the little web server that serves it.

The phone opens http://localhost:8777/ in Chrome (adb reverse brings it here). The page
goes full screen, keeps the screen awake, and draws two big ArUco markers on white. In
VR you see the copy of the phone's apps instead, so the real screen is free for this.
The page also reports its touches, and the server can change how bright it draws.
"""
import asyncio
import base64
import json
import threading

import cv2
import numpy as np
import websockets
from websockets.http11 import Response
from websockets.datastructures import Headers

PORT = 8777

# The Pixel 9 Pro XL's screen, as the phone itself reports it (dumpsys display: 1008 x 2244
# pixels at 365.76 x 365.369 per inch): 70.00 x 156.00 mm. The body is 76.6 x 162.8 x 8.5 mm.
SCREEN_MM = (1008 / 365.76 * 25.4, 2244 / 365.369 * 25.4)
BODY_MM = (76.6, 162.8, 8.5)
MARKER_MM = 56.0
MARKER_CY_MM = (36.0, -36.0)            # centres above / below the screen's centre (y up)
MARKER_IDS = (0, 1)
DICT = cv2.aruco.DICT_4X4_50


def marker_corners():
    """{id: 4x3 corners in the phone's frame, metres}: x right, y up, z out of the screen,
    in ArUco's order (top-left, top-right, bottom-right, bottom-left)."""
    s = MARKER_MM / 2000
    out = {}
    for mid, cy in zip(MARKER_IDS, MARKER_CY_MM):
        c = cy / 1000
        out[mid] = np.array([[-s, c + s, 0], [s, c + s, 0], [s, c - s, 0], [-s, c - s, 0]])
    return out


def _marker_png(mid):
    d = cv2.aruco.getPredefinedDictionary(DICT)
    img = cv2.aruco.generateImageMarker(d, mid, 6)          # 4x4 bits + border = 6 cells
    ok, png = cv2.imencode('.png', img)
    return 'data:image/png;base64,' + base64.b64encode(png.tobytes()).decode()


PAGE = '''<!doctype html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>Phone tracking</title>
<style>
html, body { margin: 0; height: 100%%; background: #fff; overflow: hidden; touch-action: none; }
canvas { position: fixed; inset: 0; width: 100vw; height: 100vh; }
#start { position: fixed; inset: 0; display: flex; align-items: center; justify-content: center;
         font: 600 28px sans-serif; background: #222; color: #fff; text-align: center; }
</style></head><body>
<canvas id="c"></canvas>
<div id="start">Tap to start<br>phone tracking</div>
<script>
const MARKERS = %(markers)s;          // [{src, cy_mm}]
const SCREEN_MM = %(screen)s, SIZE_MM = %(size)s;
const imgs = MARKERS.map(m => { const i = new Image(); i.src = m.src; return i; });
const c = document.getElementById('c'), g = c.getContext('2d');
let level = 255, ws = null, lock = null;

function draw() {
  const dpr = window.devicePixelRatio || 1;
  c.width = Math.round(innerWidth * dpr); c.height = Math.round(innerHeight * dpr);
  const pxmm = screen.width * dpr / SCREEN_MM[0];         // device pixels per mm
  g.fillStyle = '#fff';
  g.fillRect(0, 0, c.width, c.height);
  g.imageSmoothingEnabled = false;
  g.globalAlpha = 1;
  const s = SIZE_MM * pxmm, cx = c.width / 2, cy = c.height / 2;
  MARKERS.forEach((m, k) => {
    if (!imgs[k].complete) return;
    g.drawImage(imgs[k], cx - s / 2, cy - m.cy_mm * pxmm - s / 2, s, s);
  });
  g.fillStyle = `rgba(0,0,0,${1 - level / 255})`;       // dim it all: the cameras' view blooms
  g.fillRect(0, 0, c.width, c.height);
}

function connect() {
  ws = new WebSocket('ws://' + location.host + '/ws');
  ws.onopen = () => ws.send(JSON.stringify({hello: true, w: innerWidth, h: innerHeight,
                                             sw: screen.width, sh: screen.height, dpr: devicePixelRatio}));
  ws.onmessage = e => {
    const m = JSON.parse(e.data);
    if ('level' in m) { level = m.level; draw(); }
    if (m.reload) location.reload();
  };
  ws.onclose = () => setTimeout(connect, 1000);
}

function touch(e, action) {
  e.preventDefault();
  if (!started) { if (action === 'up') start(); return; }   // the first tap starts it
  if (!ws || ws.readyState !== 1) return;
  const pts = [...e.changedTouches].map(t => ({id: t.identifier, x: t.clientX / innerWidth, y: t.clientY / innerHeight,
                                               p: t.force || 1}));
  ws.send(JSON.stringify({touch: action, pts}));
}

let started = false;
async function start() {
  if (started) return;
  started = true;
  document.getElementById('start').remove();
  try { await document.documentElement.requestFullscreen({navigationUI: 'hide'}); } catch (e) {}
  try { await screen.orientation.lock('portrait'); } catch (e) {}   // the markers' layout is portrait
  try { lock = await navigator.wakeLock.request('screen'); } catch (e) {}
  draw();
}
document.addEventListener('visibilitychange', async () => {
  if (document.visibilityState === 'visible') { try { lock = await navigator.wakeLock.request('screen'); } catch (e) {} }
});
addEventListener('resize', draw);
imgs.forEach(i => i.onload = draw);
for (const [ev, a] of [['touchstart', 'down'], ['touchmove', 'move'], ['touchend', 'up'], ['touchcancel', 'up']])
  addEventListener(ev, e => touch(e, a), {passive: false});
connect();
draw();
</script></body></html>
'''


class PatternServer:
    def __init__(self, log=print, on_touch=None):
        self.log, self.on_touch = log, on_touch
        self.page = (PAGE % dict(
            markers=json.dumps([dict(src=_marker_png(m), cy_mm=cy) for m, cy in zip(MARKER_IDS, MARKER_CY_MM)]),
            screen=json.dumps(SCREEN_MM), size=MARKER_MM)).encode()
        self.clients = set()
        self.loop = None
        self.info = None
        threading.Thread(target=lambda: asyncio.run(self._main()), daemon=True).start()

    def _http(self, conn, request):
        if request.path == '/ws':
            return None
        return Response(200, 'OK', Headers({'Content-Type': 'text/html; charset=utf-8',
                                             'Cache-Control': 'no-store'}), self.page)

    async def _client(self, ws):
        self.clients.add(ws)
        try:
            async for msg in ws:
                m = json.loads(msg)
                if m.get('hello'):
                    self.info = m
                    self.log('pattern page connected: %s' % m)
                elif 'touch' in m and self.on_touch:
                    self.on_touch(m['touch'], m['pts'])
        except websockets.ConnectionClosed:
            pass
        finally:
            self.clients.discard(ws)

    async def _main(self):
        self.loop = asyncio.get_running_loop()
        async with websockets.serve(self._client, '127.0.0.1', PORT, process_request=self._http):
            await asyncio.Future()

    def set_level(self, level):
        """Background grey, 0-255 (lower it when the cameras see the screen blooming)."""
        if self.loop is None:
            return
        msg = json.dumps({'level': int(level)})
        for ws in list(self.clients):
            asyncio.run_coroutine_threadsafe(ws.send(msg), self.loop)

    @property
    def connected(self):
        return bool(self.clients)
