"""Overlay pictures through shared DMA-BUFs, like Frametop's keyboard (screens/keyboard.cpp).

Three linear GBM buffers per picture size, each imported into SteamVR once; a new picture
is copied into the next buffer and the overlay switches to it. SetOverlayRaw makes a new
texture on every call: it stopped working after a while here, and big uploads crashed
vrcompositor once.
"""
import ctypes
import os

import numpy as np
import openvr

GBM_FORMAT_ABGR8888 = 0x34324241        # 'AB24': R, G, B, A in memory
GBM_BO_USE_RENDERING = 1 << 2
GBM_BO_USE_LINEAR = 1 << 4
GBM_BO_TRANSFER_WRITE = 1 << 1
DRM_FORMAT_MOD_LINEAR = 0

gbm = ctypes.CDLL('libgbm.so.1')
gbm.gbm_create_device.restype = ctypes.c_void_p
gbm.gbm_create_device.argtypes = [ctypes.c_int]
gbm.gbm_bo_create.restype = ctypes.c_void_p
gbm.gbm_bo_create.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32]
gbm.gbm_bo_get_fd.restype = ctypes.c_int
gbm.gbm_bo_get_fd.argtypes = [ctypes.c_void_p]
gbm.gbm_bo_get_offset.restype = ctypes.c_uint32
gbm.gbm_bo_get_offset.argtypes = [ctypes.c_void_p, ctypes.c_int]
gbm.gbm_bo_get_stride.restype = ctypes.c_uint32
gbm.gbm_bo_get_stride.argtypes = [ctypes.c_void_p]
gbm.gbm_bo_map.restype = ctypes.c_void_p
gbm.gbm_bo_map.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32,
                           ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint32), ctypes.POINTER(ctypes.c_void_p)]
gbm.gbm_bo_unmap.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
gbm.gbm_bo_destroy.argtypes = [ctypes.c_void_p]


_ipc = None


def ipc():
    """SteamVR's IVRIPCResourceManagerClient_003 function table. pyopenvr asks for _002, which
    the Frame's SteamVR doesn't serve; ImportDmabuf is at the same place in both."""
    global _ipc
    if _ipc is None:
        err = openvr.EVRInitError()
        ptr = openvr._openvr.VR_GetGenericInterface(b'FnTable:IVRIPCResourceManagerClient_003', ctypes.byref(err))
        if not ptr or err.value:
            raise RuntimeError('no IVRIPCResourceManagerClient_003 (error %d)' % err.value)
        _ipc = ctypes.cast(ptr, ctypes.POINTER(openvr.IVRIPCResourceManagerClient_FnTable)).contents
    return _ipc


class Buffer:
    def __init__(self, dev, w, h):
        self.bo = gbm.gbm_bo_create(dev, w, h, GBM_FORMAT_ABGR8888, GBM_BO_USE_RENDERING | GBM_BO_USE_LINEAR)
        if not self.bo:
            raise RuntimeError('gbm_bo_create failed')
        fd = gbm.gbm_bo_get_fd(self.bo)
        if fd < 0:
            raise RuntimeError('gbm_bo_get_fd failed')
        a = openvr.DmabufAttributes_t()
        a.unWidth, a.unHeight = w, h
        a.unDepth = a.unMipLevels = a.unArrayLayers = a.unSampleCount = 1
        a.unFormat = GBM_FORMAT_ABGR8888
        a.ulModifier = DRM_FORMAT_MOD_LINEAR
        a.unPlaneCount = 1
        a.plane[0].unOffset = gbm.gbm_bo_get_offset(self.bo, 0)
        a.plane[0].unStride = gbm.gbm_bo_get_stride(self.bo)
        a.plane[0].nFd = fd
        self.handle = openvr.SharedTextureHandle_t()
        ok = ipc().importDmabuf(
            openvr.VRApplication_Overlay, ctypes.byref(a), ctypes.byref(self.handle))
        os.close(fd)                    # SteamVR dup'ed it
        if not ok or not self.handle.value:
            raise RuntimeError('SteamVR would not import the buffer')
        self.w, self.h = w, h

    def write(self, img):
        stride = ctypes.c_uint32()
        mapping = ctypes.c_void_p()
        ptr = gbm.gbm_bo_map(self.bo, 0, 0, self.w, self.h, GBM_BO_TRANSFER_WRITE,
                             ctypes.byref(stride), ctypes.byref(mapping))
        if not ptr:
            return False
        try:
            dst = np.ctypeslib.as_array((ctypes.c_uint8 * (stride.value * self.h)).from_address(ptr))
            dst = dst.reshape(self.h, stride.value)[:, :self.w * 4]
            dst[:] = img.reshape(self.h, self.w * 4)
        finally:
            gbm.gbm_bo_unmap(self.bo, mapping)
        return True


class SharedTexture:
    """Shows RGBA numpy pictures on one overlay."""

    def __init__(self, overlay, handle):
        self.ov, self.overlay = overlay, handle
        self.fd = os.open('/dev/dri/renderD128', os.O_RDWR | os.O_CLOEXEC)
        self.dev = gbm.gbm_create_device(self.fd)
        if not self.dev:
            raise RuntimeError('gbm_create_device failed')
        self.sets = {}                  # (w, h) -> [Buffer x3]
        self.next = 0

    def show(self, img):
        h, w = img.shape[:2]
        bufs = self.sets.get((w, h))
        if bufs is None:
            if len(self.sets) >= 4:     # the phone rotated a few times: keep the imports bounded
                raise RuntimeError('too many picture sizes')
            bufs = self.sets[(w, h)] = [Buffer(self.dev, w, h) for _ in range(3)]
        b = bufs[self.next % 3]
        self.next += 1
        if not b.write(np.ascontiguousarray(img)):
            return False
        tex = openvr.Texture_t()
        tex.handle = ctypes.addressof(b.handle)
        tex.eType = openvr.TextureType_SharedTextureHandle
        tex.eColorSpace = openvr.ColorSpace_Gamma
        self.ov.setOverlayTexture(self.overlay, tex)
        return True
