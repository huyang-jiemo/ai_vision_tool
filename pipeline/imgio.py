# -*- coding: utf-8 -*-
"""图像读写辅助：绕开 OpenCV 在 Windows 上的 ANSI 路径限制。

cv2.imwrite / cv2.imread 在 Windows 上以 ANSI 代码页（GBK）fopen 处理路径，
包含中文的路径会写出行走样的文件名（UTF-8 字节被按 GBK 解释）或直接失败。
统一改走 imencode + ndarray.tofile（numpy 使用 Unicode 宽字符接口），全平台安全。
"""
import os


def imwrite_unicode(path, img, params=None):
    """把 OpenCV 图像写入 path（支持中文路径）。返回是否成功。"""
    import cv2
    import numpy as np
    ext = os.path.splitext(str(path))[1] or ".png"
    ok, buf = cv2.imencode(ext, img, params or [])
    if not ok:
        return False
    buf.tofile(str(path))
    return True


def imread_unicode(path, flags=None):
    """读取路径可能含中文的图像（PIL 不可用时的兜底）。失败返回 None。"""
    import cv2
    import numpy as np
    try:
        data = np.fromfile(str(path), dtype=np.uint8)
        if data.size == 0:
            return None
        return cv2.imdecode(data, cv2.IMREAD_COLOR if flags is None else flags)
    except Exception:
        return None
