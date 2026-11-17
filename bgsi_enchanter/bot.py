"""The enchant bot (Windows 10/11): Select Pet -> pet -> lock wanted enchants ->
Shadow Crystal -> confirm (1/2) -> answer the reroll confirmation (2/2) safely ->
repeat.

Every click is made from a fresh screenshot, after the target was recognised.
The Roblox window is re-measured on EVERY screenshot: if it moves, clicks
follow; if it is resized / reshaped, the bot waits until the size settles,
re-measures the UI scale and carries on."""
import json
import re
import threading
import time
import traceback

import cv2
import numpy as np

from . import ocr
from .config import DEBUG_DIR, ROOT, SECRET_ENCHANTS, TEMPLATE_DIR, known_names
from .geom import Rect
from .imgio import imwrite
from .logic import LOCKED, UNLOCKED, Plan, decide_dialog, evaluate
from .vision import T_BUTTON_STILL, Vision

try:
    from . import win as plat
except ImportError:                    # not on Windows (offline tests / simulator)
    plat = None

DIALOGS = ("dlg_crystal", "dlg_reroll", "dimmed")
STATE_PATH = ROOT / ".state.json"      # remembered UI scale per window size
LEARNED_PATH = ROOT / ".learned.json"  # enchant names recognised in earlier runs


TEST_IMAGE = "test_detection.png"      # the one non-error image (Test detection button)
_ERROR_SHOT = re.compile(r"^\d{8}_\d{6}_.+\.png$")


def prune_debug(keep=10):
    """debug/ keeps only the `keep` newest error screenshots (+ the latest
    test_detection.png). Everything else the bot ever wrote there is deleted."""
    if not DEBUG_DIR.is_dir():
        return
    errors = []
    for p in DEBUG_DIR.iterdir():
        if not p.is_file() or p.name == TEST_IMAGE:
            continue
        if _ERROR_SHOT.match(p.name):
            errors.append(p)
        elif p.suffix.lower() == ".png":
            p.unlink(missing_ok=True)
    errors.sort(key=lambda p: (p.stat().st_mtime, p.name), reverse=True)
    for p in errors[max(0, keep):]:
        p.unlink(missing_ok=True)


class Stopped(Exception):
    pass


class BotError(Exception):
    pass


class FatalBotError(BotError):
    """A deliberate stop (limit reached, safety stop) - never retried."""


