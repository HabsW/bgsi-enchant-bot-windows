"""Windows diagnostics that never click: window probe, mouse-accuracy test, doctor."""
import importlib
import platform
import subprocess
import sys
import time

from . import ocr


def _plat():
    from . import win
    return win


def probe(cfg, log=print):
    win = _plat()
    v = win.virtual_screen()
    log(f"Virtual desktop: {v.w}x{v.h} at ({v.x},{v.y}); DPI awareness: {win.DPI_MODE}")
    w = win.find_game_window(cfg["window_match"])
    if w is None:
        log("Roblox window: NOT FOUND (start Roblox; match list: "
            f"{', '.join(cfg['window_match'])})")
        return None
    r = w.rect
    log(f"Roblox window: '{w.title}' [{w.exe or '?'}] client area {r.w}x{r.h} at ({r.x},{r.y}), "
        f"minimised={w.minimized}, in front={win.is_foreground(w.hwnd)}")
    return w


def mouse_test(cfg, log=print) -> bool:
    """Moves the pointer to 5 points inside the Roblox window (no clicks), reads
    the real cursor position back and reports the error."""
    win = _plat()
    w = win.find_game_window(cfg["window_match"])
    if w is None:
        log("Roblox window not found - start the game first.")
        return False
    r = win.client_rect(w.hwnd) or w.rect
    start = win.cursor_pos()
    m = win.Mouse()
    pts = [(r.x + 40, r.y + 40), (r.x + r.w - 40, r.y + 40), (r.x + r.w - 40, r.y + r.h - 40),
           (r.x + 40, r.y + r.h - 40), r.center]
    worst = 0
    try:
        for x, y in pts:
            m.move(x, y)
            time.sleep(0.15)
            cx, cy = win.cursor_pos()
            err = max(abs(cx - x), abs(cy - y))
            worst = max(worst, err)
            log(f"  target ({x:5d},{y:5d}) -> cursor ({cx:5d},{cy:5d})  error {err}px")
    finally:
        m.move(*start)
    ok = worst <= 1
    log(f"Mouse test {'OK' if ok else 'FAILED'} (worst error {worst}px). No clicks were made.")
    return ok


def doctor(cfg, log=print) -> bool:
    """Checks everything the bot needs and says exactly what is missing."""
    ok = True

    def bad(msg):
        nonlocal ok
        ok = False
        log("  FAIL  " + msg)

    log(f"System: {platform.platform()} | Python {sys.version.split()[0]} "
        f"({platform.architecture()[0]})")
    if sys.platform != "win32":
        bad("this is the Windows version")
        return False
    for mod, needed in (("numpy", True), ("cv2", True), ("PySide6", True), ("mss", False)):
        try:
            m = importlib.import_module(mod)
            log(f"  ok    {mod} {getattr(m, '__version__', '')}")
        except ImportError:
            if needed:
                bad(f"{mod} missing - run setup.bat")
            else:
                log(f"  warn  {mod} missing - using the slower built-in GDI capture")
    exe = ocr.set_tesseract_path(cfg.get("tesseract_path", ""))
    if not exe:
        bad("Tesseract OCR not found. Install: winget install -e --id UB-Mannheim.TesseractOCR "
            "(or set \"tesseract_path\" in config.json)")
    else:
        try:
            out = subprocess.run([exe, "--list-langs"], capture_output=True, text=True, timeout=10,
                                 creationflags=0x08000000).stdout
            if "eng" in out.split():
                log(f"  ok    tesseract: {exe}")
            else:
                bad(f"tesseract at {exe} has no English data (eng) - reinstall with English")
        except OSError as exc:
            bad(f"tesseract at {exe} does not run: {exc}")
    win = _plat()
    log(f"  ok    DPI awareness: {win.DPI_MODE}" if win.DPI_MODE == "per-monitor"
        else f"  warn  DPI awareness: {win.DPI_MODE} (coordinates may be off at display scaling != 100%)")
    w = probe(cfg, lambda m: log("        " + m))
    if w is None:
        bad("Roblox window not found")
        return ok
    if w.minimized:
        bad("Roblox is minimised - restore it and run doctor again")
        return ok
    cap = win.Capture()
    try:
        r = win.client_rect(w.hwnd)
        t0 = time.perf_counter()
        frame = cap.grab(r)
        dt = (time.perf_counter() - t0) * 1000
        if int(frame.max()) < 8:
            bad("captured image is black (exclusive fullscreen / HDR / window hidden?)")
        else:
            log(f"  ok    capture ({cap.backend}): {frame.shape[1]}x{frame.shape[0]} in {dt:.0f} ms")
        from .config import TEMPLATE_DIR, known_names
        from .vision import Vision
        v = Vision(TEMPLATE_DIR, known_names(cfg))
        cal = v.calibrate(frame)
        if cal:
            log(f"  ok    enchant UI found: {cal[1].replace('_title', '')} screen, UI scale {cal[0]:.2f}")
        else:
            log("  info  enchant UI not visible right now (open the enchant book to test this part)")
        if r.w < 1000 or r.h < 650:
            log("  warn  the Roblox window is small - maximise it for reliable text reading")
    finally:
        cap.close()
    log("All checks passed - ready." if ok else "Fix the FAIL lines above, then run doctor again.")
    return ok
