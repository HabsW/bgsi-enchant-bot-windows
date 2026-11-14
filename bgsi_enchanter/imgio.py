"""Image files with any path. On Windows cv2.imread / cv2.imwrite fail silently
on paths with non-ASCII characters (a user folder like C:\\Users\\Jürgen, or the
'⁄' in the reference screenshot names), so the bytes go through numpy instead."""
from pathlib import Path

import cv2
import numpy as np


def imread(path, flags=cv2.IMREAD_COLOR):
    """Like cv2.imread: the image, or None if it can't be read."""
    try:
        data = np.fromfile(str(path), dtype=np.uint8)
    except OSError:
        return None
    if data.size == 0:
        return None
    return cv2.imdecode(data, flags)


def imwrite(path, img) -> bool:
    """Like cv2.imwrite (format from the file extension)."""
    ok, buf = cv2.imencode(Path(path).suffix or ".png", img)
    if not ok:
        return False
    try:
        buf.tofile(str(path))
    except OSError:
        return False
    return True