class Bot:
    def __init__(self, cfg, log=print, status=None, dry_run=False):
        self.cfg = cfg
        self._log = log
        self._status = status or (lambda d: None)
        self.dry_run = dry_run
        self.stop_evt = threading.Event()
        self.targets = list(cfg["targets"])
        self.vision = Vision(TEMPLATE_DIR, known_names(cfg))
        self.vision.exact_numerals = {ocr.family(t) for t in self.targets if ocr.has_numeral(t)}
        self.cap = plat.Capture() if plat else None
        self.mouse = self.hotkey = None
        self.hwnd = None
        self._logfile = None
        self.rect = None          # Roblox client area (screen px), refreshed every frame
        self.park = None
        ocr.set_tesseract_path(cfg.get("tesseract_path", ""))
        self._dim_since = None
        self._last_enchants = None
        self._last_click = (None, (0, 0), "")
        self._debug_saved = 0
        self._final = {}          # (screen, button) -> resting position learned by settle()
        self._pointer_frame = None   # where the pointer rests, if on a game spot (frame px)
        self._last_click_early = False
        self._early_ok = bool(cfg.get("early_press", True))
        self.settled_early = False
        # hover check per button key: seen hovered at least once / times it never lit up
        self._hover_seen, self._hover_misses = set(), {}
        self._dialog_shot_saved = False
        self._asks_about = set()      # enchants the 2/2 dialog has named in this run
        # resumable main-loop state (see main_loop / with_retries)
        self._pass_no, self._order, self._pos, self._cur = 1, None, 0, None
        self._pass_used_before = self._done_this_pass = 0
        self._pets, self._grid_title = [], None
        self._progress = 0        # bumps on every crystal used / pet finished
        self._renewed = set()     # grid positions re-enchanted in this run (reenchant_once)
        self._forcing = False     # the current crystal re-enchants: reroll even wanted enchants
        self.stats ={"crystals": 0, "pets_done": 0, "pets_seen": 0, "state": "idle"}

    # ------------------------------------------------------------ plumbing
    def log(self, msg):
        self._log(f"[{time.strftime('%H:%M:%S')}] {msg}")
        self.trace(msg)

    def trace(self, msg):
        """Run log with milliseconds (debug/last_run.log) - send it in if something hangs."""
        f = self._logfile
        if f is not None:
            t = time.time()
            try:
                f.write(f"{time.strftime('%H:%M:%S', time.localtime(t))}.{int(t * 1000) % 1000:03d} {msg}\n")
                f.flush()
            except (OSError, ValueError):
                pass

    def status(self, **kw):
        self.stats.update(kw)
        self._status(dict(self.stats))

    def stop(self):
        self.stop_evt.set()

    def sleep(self, t):
        if self.stop_evt.wait(t):
            raise Stopped()

    def save_debug(self, frame, screen=None, tag="debug", slots=None, pets=None, name=None):
        """Save an annotated error screenshot to debug/, then prune the folder so only
        the newest `keep_error_screenshots` (10) error screenshots remain."""
        if not self.cfg.get("save_debug_images", True) or frame is None:
            return None
        DEBUG_DIR.mkdir(exist_ok=True)
        path = DEBUG_DIR / (name or f"{time.strftime('%Y%m%d_%H%M%S')}_{tag}.png")
        try:
            img = self.vision.annotate(frame, screen, slots, pets) if self.vision.scale else frame
            imwrite(str(path), img)
        except Exception:
            imwrite(str(path), frame)
        prune_debug(int(self.cfg.get("keep_error_screenshots", 10)))
        return path

    # ------------------------------------------------------------ setup
    def connect(self):
        if plat is None:
            raise FatalBotError("This is the Windows version - run it on Windows 10/11.")
        win = plat.find_game_window(self.cfg["window_match"])
        if win is None:
            raise BotError("Roblox window not found - start the game first.")
        if win.minimized and self.dry_run:
            raise BotError("The Roblox window is minimised - restore it first.")
        if not self.dry_run and (win.minimized or not plat.is_foreground(win.hwnd)):
            plat.activate(win.hwnd)
            time.sleep(0.3)
        self.hwnd = win.hwnd
        r = plat.client_rect(self.hwnd)
        if r is None:
            raise BotError("The Roblox window is minimised or closed.")
        self.rect = r
        self.log(f"Roblox window ({win.exe or win.title}): {r.w}x{r.h} at ({r.x},{r.y}), "
                 f"DPI awareness: {plat.DPI_MODE}, capture: {self.cap.backend}")
        if r.w < 1000 or r.h < 650:
            self.log("WARNING: the game window is small - maximise it or go fullscreen (F11) "
                     "so the text is big enough to read reliably.")
        self.park = self._park_point()

    def _park_point(self):
        """Where the cursor rests between clicks: the window's title bar if it has
        one, else the very top edge of the game - away from the enchant UI so hover
        effects never disturb recognition."""
        r = self.rect
        wr = plat.window_rect(self.hwnd) if plat and self.hwnd else None
        if wr is not None and r.y - wr.y >= 16:
            return (r.x + r.w // 2, (wr.y + r.y) // 2)
        return (r.x + r.w // 2, r.y + 3)

    def _refocus(self):
        """Bring Roblox back to the front (e.g. after a popup of another app)."""
        if plat and self.hwnd and not self.dry_run and not plat.is_foreground(self.hwnd):
            plat.activate(self.hwnd)

    def calibrate(self, frame=None):
        if frame is None:
            frame = self.grab()
        key = f"{self.rect.w}x{self.rect.h}" if self.rect else None
        try:
            state = json.loads(STATE_PATH.read_text()) if key else {}
        except (OSError, ValueError):
            state = {}
        cached = state.get("scale", {}).get(key) if key else None
        if cached:                       # same window size as last time: verify, skip the search
            self.vision.scale = float(cached)
            scr = self.vision.detect(frame, full=True)
            best = max((m.score for m in scr.matches.values() if m), default=0)
            if scr.kind in ("select_pet", "enchants", "dlg_crystal", "dlg_reroll") and best >= 0.85:
                self.log(f"UI found: {scr.kind} screen, UI scale {self.vision.scale:.2f} (remembered)")
                return
            self.vision.scale = None
        cal = self.vision.calibrate(frame)
        if cal is None:
            path = self.save_debug(frame, tag="calibration_failed")
            raise BotError("Could not find the enchant UI. Open the enchant book (Select Pet or "
                           f"Enchants screen) and keep the game window visible. Screenshot: {path}")
        self.log(f"UI found: {cal[1].replace('_title', '')} screen, UI scale {cal[0]:.2f} "
                 f"(match {cal[2]:.2f})")
        if key:
            state.setdefault("scale", {})[key] = round(cal[0], 4)
            try:
                STATE_PATH.write_text(json.dumps(state))
            except OSError:
                pass

    # ------------------------------------------------------------ io
    def _client_rect(self):
        return plat.client_rect(self.hwnd)

    def _capture(self, rect):
        return self.cap.grab(rect)

    def grab(self):
        """Screenshot of the Roblox client area. The window is re-measured every
        time: a move just updates the click origin, a resize triggers a re-measure."""
        if self.stop_evt.is_set():
            raise Stopped()
        r = self._client_rect()
        if r is None:
            raise BotError("The Roblox window is minimised or was closed.")
        if self.rect is None or (r.w, r.h) != (self.rect.w, self.rect.h):
            r = self._window_resized(r)
        elif (r.x, r.y) != (self.rect.x, self.rect.y):
            self.rect = r                       # moved: frame coords are client-relative,
            self.park = self._park_point()      # so only the click origin changes
        frame = self._capture(r)
        if frame.size and int(frame.max()) < 8:
            raise BotError("The captured game window is completely black - turn off exclusive "
                           "fullscreen / HDR, or make sure Roblox is not hidden behind something.")
        return frame

    def _window_resized(self, r):
        """The client area changed size: wait until it stops changing (dragging,
        snapping animation), forget everything measured at the old size and
        re-measure the UI scale."""
        old = self.rect
        t0 = time.time()
        since = time.time()
        while time.time() - t0 < 3.0:
            self.sleep(0.05)
            r2 = self._client_rect()
            if r2 is None:
                raise BotError("The Roblox window is minimised or was closed.")
            if (r2.w, r2.h) != (r.w, r.h):
                r, since = r2, time.time()
            elif time.time() - since >= 0.3:
                r = r2
                break
        if old is not None:
            self.log(f"   (window changed from {old.w}x{old.h} to {r.w}x{r.h} - re-measuring the UI)")
        if self.vision.scale is not None:
            self.save_learned()
        self.rect = r
        self.park = self._park_point() if self.hwnd else (r.x + r.w // 2, r.y + 3)
        # everything measured in pixels of the old size is invalid now
        self.vision.scale = None
        self.vision._last.clear()
        self.vision._cache.clear()
        self.vision._learned = {"slot": [], "dialog": []}
        self._final.clear()
        self._pets, self._grid_title = [], None
        self._last_enchants = self._pointer_frame = None
        if old is not None:                  # first measurement happens in run()
            self.calibrate(self._capture(r))
            self.load_learned(quiet=True)
        return r

    def look(self):
        frame = self.grab()
        if self.vision.scale is None:          # e.g. re-measure after a resize failed once
            self.calibrate(frame)
        scr = self.vision.detect(frame)
        if scr.kind == "dimmed":
            self._dim_since = self._dim_since or time.time()
        else:
            self._dim_since = None
        return frame, scr

    def popup_needs_closing(self, scr) -> bool:
        """Known dialogs: yes. A dimmed screen with no recognised dialog: only once it
        has stayed like that for a while (dialogs animate in, so the dim comes first)."""
        if scr.kind in ("dlg_crystal", "dlg_reroll"):
            return True
        return scr.kind == "dimmed" and self._dim_since is not None \
            and time.time() - self._dim_since > 1.5

    def click_and_wait(self, what, locate, expect, timeout, from_kind, tries=None,
                       presettled=False, button=None, then_hover=None, key=None):
        """Click something on screen `from_kind` and wait for the expected screen
        (settled, and with `button` ready if given). The first wait is short
        (react_timeout); before a retry the bot re-checks that the original screen
        is still showing and settled - it never clicks blindly into a screen that
        is in the middle of changing. presettled: the caller has just confirmed
        the screen is still, so the first click needs no extra settle.
        then_hover: rest the pointer where `button` will appear (see settle).
        key: template of the thing being clicked (see click)."""
        def done(f, s):
            early = then_hover if self.pointer_at(then_hover) else None
            return self.settle(f, s, button, early_aim=early)

        # every attempt waits only react_timeout (no long silent waits after a missed click)
        tries = tries or int(self.cfg["click_retries"]) + 1
        last = None
        for attempt in range(tries):
            frame, scr = self.look()
            if scr.kind in expect:
                return done(frame, scr)
            if scr.kind != from_kind:              # still changing: let it finish first
                frame, scr = self.wait_for(set(expect) | {from_kind}, timeout, what)
                if scr.kind in expect:
                    return done(frame, scr)
            if attempt > 0 or not presettled or \
                    (scr.kind == "enchants" and not self.vision.is_clear(frame, scr)):
                frame, scr = self.settle(frame, scr)
                if scr.kind in expect:
                    return done(frame, scr)
            xy = locate(frame, scr) if scr.kind == from_kind else None
            if xy is None:
                path = self.save_debug(frame, scr, "not_found")
                raise BotError(f"Could not find {what} on screen ({scr.summary()}). Screenshot: {path}")
            self.click(xy, what, frame, then_hover=then_hover, key=key)
            try:
                frame, scr = self.wait_for(expect, self.cfg["react_timeout"],
                                           f"a reaction to clicking {what}")
                return done(frame, scr)
            except BotError as exc:
                last = exc
                if attempt + 1 < tries:
                    self.missed_click(f"no reaction to {what}, clicking again "
                                      f"({attempt + 2}/{tries})")
        raise last

    TITLE_OF = {"dlg_crystal": "dlg_crystal_title", "dlg_reroll": "dlg_reroll_title",
                "select_pet": "select_pet_title", "enchants": "enchants_title"}

    def settle(self, frame, scr, button=None, early_aim=None):
        """Wait until the screen has really stopped moving.

        First time a screen/dialog is seen: its title (and `button`) must be matched
        at full size and unmoved for `settle_time` SECONDS (time-based on purpose:
        screenshots arrive faster than the game draws, so two identical screenshots
        do not mean the animation is over). That resting position is remembered.
        After that, a dialog always ends up at the same place, so the bot only waits
        until it ARRIVES there (+ arrive_grace) - no stillness window needed.
        Enchants screens must also be fully un-dimmed (a fading overlay eats clicks).

        early_aim: the pointer already rests on the spot where `button` will come to
        rest. Then return as soon as the still-moving button already lies under the
        pointer with a safe margin (self.settled_early = True) - the press lands on
        it and the button keeps moving deeper under the pointer."""
        self.settled_early = False
        key = self.TITLE_OF.get(scr.kind)
        if key is None:
            return frame, scr
        kind, ref, ref_t = scr.kind, None, None
        final = self._final.get((kind, button))
        arrived_at = None
        t0 = time.time()
        if early_aim is not None and button and final and len(final) >= 4 and self._early_ok:
            bh, bw = (v * self.vision.scale for v in self.vision.t[button].shape[:2])
            # pointer offset inside the resting button; button moves with the title
            px, py = early_aim[0] - final[2], early_aim[1] - final[3]
            margin = 10 * self.vision.scale
        else:
            early_aim = None
        while True:
            m = scr.m(key)
            ok = m is not None and m.score >= 0.8
            if ok and kind == "enchants":
                ok = self.vision.is_clear(frame, scr)
            now = time.time()
            if ok and early_aim is not None and now - t0 < 0.8:
                dx, dy = m.x - final[0], m.y - final[1]
                if margin <= px - dx <= bw - margin and margin <= py - dy <= bh - margin:
                    self.settled_early = True
                    return frame, scr
            # fast path: the title is at its known resting place (the whole dialog
            # moves as one, so the title is enough - and unaffected by button hover)
            if ok and final is not None and now - t0 < 0.8 and m.score >= 0.85 \
                    and abs(m.x - final[0]) <= 1 and abs(m.y - final[1]) <= 1:
                arrived_at = arrived_at or now
                if now - arrived_at >= self.cfg["arrive_grace"]:
                    return frame, scr
            else:
                arrived_at = None
            # first time: title (+ button) unmoved for settle_time, then remember it
            pos = (m.x, m.y) if ok else None
            if pos and button:
                # (hover-aware: a hovered button used to fail this check and every
                # such dialog then sat out the full 1.5 s timeout below)
                b = self.vision.find_dialog_button(frame, scr, button)
                pos = pos + (b.x, b.y) if b is not None and b.score >= T_BUTTON_STILL else None
            if pos:
                if ref is None or any(abs(a - b) > 1 for a, b in zip(pos, ref)):
                    ref, ref_t = pos, now
                elif now - ref_t >= self.cfg["settle_time"]:
                    self._final[(kind, button)] = pos   # learn where it comes to rest
                    return frame, scr
            else:
                ref = None
            if now - t0 > 1.5:
                return frame, scr
            self.sleep(0.002)
            frame, scr = self.look()
            if scr.kind != kind:
                return frame, scr

    def predicted_aim(self, kind, button):
        """Where `button` of dialog `kind` will be once it has finished opening
        (known after it has been seen once), as a click point - or None."""
        f = self._final.get((kind, button))
        if not f or len(f) < 4:
            return None
        h, w = self.vision.t[button].shape[:2]
        s = self.vision.scale
        return (f[2] + int(w * s) // 2, f[3] + int(h * s * 0.42))

    @staticmethod
    def aim(m):
        """Click point for a button: horizontally centred, a bit above the middle
        (keeps a margin from the bottom edge / 3D shadow)."""
        return (m.cx, m.y + int(m.h * 0.42))

    def to_desktop(self, px, py):
        """Frame pixel -> screen pixel (frames are the client area at 1:1, physical px;
        self.rect is refreshed on every screenshot, so a moved window is followed)."""
        return self.rect.x + px, self.rect.y + py

    def pointer_at(self, xy) -> bool:
        p = self._pointer_frame
        return p is not None and xy is not None and abs(p[0] - xy[0]) <= 6 and abs(p[1] - xy[1]) <= 6

    # buttons the game draws bigger/lighter while the pointer is on them
    # (Change Pet has no hover effect - seen in the field - so it is not checked)
    HOVER_KEYS = ("btn_reroll", "btn_close", "btn_confirm", "btn_cancel", "use_crystal_btn")

    def click(self, xy, what, frame=None, then_hover=None, early=False, key=None):
        """then_hover: frame point to rest the pointer on afterwards (where the next
        button will appear) instead of the park spot - the next click then needs no
        travel/hover time. Resting there is only hovering, never pressing.
        early: this press was made while the button was still sliding in.
        key: template of the button being clicked - the press then waits until the
        game visibly shows that button hovered (= Roblox really has the pointer there)."""
        if self.stop_evt.is_set():
            raise Stopped()
        self._last_click = (frame, xy, what)
        self._last_click_early = early
        if self.dry_run or self.mouse is None:
            self.log(f"   (dry run) would click {what}")
            return
        here = self.pointer_at(xy)
        x, y = self.to_desktop(*xy)
        self.trace(f"      click {what} at frame {tuple(int(v) for v in xy)} screen ({int(x)},{int(y)})"
                   f"{' early' if early else ''}")
        self.mouse.move(x, y)                      # absolute move + relative wiggle
        hovered = None if early else self.wait_hover(key, xy)
        if hovered == "gone":
            self.trace(f"      ({what} is no longer on screen - not pressing)")
        else:
            if hovered is None:                    # can't see a hover effect: go by time
                self.sleep(self.cfg["click_hover_resting"] if here else self.cfg["click_hover"])
            self.mouse.press(x, y, self.cfg["click_hold"], self.cfg["click_release_hold"])
        self.mouse.move(*(self.to_desktop(*then_hover) if then_hover else self.park))
        self._pointer_frame = then_hover
        if self.cfg["after_click"] > 0:
            self.sleep(self.cfg["after_click"])

    def _grab_near(self, xy, key):
        """Screenshot of just the area around button `key` at frame point `xy`
        (~1 ms instead of ~20 ms for the whole window). Returns (image, xy in it)."""
        if self.stop_evt.is_set():
            raise Stopped()
        s = self.vision.scale
        h, w = self.vision.t[key].shape[:2]
        hw, hh = w * s * 0.6 + 24 * s, h * s * 0.6 + 24 * s
        r = self.rect
        x0, y0 = max(0, int(xy[0] - hw)), max(0, int(xy[1] - hh))
        x1, y1 = min(r.w, int(xy[0] + hw)), min(r.h, int(xy[1] + hh))
        img = self._capture(Rect(r.x + x0, r.y + y0, max(1, x1 - x0), max(1, y1 - y0)))
        return img, (xy[0] - x0, xy[1] - y0)

    def use_crystal_spot(self):
        """Where Use Crystal is (frame px), once seen - the pointer waits there
        while a result comes in, so the next Use Crystal click is instant."""
        m = self.vision._last.get("use_crystal_btn")
        return self.aim(m) if m is not None else None

    def wait_hover(self, key, xy):
        """After moving onto button `key`: wait until the game draws it hovered,
        wiggling the pointer again if it does not light up. Returns True (hovered,
        press now), None (no hover effect to go by - press after the normal hover
        time) or "gone" (the button is not there any more - don't press blind).
        A button that never showed a hover effect twice is no longer checked."""
        if key not in self.HOVER_KEYS or (key not in self._hover_seen
                                          and self._hover_misses.get(key, 0) >= 2):
            return None
        cfg = self.cfg
        t0 = last_nudge = time.time()
        gone = 0
        while True:
            img, at = self._grab_near(xy, key)
            st = self.vision.hover_state(img, key, at)
            now = time.time()
            if st is True:
                self._hover_seen.add(key)
                self.trace(f"      (hover on {key} seen after {int((now - t0) * 1000)} ms)")
                return True
            gone = gone + 1 if st is None else 0
            if gone >= 3 and key.startswith("btn_"):     # a dialog closed under the pointer
                return "gone"
            if now - t0 > cfg["hover_timeout"]:
                if key not in self._hover_seen:
                    self._hover_misses[key] = self._hover_misses.get(key, 0) + 1
                self.trace(f"      (no hover effect on {key} after {int((now - t0) * 1000)} ms - "
                           "pressing anyway)")
                return None
            if now - last_nudge >= cfg["hover_nudge_every"]:
                self.mouse.nudge()                 # Roblox may still have the old pointer spot
                last_nudge = time.time()
            self.sleep(0.004)

    def missed_click(self, msg):
        """Log a missed click and keep a screenshot showing where it was aimed."""
        self.log(f"   ({msg})")
        if self._last_click_early and self._early_ok:
            self._early_ok = False
            self.log("   (the game ignored a press made while the dialog was still sliding in - "
                     "early presses are off for the rest of this run)")
        frame, xy, what = self._last_click
        if frame is None or self._debug_saved >= 30 or not self.cfg.get("save_debug_images", True):
            return
        self._debug_saved += 1
        img = frame.copy()
        x, y = int(xy[0]), int(xy[1])
        cv2.drawMarker(img, (x, y), (0, 0, 255), cv2.MARKER_CROSS, 40, 3)
        cv2.circle(img, (x, y), 22, (0, 0, 255), 2)
        DEBUG_DIR.mkdir(exist_ok=True)
        path = DEBUG_DIR / f"{time.strftime('%Y%m%d_%H%M%S')}_missed_{what.split()[0].lower()}.png"
        imwrite(str(path), img)
        prune_debug(int(self.cfg.get("keep_error_screenshots", 10)))
        self.log(f"   (screenshot of where it clicked: {path.name})")

    def wait_for(self, kinds, timeout, what, dismiss_popups=True):
        t0 = time.time()
        dismissed = 0
        while True:
            frame, scr = self.look()
            if scr.kind in kinds:
                return frame, scr
            if dismiss_popups and scr.kind == "dimmed" and self.popup_needs_closing(scr) \
                    and dismissed < 2:
                self.dismiss(frame, scr)
                dismissed += 1
                continue
            if time.time() - t0 > timeout:
                path = self.save_debug(frame, scr, "timeout")
                raise BotError(f"Timed out waiting for {what} (seeing: {scr.summary()}). "
                               f"Screenshot: {path}")
            self.sleep(0.005)

    def dismiss(self, frame, scr):
        """Close a popup WITHOUT confirming anything (Cancel / Close / X only)."""
        if scr.kind == "dlg_crystal":
            btn = self.vision.find_dialog_button(frame, scr, "btn_cancel")
        elif scr.kind == "dlg_reroll":
            btn = self.vision.find_dialog_button(frame, scr, "btn_close")
        else:
            btn = None
            for key in ("btn_close", "btn_cancel"):
                m = self.vision.find(frame, key, color=True)
                if m and m.score >= 0.75:
                    btn = m
                    break
        if btn is None:
            path = self.save_debug(frame, scr, "unknown_popup")
            raise BotError(f"An unknown popup is open ({scr.summary()}) and I could not find a "
                           f"Close/Cancel button. Close it manually. Screenshot: {path}")
        self.log(f"   closing popup ({scr.kind}) with {btn.key}")
        self.click(self.aim(btn), btn.key, frame, key=btn.key)

    # ------------------------------------------------------------ main
    def run(self):
        self.stop_evt.clear()
        self.status(state="starting")
        prune_debug(int(self.cfg.get("keep_error_screenshots", 10)))
        try:
            DEBUG_DIR.mkdir(exist_ok=True)
            self._logfile = open(DEBUG_DIR / "last_run.log", "w", encoding="utf-8")
        except OSError:
            self._logfile = None
        try:
            if not ocr.tesseract_available():
                raise FatalBotError("Tesseract OCR not found. Install it (setup.bat shows how) or set "
                                    "\"tesseract_path\" in config.json to tesseract.exe.")
            self.with_retries(self.connect)
            if not self.dry_run:
                self.mouse = plat.Mouse()
                self.mouse.move(*self.park)
            self.with_retries(self.calibrate)
            self.load_learned()
            if self.dry_run:
                self.analyze()
                return
            self.hotkey = plat.InputWatcher(self.cfg["stop_hotkey"], self._hotkey_pressed,
                                            watch_mouse=self.cfg.get("stop_on_mouse_move", True),
                                            mouse=self.mouse)
            self.hotkey.start()
            time.sleep(0.3)
            if self.hotkey.error:
                self.log(f"WARNING: stop hotkey unavailable ({self.hotkey.error}) - use the Stop button")
            else:
                key = self.cfg["stop_hotkey"].replace("KEY_", "")
                extra = " or move your mouse" if self.cfg.get("stop_on_mouse_move", True) else ""
                self.log(f"Running. Press {key}{extra} to stop - don't touch the mouse while it works.")
            self.log(f"Targets: {' + '.join(self.targets)}"
                     f"{' (fixed slot order)' if self.cfg['order_matters'] else ''}")
            self.status(state="running")
            self.with_retries(self.main_loop)
        except Stopped:
            self.log("Stopped.")
        except BotError as exc:
            self.log(f"ERROR: {exc}")
        except Exception:
            self.log("CRASH:\n" + traceback.format_exc())
        finally:
            if self.hotkey:
                self.hotkey.stop()
            if self.mouse:
                self.mouse.close()
            if self.cap:
                self.cap.close()
            self.save_learned()
            self.log(f"Crystals used: {self.stats['crystals']}, pets finished: "
                     f"{self.stats['pets_done']}")
            if self._logfile is not None:
                self._logfile.close()
                self._logfile = None
            self.status(state="idle")

    # learned names are stored per UI scale (each window size has its own scale)
    @staticmethod
    def _learned_sets():
        try:
            data = json.loads(LEARNED_PATH.read_text())
        except (OSError, ValueError):
            return []
        return data.get("sets", [data]) if isinstance(data, dict) else []

    def load_learned(self, quiet=False):
        n = 0
        for s in self._learned_sets():
            try:
                n += self.vision.import_learned(s)
            except (KeyError, TypeError, ValueError):
                pass
        if n and not quiet:
            self.log(f"Remembered {n} enchant names from earlier runs (no OCR needed for them).")

    def save_learned(self):
        if self.vision.scale is None or not any(self.vision._learned.values()):
            return
        cur = self.vision.export_learned()
        sets = [s for s in self._learned_sets()
                if abs(float(s.get("scale", 0)) - cur["scale"]) > 0.005][-5:] + [cur]
        try:
            LEARNED_PATH.write_text(json.dumps({"sets": sets}))
        except OSError:
            pass

    def _hotkey_pressed(self, reason="hotkey"):
        if not self.stop_evt.is_set():
            self._log("[stop] you moved/clicked your mouse - stopping so no click lands in the "
                      "wrong place" if reason == "mouse" else "[stop] stop key pressed")
            self.stop()

    # ------------------------------------------------------------ error recovery
    def with_retries(self, fn):
        """Run fn(); if it fails, wait error_retry_wait (0.25 s), look at the screen
        again and continue - up to error_retries (4) times in a row before giving
        up. Any real progress (a crystal used, a pet finished) resets the count."""
        tries = int(self.cfg["error_retries"])
        wait = float(self.cfg["error_retry_wait"])
        failures, mark = 0, self._progress
        while True:
            try:
                return fn()
            except (Stopped, FatalBotError):
                raise
            except Exception as exc:
                if self._progress != mark:
                    failures = 0
                mark = self._progress
                failures += 1
                if failures > tries:
                    raise
                msg = str(exc) if isinstance(exc, BotError) else f"{type(exc).__name__}: {exc}"
                self.log(f"   (problem: {msg.splitlines()[0][:220]} - checking again in "
                         f"{wait:g}s, try {failures}/{tries})")
                self.sleep(wait)
                self._last_enchants = self._dim_since = self._grid_title = None
                try:
                    self._refocus()            # another window may have covered Roblox
                except Exception:
                    pass

    # ------------------------------------------------------------ main loop
    def main_loop(self):
        """Resumable: after an error it is simply called again and continues with
        the pet / pass it was on (state lives in self._pass_no, _order, _pos, _cur)."""
        frame, scr = self.look()
        if scr.kind == "dimmed":
            self.sleep(1.6)
            frame, scr = self.look()
        if scr.kind in DIALOGS:
            self.dismiss(frame, scr)
            frame, scr = self.wait_for({"enchants", "select_pet"}, self.cfg["screen_timeout"],
                                       "the enchant book")
        if scr.kind == "enchants":
            if self._cur is not None:               # a problem happened inside this pet
                self.finish_pet(self._cur)
            elif not self.cfg.get("reenchant_once"):  # (re-enchanting: the pass does every pet)
                if self._order is None:
                    self.log("An Enchants screen is open - doing that pet first.")
                if self.enchant_current("Open pet") == "done":
                    self._progress += 1
        only = set(int(i) for i in self.cfg.get("only_pets") or [])
        while self._pass_no <= int(self.cfg["passes"]):
            if self._order is None:
                self.start_pass(only)
            while self._pos < len(self._order):
                i = self._order[self._pos]
                self.to_select_pet()
                self._cur = i
                self.click_and_wait(f"pet #{i + 1}", self.locate_pet(i), {"enchants"},
                                    self.cfg["screen_timeout"], from_kind="select_pet", presettled=True)
                self.finish_pet(i)
            if self.stats["crystals"] == self._pass_used_before:
                self.log(f"Pass {self._pass_no} used no crystals: {self._done_this_pass}/"
                         f"{len(self._order)} pets have the target combination. Finished!")
                break
            self.log(f"Pass {self._pass_no} done; running another pass to verify "
                     "(pets may have re-sorted).")
            self._pass_no += 1
            self._order = None
        self.to_select_pet()

    def start_pass(self, only):
        frame, scr = self.to_select_pet()
        all_cards = self.vision.find_pets(frame, scr)
        pets = [p for p in all_cards if not p.small]
        if not pets:
            path = self.save_debug(frame, scr, "no_pets", pets=all_cards)
            raise BotError(f"No eligible equipped pets found. Screenshot: {path}")
        self._order = [i for i in range(len(pets)) if not only or (i + 1) in only]
        self._pos, self._cur, self._done_this_pass = 0, None, 0
        self._pass_used_before = self.stats["crystals"]
        self._pets, self._grid_title = pets, scr.m("select_pet_title")
        self.log(f"--- Pass {self._pass_no}: {len(pets)} eligible equipped pets "
                 f"({len(all_cards) - len(pets)} Super Legendary skipped), doing {len(self._order)}")
        self.status(pets_seen=len(self._order))

    def locate_pet(self, i):
        def locate(f, s):
            if s.kind != "select_pet":
                return None
            t = s.m("select_pet_title")
            g = self._grid_title
            if g is None or t is None or abs(t.x - g.x) > 3 or abs(t.y - g.y) > 3:
                self._pets = [p for p in self.vision.find_pets(f, s) if not p.small]  # moved
                self._grid_title = t
            return self._pets[i].center if i < len(self._pets) else None
        return locate

    def finish_pet(self, i):
        """Enchant the open pet #i, then mark it handled for this pass."""
        t_pet, used_pet = time.time(), self.stats["crystals"]
        if self.enchant_current(f"Pet #{i + 1}", renew_key=i) == "done":
            self._done_this_pass += 1
            self._progress += 1
            self.status(pets_done=self._done_this_pass)
        n = self.stats["crystals"] - used_pet
        if n:
            dt = time.time() - t_pet
            self.log(f"   ({n} crystal{'s' if n != 1 else ''} in {dt:.1f}s = {dt / n:.2f}s per crystal)")
        self._pos += 1
        self._cur = None

    def to_select_pet(self):
        deadline = time.time() + self.cfg["screen_timeout"] * 2
        while True:
            frame, scr = self.look()
            if scr.kind == "select_pet":
                return frame, scr
            if time.time() > deadline:
                path = self.save_debug(frame, scr, "no_select_pet")
                raise BotError(f"Could not get to the Select Pet screen ({scr.summary()}). "
                               f"Screenshot: {path}")
            if scr.kind in DIALOGS:
                if self.popup_needs_closing(scr):
                    self.dismiss(frame, scr)
                else:
                    self.sleep(0.3)
            elif scr.kind == "enchants":
                def locate_cp(f, s):
                    cp, _ = self.vision.enchant_buttons(f) if s.kind == "enchants" else (None, None)
                    return self.aim(cp) if cp else None
                self.click_and_wait("Change Pet", locate_cp, {"select_pet"}, self.cfg["screen_timeout"],
                                    from_kind="enchants", presettled=True, key="change_pet")
            else:
                self.sleep(0.4)

    # ------------------------------------------------------------ one pet
    def read_slots_stable(self):
        """Wait until the slot cards have not changed for `stable_time` seconds
        (animations finished), then read them."""
        first = self._last_enchants       # newest Enchants frame seen by after_confirm
        self._last_enchants = None
        ref = ref_t = None
        t0 = time.time()
        told = False
        while time.time() - t0 < 4.0:
            if first is not None:
                (frame, scr), first = first, None
            else:
                frame, scr = self.look()
            if not told and time.time() - t0 > 0.6:     # say what it is waiting for
                told = True
                self.trace(f"      (waiting for readable enchant slots, seeing {scr.summary()})")
            if scr.kind != "enchants":
                if scr.kind == "dlg_reroll":
                    return frame, scr, None
                if scr.kind in DIALOGS and self.popup_needs_closing(scr):
                    self.dismiss(frame, scr)
                else:
                    self.sleep(0.02)
                ref = None
                continue
            cp, uc = self.vision.enchant_buttons(frame)
            if cp is None:
                self.sleep(0.02)
                continue
            s = self.vision.scale
            y1 = uc.y if uc else int(cp.y + 760 * s)
            x0, x1 = max(0, int(cp.cx - 200 * s)), int(cp.cx + 200 * s)
            col = cv2.cvtColor(frame[int(cp.y + 150 * s):y1, x0:x1], cv2.COLOR_BGR2GRAY)
            col = cv2.resize(col, None, fx=0.25, fy=0.25, interpolation=cv2.INTER_AREA).astype(np.float32)
            now = time.time()
            clear = self.vision.is_clear(frame, scr)    # no dialog overlay still fading
            if not clear or ref is None or ref.shape != col.shape \
                    or float(np.abs(col - ref).mean()) >= 4:
                ref, ref_t = (col, now) if clear else (None, None)
            if (ref_t is not None and now - ref_t >= self.cfg["stable_time"]) or now - t0 > 1.5:
                if now - t0 > 1.5:
                    self.log("   (enchant slots never settled within 1.5s - reading anyway)")
                slots = self.vision.read_slots(frame, cp, uc)
                if slots is not None:
                    return frame, scr, slots
            self.sleep(0.005)
        frame, scr = self.look()
        path = self.save_debug(frame, scr, "slots_unreadable")
        raise BotError(f"Could not read the enchant slots ({scr.summary()}). Screenshot: {path}")

    def enchant_current(self, label, renew_key=None):
        """renew_key: the pet's grid position. With reenchant_once, a pet that was not
        re-enchanted yet in this run first gets ALL slots rerolled once - wanted
        enchants too, since they may be worn out - and then the combination again."""
        used = closes = lock_rounds = 0
        hint = None
        pre_slots = []
        pre_sig = None
        cfg = self.cfg
        renew = renew_key is not None and bool(cfg.get("reenchant_once")) \
            and self._pass_no == 1 and renew_key not in self._renewed

        def handle(outcome, named):
            nonlocal closes, hint, renew
            if renew and used and outcome == "rerolled":    # the re-enchant crystal went through
                renew = False
                self._renewed.add(renew_key)
            if outcome == "closed":
                closes += 1
                hint = named
                if closes >= 3:
                    raise FatalBotError("The reroll confirmation keeps naming an enchant I want to "
                                        "keep but cannot lock - please check this pet manually.")
            else:
                closes = 0

        fresh = None
        while True:
            self._forcing = renew                   # the 2/2 dialog is answered Reroll then
            if fresh is not None:                   # already read while verifying a lock
                (frame, scr, slots), fresh = fresh, None
            else:
                frame, scr, slots = self.read_slots_stable()
            if slots is None:                       # a 2/2 dialog showed up late
                handle(*self.after_confirm(pre_slots, pre_sig))
                continue
            if hint:
                self.apply_hint(slots, hint)
                hint = None
            plan = evaluate(slots, self.targets, cfg["order_matters"], cfg["unlock_unwanted"])
            if renew:                               # re-enchant: unlock all, reroll every slot
                plan = Plan(done=False, to_unlock=[s.index for s in slots if s.lock == LOCKED])
            desc = " | ".join(s.describe() for s in slots) or "(no enchants)"
            self.log(f"{label}: {desc}")
            if renew and not used and not lock_rounds:
                self.log("   re-enchanting it once: every slot gets rerolled, then the combination "
                         "is rolled again")

            if plan.done:              # both enchants present -> next pet, no locking
                self.log(f"   {label} DONE: {' + '.join(self.targets)} "
                         f"({used} crystal{'s' if used != 1 else ''})")
                return "done"
            if plan.stuck:
                self.log(f"   {label} skipped: {plan.reason}")
                return "stuck"

            if plan.to_lock or plan.to_unlock:
                if lock_rounds >= 3:
                    path = self.save_debug(frame, scr, "lock_failed", slots=slots)
                    raise BotError(f"Clicking the lock icon is not working. Screenshot: {path}")
                lock_rounds += 1
                expected = {}
                for i in plan.to_lock:
                    s = slots[i]
                    self.log(f"   locking {s.name}")
                    self.click(s.lock_xy, f"lock on {s.name}", frame, then_hover=self.use_crystal_spot())
                    expected[i] = LOCKED
                for i in plan.to_unlock:
                    s = slots[i]
                    self.log(f"   unlocking {'' if renew else 'unwanted '}{s.name or s.raw}")
                    self.click(s.lock_xy, f"lock on {s.name or s.raw}", frame,
                               then_hover=self.use_crystal_spot())
                    expected[i] = UNLOCKED
                if self.dry_run:
                    return "dry"
                # A lock icon is a toggle: never click it again until we are sure the
                # first click really did nothing (otherwise we would undo it). The game
                # needs a server round-trip, so give it up to lock_timeout.
                t_lock = time.time()
                while time.time() - t_lock < cfg["lock_timeout"]:
                    frame, scr, now = self.read_slots_stable()
                    if now and all(i < len(now) and now[i].lock == st for i, st in expected.items()):
                        fresh = (frame, scr, now)
                        break
                    self.sleep(0.01)
                continue
            lock_rounds = 0

            if cfg["max_crystals_total"] and self.stats["crystals"] >= cfg["max_crystals_total"]:
                raise FatalBotError(f"Total crystal limit ({cfg['max_crystals_total']}) reached.")

            if self.dry_run:
                return "dry"
            pre_slots = slots
            pre_sig = self.vision.result_signature(frame)

            def locate_uc(f, s):
                _, uc = self.vision.enchant_buttons(f) if s.kind == "enchants" else (None, None)
                return self.aim(uc) if uc else None

            # The slots were just read from a still, clear screen -> click right away,
            # then wait with the pointer already resting where Confirm will appear.
            confirm_at = self.predicted_aim("dlg_crystal", "btn_confirm")
            frame, scr = self.click_and_wait("Use Crystal (out of Shadow Crystals?)", locate_uc,
                                             {"dlg_crystal"}, cfg["dialog_timeout"], from_kind="enchants",
                                             presettled=True, button="btn_confirm", then_hover=confirm_at,
                                             key="use_crystal_btn")
            early = self.settled_early
            btn = self.vision.find_dialog_button(frame, scr, "btn_confirm") \
                if scr.kind == "dlg_crystal" else None
            if btn is None:
                path = self.save_debug(frame, scr, "no_confirm")
                raise BotError(f"Confirm button not found in the crystal dialog. Screenshot: {path}")
            # Wait for the 2/2 "reroll your X?" dialog (if it can come) with the pointer
            # already resting where its Reroll button will appear, otherwise on Use
            # Crystal (the next click). Only hovering.
            if self.expects_reroll_dialog(pre_slots):
                reroll_at = self.predicted_aim("dlg_reroll", "btn_reroll")
            else:
                reroll_at = self.use_crystal_spot()
            conf_sig = self.vision.result_signature(frame)    # counter as seen at the click
            self.click(confirm_at if early else self.aim(btn), "Confirm (1/2)", frame,
                       then_hover=reroll_at, early=early, key="btn_confirm")
            used += 1
            self._progress += 1
            self.status(crystals=self.stats["crystals"] + 1)
            handle(*self.after_confirm(pre_slots, pre_sig, conf_sig))

    def apply_hint(self, slots, named):
        """The 2/2 dialog told us an unlocked slot holds `named` but OCR missed it."""
        if named not in self.targets or any(s.name == named for s in slots):
            return
        cands = [s for s in slots if s.lock == UNLOCKED and (s.name is None or s.name not in self.targets)]
        if len(cands) == 1:
            self.log(f"   (slot {cands[0].index + 1} read as {cands[0].raw!r}; the dialog says it is "
                     f"{named} - treating it as {named})")
            cands[0].name = named

    def expects_reroll_dialog(self, pre_slots) -> bool:
        """The game asks 'reroll your X?' (2/2) if an unlocked slot holds a secret -
        or another rare one it asked about earlier in this run (Looter V, Magnetism...)."""
        asks = set(SECRET_ENCHANTS) | self._asks_about
        return any(s.lock != LOCKED and (s.name is None or s.name in asks) for s in pre_slots)

    def after_confirm(self, pre_slots, pre_sig, conf_sig=None):
        """After Confirm (1/2): the 2/2 'reroll your X?' dialog appears (only if an
        unlocked slot holds a secret enchant) and/or the result arrives. The result
        is detected the moment the crystal counter ticks or the slot changes - no
        fixed waiting. Returns (outcome, named_enchant)."""
        v, cfg = self.vision, self.cfg
        expect = self.expects_reroll_dialog(pre_slots)
        max_retries = int(cfg["click_retries"])
        t0 = time.time()
        confirm_retries = reanswers = 0
        last_confirm = t0
        answered_at = ans_cnt = changed_at = None
        while True:
            frame, scr = self.look()
            now = time.time()
            if scr.kind == "dlg_reroll":
                title = scr.m("dlg_reroll_title")
                # Re-answer only if the dialog is still FULLY open well after the click
                # (a dialog that took the click closes at once) and no crystal was used.
                missed = False
                if answered_at is not None and reanswers < max_retries and title is not None \
                        and now - answered_at > cfg["retry_after"] and title.score >= 0.85:
                    sig = v.result_signature(frame)
                    missed = not (ans_cnt is not None and sig and v.counter_changed(ans_cnt, sig[1]))
                if answered_at is None or missed:
                    if missed:
                        reanswers += 1
                        self.missed_click(f"the 2/2 dialog did not take the click - answering again "
                                          f"({reanswers}/{max_retries})")
                    sig_d = v.result_signature(frame)          # counter is not covered by it
                    outcome, named = self.answer_reroll_dialog(frame, scr, pre_slots)
                    if outcome == "closed":
                        self.wait_for({"enchants"}, cfg["dialog_timeout"], "the Enchants screen")
                        return "closed", named
                    if answered_at is None:
                        ans_cnt = sig_d[1] if sig_d else None
                    answered_at = time.time()
                    continue
            elif scr.kind == "dlg_crystal":
                title = scr.m("dlg_crystal_title")
                if confirm_retries < max_retries and now - last_confirm > cfg["retry_after"] \
                        and title is not None and title.score >= 0.85:
                    # still fully open and no crystal used since the click -> missed
                    sig = v.result_signature(frame)
                    if not (conf_sig and sig and v.counter_changed(conf_sig[1], sig[1])):
                        frame, scr = self.settle(frame, scr, button="btn_confirm")
                        btn = v.find_dialog_button(frame, scr, "btn_confirm") \
                            if scr.kind == "dlg_crystal" else None
                        if btn:
                            confirm_retries += 1
                            self.missed_click(f"Confirm (1/2) did not take the click - clicking again "
                                              f"({confirm_retries}/{max_retries})")
                            self.click(self.aim(btn), "Confirm (1/2) retry", frame, key="btn_confirm")
                    last_confirm = time.time()
            elif scr.kind == "enchants":
                sig = v.result_signature(frame)
                # a closing dialog can still overlap the slot column -> only trust the
                # column once the screen is fully clear (the counter is never covered)
                col_changed = bool(sig and pre_sig) and v.is_clear(frame, scr) \
                    and v.column_changed(pre_sig[0], sig[0])
                if answered_at is not None:
                    cnt_changed = bool(sig) and ans_cnt is not None and v.counter_changed(ans_cnt, sig[1])
                    if cnt_changed or col_changed or now - answered_at > cfg["result_fallback"]:
                        if not (cnt_changed or col_changed):
                            self.log(f"   (no visible change {cfg['result_fallback']}s after the 2/2 "
                                     "dialog - same enchant again? continuing)")
                        self._last_enchants = (frame, scr)
                        return "rerolled", None
                else:
                    changed = col_changed or (bool(sig and pre_sig) and v.counter_changed(pre_sig[1], sig[1]))
                    if changed:
                        if not expect:
                            self._last_enchants = (frame, scr)
                            return "rerolled", None
                        changed_at = changed_at or now        # 2/2 may still be on its way
                        if now - changed_at > 0.4:
                            self._last_enchants = (frame, scr)
                            return "rerolled", None
                    else:
                        if pre_sig is None or sig is None:
                            limit = cfg["expect_dialog_wait"] if expect else cfg["result_wait"]
                        else:
                            limit = cfg["expect_dialog_wait"] if expect else cfg["result_fallback"]
                        if now - t0 > limit:
                            path = self.save_debug(frame, scr, "no_change")
                            self.log(f"   (no visible change {limit}s after confirming - continuing)"
                                     + (f" Screenshot: {path.name}" if path else ""))
                            self._last_enchants = (frame, scr)
                            return "rerolled", None
            elif scr.kind == "dimmed" and self.popup_needs_closing(scr):
                self.log("   unexpected popup after confirming - closing it without confirming")
                self.dismiss(frame, scr)
                return "closed", None
            if now - t0 > cfg["dialog_timeout"] + (max_retries + 1) * cfg["retry_after"] + 2:
                path = self.save_debug(frame, scr, "after_confirm")
                raise BotError(f"Nothing happened after confirming the crystal ({scr.summary()}). "
                               f"Screenshot: {path}")
            self.sleep(0.002)

    def answer_reroll_dialog(self, frame, scr, pre_slots):
        reroll_at = self.predicted_aim("dlg_reroll", "btn_reroll")
        early_aim = reroll_at if self.pointer_at(reroll_at) else None
        frame, scr = self.settle(frame, scr, button="btn_reroll", early_aim=early_aim)
        early = self.settled_early
        if scr.kind != "dlg_reroll":
            return "rerolled", None
        raw, named, score = self.vision.read_dialog_name(frame, scr)
        if named:
            self._asks_about.add(named)
        if self._forcing:
            decision, why = "reroll", "re-enchanting this pet once - every enchant gets rerolled"
        else:
            decision, why = decide_dialog(named, pre_slots, self.targets, self.cfg["order_matters"])
        self.log(f"   2/2 confirmation names '{named or raw or '?'}' -> {decision.upper()} ({why})")
        if not named and not self._dialog_shot_saved:       # once per run: to fix the reading
            self._dialog_shot_saved = True
            path = self.save_debug(frame, scr, "dialog_unreadable")
            if path:
                self.log(f"   (screenshot of the unreadable dialog: {path.name})")
        if decision == "reroll" and early:
            # pointer already rests where Reroll is sliding to - press now
            self.click(reroll_at, "btn_reroll", frame, early=True, then_hover=self.use_crystal_spot())
            return "rerolled", named
        key = "btn_reroll" if decision == "reroll" else "btn_close"
        if early:                                   # Close: let the dialog finish first
            frame, scr = self.settle(frame, scr, button=key)
            if scr.kind != "dlg_reroll":
                return "rerolled", None
        btn = self.vision.find_dialog_button(frame, scr, key)
        if btn is None:
            path = self.save_debug(frame, scr, "no_dialog_button")
            raise BotError(f"'{key}' not found in the reroll confirmation. Screenshot: {path}")
        self.click(self.aim(btn), key, frame, key=key,
                   then_hover=self.use_crystal_spot() if key == "btn_reroll" else None)
        return ("closed" if decision == "close" else "rerolled"), named

    # ------------------------------------------------------------ dry run
    def analyze(self):
        """Look at the current screen and report what the bot would do. No clicks."""
        frame, scr = self.look()
        self.log(f"Screen: {scr.summary()}")
        slots = pets = None
        if scr.kind == "enchants":
            slots = self.vision.read_slots(frame) or []
            for s in slots:
                self.log(f"   slot {s.index + 1}: {s.describe()}  (ocr {s.raw!r}, {s.score:.2f})")
            plan = evaluate(slots, self.targets, self.cfg["order_matters"], self.cfg["unlock_unwanted"])
            todo = []
            todo += [f"lock {slots[i].name}" for i in plan.to_lock]
            todo += [f"unlock {slots[i].name or slots[i].raw}" for i in plan.to_unlock]
            if plan.done:
                todo.append("pet finished")
            elif plan.stuck:
                todo.append(f"skip ({plan.reason})")
            else:
                todo.append("use a Shadow Crystal")
            if self.cfg.get("reenchant_once"):
                todo = [f"unlock {s.name or s.raw}" for s in slots if s.lock == LOCKED] + \
                    ["reroll every slot once (re-enchant)", "roll the combination again"]
            self.log(f"   targets {' + '.join(self.targets)} -> would: {', then '.join(todo)}")
        elif scr.kind == "select_pet":
            pets = self.vision.find_pets(frame, scr)
            n = sum(not p.small for p in pets)
            self.log(f"   {n} eligible equipped pets, {len(pets) - n} small (Super Legendary) skipped")
        elif scr.kind == "dlg_reroll":
            raw, named, score = self.vision.read_dialog_name(frame, scr)
            self.log(f"   dialog names {named!r} (ocr {raw!r}, {score:.2f})")
        path = self.save_debug(frame, scr, slots=slots, pets=pets, name=TEST_IMAGE)
        self.log(f"Annotated screenshot: {path}")
