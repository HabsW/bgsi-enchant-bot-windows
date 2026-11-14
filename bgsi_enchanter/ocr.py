"""Tesseract wrapper + preprocessing tuned for Roblox's outlined cartoon font,
and fuzzy matching of OCR output against known enchant names."""
import difflib
import os
import re
import shutil
import subprocess
import sys

import cv2
import numpy as np

_LETTERS = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
_ENV = dict(os.environ, OMP_THREAD_LIMIT="1")
# no console window flashing up for every OCR call on Windows
_FLAGS = 0x08000000 if sys.platform == "win32" else 0          # CREATE_NO_WINDOW
_exe = None


def _candidates():
    yield shutil.which("tesseract")
    if sys.platform == "win32":
        for base in (os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)"),
                     os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs"),
                     os.environ.get("LOCALAPPDATA")):
            if base:
                yield os.path.join(base, "Tesseract-OCR", "tesseract.exe")


def set_tesseract_path(path: str = ""):
    """Use `path` (config "tesseract_path") or auto-detect tesseract(.exe)."""
    global _exe
    _exe = None
    for c in ([path] if path else []) + list(_candidates()):
        if c and os.path.isfile(c):
            _exe = c
            return _exe
    return None


def tesseract_available() -> bool:
    return bool(_exe or set_tesseract_path())


def tesseract(img: np.ndarray, psm: int = 7, letters_only: bool = True) -> str:
    if not tesseract_available():
        return ""
    ok, buf = cv2.imencode(".png", img)
    if not ok:
        return ""
    args = [_exe, "stdin", "stdout", "--psm", str(psm), "-l", "eng"]
    if letters_only:
        args += ["-c", f"tessedit_char_whitelist={_LETTERS}"]
    try:
        r = subprocess.run(args, input=buf.tobytes(), capture_output=True, timeout=10, env=_ENV,
                           creationflags=_FLAGS)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return r.stdout.decode("utf-8", "ignore").strip().replace("\n", " ")


def _finish(mask: np.ndarray, zoom: int = 3) -> np.ndarray:
    """mask (255 = text) -> upscaled black-on-white image with a margin."""
    m = cv2.resize(mask, None, fx=zoom, fy=zoom, interpolation=cv2.INTER_CUBIC)
    out = np.where(m > 127, 0, 255).astype(np.uint8)
    return cv2.copyMakeBorder(out, 20, 20, 20, 20, cv2.BORDER_CONSTANT, value=255)


def text_mask_on_card(crop: np.ndarray, vmin=170, dmin=70, erode=False) -> np.ndarray:
    """Coloured/white text with a dark outline on a flat background: keep the
    bright fill, drop the outline and the (most common) background colour."""
    c = crop.astype(np.int32)
    q = (c // 16).reshape(-1, 3)
    mode = int(np.bincount((q[:, 0] << 8) | (q[:, 1] << 4) | q[:, 2], minlength=4096).argmax())
    bg = np.array([mode >> 8, (mode >> 4) & 15, mode & 15]) * 16 + 8
    bright = c.max(axis=2)
    dist = np.sqrt(((c - bg) ** 2).sum(axis=2))
    mask = ((bright > vmin) & (dist > dmin)).astype(np.uint8) * 255
    if erode:
        mask = cv2.erode(mask, np.ones((2, 2), np.uint8))
    return mask


def prep_text_on_card(crop: np.ndarray, vmin=170, dmin=70, erode=False) -> np.ndarray:
    return _finish(text_mask_on_card(crop, vmin, dmin, erode))


def saturated_mask(crop: np.ndarray, scale: float = 1.0, smin=80, vmin=130) -> np.ndarray:
    """Keep only strongly coloured pixels (the enchant name inside a white-text dialog).
    The anti-aliased edges of the white text are faintly coloured too; they are thin,
    so an opening plus a component-size filter removes them."""
    hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
    mask = ((hsv[..., 1] > smin) & (hsv[..., 2] > vmin)).astype(np.uint8) * 255
    if scale >= 0.6:
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((2, 2), np.uint8))
    n, lab, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
    keep = np.zeros(n, bool)
    h_img = mask.shape[0]
    for i in range(1, n):
        x, y, w, h, area = stats[i]
        if area >= 6 * scale * scale and h < 0.7 * h_img and w > 1:
            keep[i] = True
    return np.where(keep[lab], 255, 0).astype(np.uint8)


def norm(s: str) -> str:
    s = s.lower()
    s = re.sub(r"[l|1!]", "i", s)      # Roblox font: l / I / 1 look alike
    return re.sub(r"[^a-z]", "", s)


_ROMAN = re.compile(r"\s(I{1,3}|IV|V)$")


def has_numeral(name: str) -> bool:
    return bool(name and _ROMAN.search(name))


def family(name: str) -> str:
    """'Bubbler III' -> 'Bubbler'."""
    return _ROMAN.sub("", name or "").strip()


def vote(readings):
    """readings: list of (raw, name, score). Majority vote on the name; ties go to
    the higher total score, then to the longer name (OCR tends to drop 'I's)."""
    tally = {}
    for raw, name, score in readings:
        if name:
            n, tot = tally.get(name, (0, 0.0))
            tally[name] = (n + 1, tot + score)
    if not tally:
        return readings[0] if readings else ("", None, 0.0)
    best = max(tally, key=lambda k: (tally[k][0], tally[k][1], len(k)))
    raw = next(r for r, n, _ in readings if n == best)
    return raw, best, tally[best][1] / tally[best][0]


def match_name(text: str, names, threshold: float = 0.78):
    """Return (best_name or None, score)."""
    t = norm(text)
    if len(t) < 3:
        return None, 0.0
    best_name, best = None, 0.0
    for n in names:
        k = norm(n)
        r = difflib.SequenceMatcher(None, t, k).ratio()
        if len(k) >= 6 and k in t:          # name plus some junk characters
            r = max(r, 0.93 - 0.01 * max(0, len(t) - len(k) - 3))
        if r > best:
            best_name, best = n, r
    return (best_name, best) if best >= threshold else (None, best)
