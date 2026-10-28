"""A printable A4 PDF of the headset marker (ArUco DICT_4X4_50 id 10) in three sizes, drawn
as vector squares so the sizes are exact, with a 100 mm line to check the print scale."""
import sys

import cv2
import numpy as np

MARKER_ID = 10
SIZES_MM = (30, 40, 50)
PT = 72 / 25.4                  # points per mm
W, H = 210, 297                 # A4, mm


def bits(mid):
    d = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_50)
    return cv2.aruco.generateImageMarker(d, mid, 6) > 127          # 6x6 cells, True = white


def page():
    ops = ['0 g']
    def rect(x, y, w, h):       # mm, from the top-left
        ops.append('%.3f %.3f %.3f %.3f re f' % (x * PT, (H - y - h) * PT, w * PT, h * PT))
    def text(x, y, size, s):
        s = s.replace('\\', '\\\\').replace('(', '\\(').replace(')', '\\)')
        ops.append('BT /F1 %d Tf %.2f %.2f Td (%s) Tj ET' % (size, x * PT, (H - y) * PT, s))
    text(20, 22, 18, 'Phone Screen - headset marker')
    text(20, 31, 11, 'Print at ACTUAL SIZE / 100% (not "fit to page"). Check: the line below must be exactly 10 cm.')
    rect(20, 38, 100, 0.6)
    rect(20, 36, 0.6, 4.6)
    rect(119.4, 36, 0.6, 4.6)
    text(124, 40, 10, '<- 100 mm')
    text(20, 52, 11, 'Cut out ONE size, keeping the white border. Stick it flat on the front of the headset,')
    text(20, 58, 11, 'level and facing forward. 40 mm suits most; use the biggest that fits flat.')
    B = bits(MARKER_ID)
    y = 72
    for s in SIZES_MM:
        cell = s / 6
        x0 = (W - s) / 2
        for r in range(6):
            for c in range(6):
                if not B[r, c]:
                    rect(x0 + c * cell - 0.02, y + r * cell - 0.02, cell + 0.04, cell + 0.04)
        # a thin cutting guide, a cell outside the marker
        g = cell
        for (gx, gy, gw, gh) in ((x0 - g, y - g, s + 2 * g, 0.2), (x0 - g, y + s + g, s + 2 * g, 0.2),
                                 (x0 - g, y - g, 0.2, s + 2 * g), (x0 + s + g, y - g, 0.2, s + 2 * g + 0.2)):
            ops.append('0.6 g')
            rect(gx, gy, gw, gh)
            ops.append('0 g')
        text(x0 + s + g + 4, y + s / 2 + 1.5, 11, '%d mm' % s)
        y += s + 2 * g + 14
    text(20, 285, 9, 'ArUco 4x4 id %d. The top of the marker is the top of the headset.' % MARKER_ID)
    return '\n'.join(ops).encode()


def pdf(content):
    objs = [b'<< /Type /Catalog /Pages 2 0 R >>',
            b'<< /Type /Pages /Kids [3 0 R] /Count 1 >>',
            b'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 %.2f %.2f] /Contents 4 0 R '
            b'/Resources << /Font << /F1 5 0 R >> >> >>' % (W * PT, H * PT),
            b'<< /Length %d >>\nstream\n' % len(content) + content + b'\nendstream',
            b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>']
    out = bytearray(b'%PDF-1.4\n')
    offs = []
    for i, o in enumerate(objs, 1):
        offs.append(len(out))
        out += b'%d 0 obj\n' % i + o + b'\nendobj\n'
    xref = len(out)
    out += b'xref\n0 %d\n0000000000 65535 f \n' % (len(objs) + 1)
    for o in offs:
        out += b'%010d 00000 n \n' % o
    out += b'trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n' % (len(objs) + 1, xref)
    return bytes(out)


if __name__ == '__main__':
    path = sys.argv[1] if len(sys.argv) > 1 else 'headset-marker.pdf'
    open(path, 'wb').write(pdf(page()))
    print(path)
