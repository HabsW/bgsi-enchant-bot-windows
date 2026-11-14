"""Screen understanding: which screen is open, where the buttons are, what is in
the enchant slots, which pets are eligible.

All reference sizes below are in pixels at UI scale 1.0 (the size of the
templates/) and are multiplied by `self.scale`, which is found once by
multi-scale template matching on the window titles."""
import base64
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

import cv2
import numpy as np

from . import ocr
from .imgio import imread
from .logic import LOCKED, UNLOCKED, Slot

TITLES = ("dlg_reroll_title", "dlg_crystal_title", "select_pet_title", "enchants_title")
DIALOG_KIND = {"dlg_reroll_title": "dlg_reroll", "dlg_crystal_title": "dlg_crystal"}

CARD_BG_BGR = np.array([244, 205, 126], np.int16)   # light-blue enchant slot card
PET_CARD = 130                                      # equipped pet card size (ref px)

T_DIALOG = 0.70     # dialog title match threshold
T_TITLE = 0.62      # window title match threshold
T_BUTTON = 0.62
T_BUTTON_STILL = 0.72   # a button this good is drawn completely (used while settling)
DIM_RATIO = 0.78    # brightness(region)/brightness(template) below this = dimmed by an overlay
# Buttons are matched at their normal size and at the sizes the game grows them
# to while hovered (measured on a 2560x1440 screenshot: best at 1.03).
HOVER_SCALES = (1.0, 1.01, 1.02, 1.03, 1.04, 1.05)
HOVER_MIN = 1.01    # best size >= this = drawn hovered (or starting to grow)


@dataclass
class Match:
    key: str
    x: int
    y: int
    w: int
    h: int
    score: float
    hovered: bool = False

    @property
    def cx(self) -> int:
        return self.x + self.w // 2

    @property
    def cy(self) -> int:
        return self.y + self.h // 2

    @property
    def center(self):
        return (self.cx, self.cy)


@dataclass
class Screen:
    kind: str                     # select_pet | enchants | dlg_crystal | dlg_reroll | dimmed | unknown
    matches: Dict[str, Optional[Match]] = field(default_factory=dict)
    note: str = ""

    def m(self, key) -> Optional[Match]:
        return self.matches.get(key)

    def summary(self) -> str:
        sc = ", ".join(f"{k.replace('_title', '')}={m.score:.2f}" for k, m in self.matches.items() if m)
        return f"{self.kind} ({sc}){' ' + self.note if self.note else ''}"


@dataclass
class PetCard:
    x: int
    y: int
    w: int
    h: int
    small: bool                   # Super Legendary (smaller card) -> never clicked

    @property
    def center(self):
        return (self.x + self.w // 2, self.y + self.h // 2)


def _gray(img):
    return img if img.ndim == 2 else cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)


def _clamp_roi(roi, shape):
    x0, y0, x1, y1 = (int(round(v)) for v in roi)
    h, w = shape[:2]
    return max(0, x0), max(0, y0), min(w, x1), min(h, y1)


class Vision:
    def __init__(self, template_dir, names: List[str], scale: Optional[float] = None):
        self.t = {}
        for p in sorted(Path(template_dir).glob("*.png")):
            img = imread(str(p), cv2.IMREAD_COLOR)
            if img is not None:
                self.t[p.stem] = img
        missing = set(TITLES) - set(self.t)
        if missing:
            raise FileNotFoundError(f"missing templates: {sorted(missing)} in {template_dir}")
        self.tg = {k: _gray(v) for k, v in self.t.items()}
        self.names = names
        self.scale = scale
        self._cache = {}
        self._last = {}       # key -> last confident Match (UI elements don't move)
        # Learned text: pixel shape of a name -> what tesseract said about it. The
        # same enchant renders identically every time, so after the first read a
        # name is recognised in ~1 ms instead of ~60-200 ms of OCR.
        self._learned = {"slot": [], "dialog": []}
        self.ocr_calls = 0
        # Families whose roman numeral matters (a target like 'Bubbler V'). None =
        # all of them. For the rest one confident read is enough: 'Bubbler I' or
        # 'Bubbler II' makes no difference to what the bot does (saves 3 OCR runs).
        self.exact_numerals = None

    # ------------------------------------------------------------------ matching
    def _tpl(self, key, s, gray=True):
        ck = (key, round(s, 4), gray)
        if ck not in self._cache:
            src = self.tg[key] if gray else self.t[key]
            w = max(4, int(round(src.shape[1] * s)))
            h = max(4, int(round(src.shape[0] * s)))
            interp = cv2.INTER_AREA if s < 1 else cv2.INTER_LINEAR
            self._cache[ck] = cv2.resize(src, (w, h), interpolation=interp)
        return self._cache[ck]

    def _match(self, img, key, s, roi=None) -> Optional[Match]:
        gray = img.ndim == 2
        tpl = self._tpl(key, s, gray)
        ox = oy = 0
        if roi is not None:
            x0, y0, x1, y1 = _clamp_roi(roi, img.shape)
            img = img[y0:y1, x0:x1]
            ox, oy = x0, y0
        if img.shape[0] < tpl.shape[0] or img.shape[1] < tpl.shape[1]:
            return None
        r = cv2.matchTemplate(img, tpl, cv2.TM_CCOEFF_NORMED)
        r = np.nan_to_num(r, nan=0.0, posinf=0.0, neginf=0.0)
        _, mx, _, loc = cv2.minMaxLoc(r)
        return Match(key, loc[0] + ox, loc[1] + oy, tpl.shape[1], tpl.shape[0], float(mx))

    def find(self, frame, key, roi=None, color=False) -> Optional[Match]:
        img = frame if color else _gray(frame)
        return self._match(img, key, self.scale, roi)

    # --------------------------------------------------------------- calibration
    def calibrate(self, frame):
        """Find the UI scale from whichever window title is visible.
        Returns (scale, title_key, score) or None."""
        g = _gray(frame)
        f = 0.5 if min(g.shape[:2]) > 600 else 1.0
        gs = cv2.resize(g, None, fx=f, fy=f, interpolation=cv2.INTER_AREA) if f != 1 else g
        coarse = {}                                   # best coarse hit per title
        for s in np.geomspace(0.3, 2.4, 48):
            for key in TITLES:
                if self.tg[key].shape[1] * s * f < 48:   # too tiny to be meaningful
                    continue
                m = self._match(gs, key, s * f)
                if m and (key not in coarse or m.score > coarse[key][0]):
                    coarse[key] = (m.score, float(s), m)
        refined = []
        for key, (score, s0, m) in coarse.items():
            if score < 0.5:
                continue
            pad = 80
            roi = (m.x / f - pad, m.y / f - pad, (m.x + m.w) / f + pad, (m.y + m.h) / f + pad)
            for s in np.linspace(s0 * 0.95, s0 * 1.05, 11):
                mm = self._match(g, key, float(s), roi)
                if mm and mm.score > score:
                    score, s0 = mm.score, float(s)
            refined.append((score, s0, key))
        for score, s0, key in sorted(refined, reverse=True):
            if score < 0.72:
                break
            old = self.scale
            self.scale = s0
            self._cache.clear()
            self._last.clear()
            if self._verify(frame, key):
                return s0, key, score
            self.scale = old
        self._cache.clear()
        self._last.clear()
        return None

    def _match_near(self, img, key, s, f=1.0, pad_ref=40.0):
        """Match only around the last known position (full-res coords in self._last).
        img may be downscaled by f. Returns a full-res Match or None."""
        last = self._last.get(key)
        if last is None:
            return None
        pad = pad_ref * self.scale
        roi = ((last.x - pad) * f, (last.y - pad) * f,
               (last.x + last.w + pad) * f, (last.y + last.h + pad) * f)
        m = self._match(img, key, s * f, roi)
        if m is not None and f != 1:
            m = Match(key, int(m.x / f), int(m.y / f), int(m.w / f), int(m.h / f), m.score)
        return m

    def _verify(self, frame, key) -> bool:
        """Second, independent anchor at the candidate scale."""
        if key == "enchants_title":
            cp, uc = self.enchant_buttons(frame)
            return cp is not None or uc is not None
        if key == "select_pet_title":
            scr = self.detect(frame)
            return len(self.find_pets(frame, scr)) >= 1
        scr = self.detect(frame)
        if key == "dlg_crystal_title":
            return any(self.find_dialog_button(frame, scr, k) for k in ("btn_confirm", "btn_cancel"))
        return any(self.find_dialog_button(frame, scr, k) for k in ("btn_reroll", "btn_close"))

    # ------------------------------------------------------------------- screens
    def detect(self, frame, full=False) -> Screen:
        """full=False: titles seen before are only searched near their last position
        (UI windows/dialogs always open at the same place) - much faster."""
        if self.scale is None:
            raise RuntimeError("Vision not calibrated")
        g = _gray(frame)
        f = 0.5 if self.scale > 0.6 and min(g.shape[:2]) > 600 else 1.0
        gs = cv2.resize(g, None, fx=f, fy=f, interpolation=cv2.INTER_AREA) if f != 1 else g
        matches = {}
        s = self.scale
        anchor = self._last.get("enchants_title") or self._last.get("select_pet_title")
        for key in TITLES:
            # dialogs slide in: a wide local window keeps tracking them cheaply
            pad = 170 if key in DIALOG_KIND else 40
            m = None if full else self._match_near(gs, key, s, f, pad_ref=pad)
            if m is None and (full or key not in self._last):
                roi = None
                if not full and key in DIALOG_KIND and anchor is not None:
                    # dialogs open over the middle of the enchant window
                    roi = ((anchor.cx - 560 * s) * f, (anchor.y + 40 * s) * f,
                           (anchor.cx + 560 * s) * f, (anchor.y + 700 * s) * f)
                m = self._match(gs, key, s * f, roi)
                if m is not None and f != 1:
                    m = Match(key, int(m.x / f), int(m.y / f), int(m.w / f), int(m.h / f), m.score)
            matches[key] = m
        scr = self._classify(g, matches)
        if scr.kind in ("unknown", "dimmed") and not full and self._last:
            return self.detect(frame, full=True)     # moved / unexpected: search everywhere
        for key, m in matches.items():
            if m is not None and m.score >= (T_DIALOG if key in DIALOG_KIND else T_TITLE) + 0.08:
                self._last[key] = m
        return scr

    def _classify(self, g, matches) -> Screen:
        def score(k):
            return matches[k].score if matches[k] else 0.0

        for key, kind in DIALOG_KIND.items():
            if score(key) >= T_DIALOG and score(key) == max(score(k) for k in DIALOG_KIND):
                return Screen(kind, matches)
        for key, kind in (("select_pet_title", "select_pet"), ("enchants_title", "enchants")):
            if score(key) >= T_TITLE and score(key) >= score("enchants_title") - 0.05:
                ratio = self._brightness_ratio(g, matches[key])
                if ratio < DIM_RATIO:
                    return Screen("dimmed", matches, note=f"{kind} behind an unknown popup "
                                                          f"(brightness {ratio:.2f})")
                return Screen(kind, matches)
        return Screen("unknown", matches)

    def _brightness_ratio(self, g, m: Match) -> float:
        region = g[m.y:m.y + m.h, m.x:m.x + m.w]
        tpl = self._tpl(m.key, self.scale, True)
        if region.size == 0:
            return 1.0
        return float(region.mean()) / max(1.0, float(tpl.mean()))

    def _match_hover_aware(self, img, key, roi=None, full=False) -> Optional[Match]:
        """Best match of a button over its normal and its hovered size. The game
        grows a button by ~3-4 % (and lightens it) while the pointer is on it;
        a single-size match then drops from ~0.8 to ~0.6 and the button is
        'not found' (seen in the field on the 2/2 Reroll button).
        Fast: the whole ROI is searched once at normal size (that still finds the
        right SPOT on a hovered button); the other sizes only around that spot."""
        m0 = self._match(img, key, self.scale, roi)
        if m0 is None:
            return None
        best = m0
        if m0.score >= 0.9 and not full:
            return best                             # clearly the normal state: done
        pad = 12 * self.scale
        for f in HOVER_SCALES[1:]:
            th, tw = self._tpl(key, self.scale * f, img.ndim == 2).shape[:2]
            near = (m0.cx - tw / 2 - pad, m0.cy - th / 2 - pad, m0.cx + tw / 2 + pad, m0.cy + th / 2 + pad)
            m = self._match(img, key, self.scale * f, near)
            if m is not None and m.score > best.score:
                best = Match(m.key, m.x, m.y, m.w, m.h, m.score, hovered=f >= HOVER_MIN)
        return best

    def find_dialog_button(self, frame, screen: Screen, key) -> Optional[Match]:
        """Buttons inside a dialog, searched in colour below the dialog title
        (normal or hovered)."""
        title = screen.m("dlg_reroll_title" if screen.kind == "dlg_reroll" else "dlg_crystal_title")
        s = self.scale
        roi = None
        if title is not None and screen.kind in ("dlg_reroll", "dlg_crystal"):
            roi = (title.cx - 380 * s, title.y + title.h, title.cx + 380 * s, title.y + title.h + 420 * s)
        m = self._match_hover_aware(frame, key, roi)
        return m if m and m.score >= T_BUTTON else None

    def hover_state(self, frame, key, xy) -> Optional[bool]:
        """Does the game show button `key` at frame point `xy` as hovered?
        True = hovered (the game has seen the pointer on it), False = visible but
        not hovered, None = button not visible there (closed / covered / moving)."""
        if key not in self.t or self.scale is None:
            return None
        h, w = self.t[key].shape[:2]
        s = self.scale * HOVER_SCALES[-1]
        pad = 14 * self.scale
        roi = (xy[0] - w * s / 2 - pad, xy[1] - h * s / 2 - pad,
               xy[0] + w * s / 2 + pad, xy[1] + h * s / 2 + pad)
        # dialog buttons are told apart by colour (Reroll vs Close); the enchant
        # screen buttons are matched in grey like enchant_buttons() does
        img = frame if key.startswith("btn_") else _gray(frame)
        # full: always compare the sizes - a button that has only just started to
        # grow still matches >= 0.9 at normal size, and that start is what counts
        m = self._match_hover_aware(img, key, roi, full=True)
        if m is None or m.score < T_BUTTON:
            return None
        return bool(m.hovered)

    # -------------------------------------------------------------- enchant slots
    def enchant_buttons(self, frame):
        g = _gray(frame)
        out = []
        for key in ("change_pet", "use_crystal_btn"):
            m = self._match_near(g, key, self.scale, pad_ref=30)
            if m is None or m.score < T_BUTTON:
                m = self._match(g, key, self.scale)
            if m is None or m.score < T_BUTTON:
                # the pointer may be resting on it -> drawn bigger/lighter (hovered)
                last, pad = self._last.get(key), 40 * self.scale
                roi = (last.x - pad, last.y - pad, last.x + last.w + pad, last.y + last.h + pad) \
                    if last is not None else None
                m = self._match_hover_aware(g, key, roi)
            if m is not None and m.score >= T_BUTTON:
                last = self._last.get(key)
                if last is not None and abs(m.cx - last.cx) <= 6 * self.scale \
                        and abs(m.cy - last.cy) <= 6 * self.scale:
                    # same button, maybe drawn hovered (bigger) because the pointer
                    # rests on it: keep its exact normal geometry, so everything
                    # measured from it (slot column, crystal counter) never jitters
                    m = Match(last.key, last.x, last.y, last.w, last.h, m.score, hovered=m.hovered)
                elif not m.hovered:
                    self._last[key] = m
                out.append(m)
            else:
                out.append(None)
        return tuple(out)

    def read_slots(self, frame, cp: Optional[Match] = None, uc: Optional[Match] = None) -> Optional[List[Slot]]:
        """Read the (up to two) enchant slots in the left panel. None = panel not found."""
        s = self.scale
        if cp is None:
            cp, uc = self.enchant_buttons(frame)
        if cp is None:
            return None
        y_end = uc.y if uc is not None else cp.y + 760 * s
        x0, y0, x1, y1 = _clamp_roi((cp.cx - 200 * s, cp.y + 150 * s, cp.cx + 200 * s, y_end), frame.shape)
        roi = frame[y0:y1, x0:x1].astype(np.int16)
        mask = (np.abs(roi - CARD_BG_BGR).max(axis=2) < 30).astype(np.uint8)
        _, _, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=4)
        cards = []
        for x, y, w, h, area in stats[1:]:
            if 215 * s <= w <= 335 * s and 50 * s <= h <= 185 * s and area > 0.25 * w * h:
                cards.append((int(x + x0), int(y + y0), int(w), int(h)))
        cards.sort(key=lambda c: c[1])
        slots = []
        for i, (x, y, w, h) in enumerate(cards[:2]):
            name_box = _clamp_roi((x + 26 * s, y + 1 * s, x + w - 48 * s, y + 38 * s), frame.shape)
            raw, name, score = self._read_name(frame[name_box[1]:name_box[3], name_box[0]:name_box[2]])
            lock, lock_xy = self._lock_state(frame, (x + w - 66 * s, y - 16 * s, x + w + 8 * s, y + 42 * s))
            slots.append(Slot(i, name, raw, score, lock, lock_xy, (x, y, w, h)))
        return slots

    # ------------------------------------------------------------ learned text
    @staticmethod
    def _fingerprint(mask):
        ys, xs = np.nonzero(mask)
        if len(xs) < 20:
            return None
        return mask[ys.min():ys.max() + 1, xs.min():xs.max() + 1] > 0

    @staticmethod
    def _fp_similarity(a, b) -> float:
        if abs(a.shape[0] - b.shape[0]) > 2 or abs(a.shape[1] - b.shape[1]) > 3:
            return 0.0
        h, w = max(a.shape[0], b.shape[0]), max(a.shape[1], b.shape[1])
        A = np.zeros((h, w), bool)
        B = np.zeros((h, w), bool)
        A[:a.shape[0], :a.shape[1]] = a
        B[:b.shape[0], :b.shape[1]] = b
        union = np.count_nonzero(A | B)
        return np.count_nonzero(A & B) / union if union else 0.0

    def _recall(self, kind, fp):
        if fp is None:
            return None
        best, hit = 0.0, None
        for entry in self._learned[kind]:
            sim = self._fp_similarity(fp, entry[0])
            if sim > best:
                best, hit = sim, entry
        return hit if best >= 0.90 else None

    def export_learned(self) -> dict:
        out = {}
        for kind, entries in self._learned.items():
            out[kind] = [{"h": int(fp.shape[0]), "w": int(fp.shape[1]),
                          "bits": base64.b64encode(np.packbits(fp)).decode(),
                          "raw": raw, "name": name, "score": float(score)}
                         for fp, raw, name, score in entries]
        return {"scale": round(float(self.scale or 0), 3), "entries": out}

    def import_learned(self, data) -> int:
        """Load names learned in earlier runs (only valid at the same UI scale)."""
        if not data or self.scale is None or abs(float(data.get("scale", 0)) - self.scale) > 0.01:
            return 0
        n = 0
        for kind, entries in data.get("entries", {}).items():
            if kind not in self._learned:
                continue
            for e in entries:
                if e.get("name") not in self.names:
                    continue
                bits = np.unpackbits(np.frombuffer(base64.b64decode(e["bits"]), np.uint8))
                fp = bits[:e["h"] * e["w"]].reshape(e["h"], e["w"]).astype(bool)
                self._learned[kind].append((fp, e["raw"], e["name"], float(e["score"])))
                n += 1
        return n

    def _learn(self, kind, fp, raw, name, score):
        if fp is None or not name or score < 0.86:
            return
        store = self._learned[kind]
        store.append((fp, raw, name, score))
        if len(store) > 80:
            del store[0]

    def _read_name(self, crop):
        if crop.size == 0:
            return "", None, 0.0
        mask = ocr.text_mask_on_card(crop)
        fp = self._fingerprint(mask)
        hit = self._recall("slot", fp)
        if hit is not None:
            return hit[1], hit[2], hit[3]
        readings = []
        for kw in ({}, {"vmin": 150, "dmin": 60}, {"erode": True}, {"vmin": 190, "dmin": 80}):
            img = ocr._finish(mask) if not kw else ocr.prep_text_on_card(crop, **kw)
            raw = ocr.tesseract(img, psm=7)
            self.ocr_calls += 1
            name, score = ocr.match_name(raw, self.names)
            readings.append((raw, name, score))
            # secret enchants have no roman numeral -> one confident read is enough;
            # the same for numeral names whose exact numeral does not matter here
            if name and score >= 0.86 and (not ocr.has_numeral(name) or (
                    self.exact_numerals is not None and ocr.family(name) not in self.exact_numerals)):
                self._learn("slot", fp, raw, name, score)
                return raw, name, score
        result = ocr.vote(readings)
        self._learn("slot", fp, *result)
        return result

    def _lock_state(self, frame, region):
        x0, y0, x1, y1 = _clamp_roi(region, frame.shape)
        c = frame[y0:y1, x0:x1].astype(np.int16)
        if c.size == 0:
            return None, None
        b, g, r = c[..., 0], c[..., 1], c[..., 2]
        gold = (r > 200) & (g > 120) & (g < 228) & (b < 125)
        grey = (np.abs(r - g) < 14) & (np.abs(g - b) < 14) & (r > 130) & (r < 225)
        thr = 45 * self.scale * self.scale
        ng, ny = int(gold.sum()), int(grey.sum())

        def centre(mask):
            ys, xs = np.nonzero(mask)
            return int(xs.mean()) + x0, int(ys.mean()) + y0

        if ng > thr and ng >= ny:
            return LOCKED, centre(gold)
        if ny > thr:
            return UNLOCKED, centre(grey)
        return None, None

    # ------------------------------------------------------------- 2/2 dialog
    def read_dialog_name(self, frame, screen: Screen):
        """Returns (raw_text, enchant_name_or_None, score) from the
        'Are you sure you want to reroll your X Enchant?' dialog."""
        m = screen.m("dlg_reroll_title")
        if m is None:
            return "", None, 0.0
        s = self.scale
        x0, y0, x1, y1 = _clamp_roi((m.cx - 320 * s, m.y + m.h + 25 * s,
                                     m.cx + 320 * s, m.y + m.h + 155 * s), frame.shape)
        crop = frame[y0:y1, x0:x1]
        if crop.size == 0:
            return "", None, 0.0
        mask = ocr.saturated_mask(crop, s)
        fp = self._fingerprint(mask)
        hit = self._recall("dialog", fp)
        if hit is not None:
            return hit[1], hit[2], hit[3]
        prepped = ocr._finish(mask)
        readings = []
        for psm in (6, 7, 11):
            raw = ocr.tesseract(prepped, psm=psm)
            self.ocr_calls += 1
            name, score = ocr.match_name(raw, self.names)
            readings.append((raw, name, score))
            if name and score >= 0.86:
                self._learn("dialog", fp, raw, name, score)
                return raw, name, score
        result = ocr.vote(readings)
        self._learn("dialog", fp, *result)
        return result

    # ------------------------------------------------------------ change signals
    def result_signature(self, frame):
        """Small grey crops of (slot column, crystal counter). When a crystal is
        used the counter ticks down even if the same enchant is rolled again."""
        cp, uc = self.enchant_buttons(frame)
        if cp is None or uc is None:
            return None
        s = self.scale
        g = _gray(frame)
        x0, y0, x1, y1 = _clamp_roi((cp.cx - 200 * s, cp.y + 150 * s, cp.cx + 200 * s, uc.y), g.shape)
        col = cv2.resize(g[y0:y1, x0:x1], None, fx=0.25, fy=0.25, interpolation=cv2.INTER_AREA)
        x0, y0, x1, y1 = _clamp_roi((uc.x + 330 * s, uc.y - 26 * s, uc.x + 560 * s, uc.y + 30 * s), g.shape)
        cnt = g[y0:y1, x0:x1].astype(np.float32)
        return col.astype(np.float32), cnt

    @staticmethod
    def _corr(a, b) -> float:
        if a is None or b is None or a.shape != b.shape or a.size == 0:
            return 0.0
        a = a - a.mean()
        b = b - b.mean()
        d = float(np.sqrt((a * a).sum() * (b * b).sum()))
        return float((a * b).sum()) / d if d > 1e-6 else 1.0

    def is_clear(self, frame, screen: Screen) -> bool:
        """Enchants screen fully visible: no dialog overlay left, not even fading out."""
        m = screen.m("enchants_title")
        return m is not None and self._brightness_ratio(_gray(frame), m) >= 0.93

    # brightness-independent, so the dim behind a dialog doesn't count as a change
    def counter_changed(self, before, after) -> bool:
        if before is None or after is None:
            return False
        if self._corr(before, after) < 0.975:
            return True
        # One digit changing (2,363 -> 2,362) moves the whole-crop score only to
        # ~0.97-0.98 and was missed in the field (1.2 s fallback wait). Compared
        # digit by digit it drops to 0.5-0.8. Only the digits (right of the icon).
        w = before.shape[1]
        x0, n = int(w * 0.38), 6
        step = (w - x0) / n
        for i in range(n):
            a, b = int(x0 + i * step), int(x0 + (i + 1) * step)
            if self._corr(before[:, a:b], after[:, a:b]) < 0.9:
                return True
        return False

    def column_changed(self, before, after) -> bool:
        return before is not None and after is not None and self._corr(before, after) < 0.95

    # --------------------------------------------------------------- pet grid
    def find_pets(self, frame, screen: Screen) -> List[PetCard]:
        """Gold-framed cards in the 'Currently Equipped' section, row by row.
        Smaller cards (Super Legendary) are returned with small=True."""
        m = screen.m("select_pet_title")
        if m is None:
            return []
        s = self.scale
        x0, y0, x1, y1 = _clamp_roi((m.cx - 540 * s, m.y + m.h + 15 * s,
                                     m.cx + 540 * s, m.y + m.h + 760 * s), frame.shape)
        roi = frame[y0:y1, x0:x1]
        size = PET_CARD * s
        # The equipped cards share one frame colour, but the game animates it
        # (gold <-> orange ...). Use the most common saturated colours as candidates
        # and keep the one that produces the most card-shaped rings.
        best = []
        for colour in self._frame_colours(roi):
            cards = self._rings(roi, colour, size, x0, y0)
            if sum(not p.small for p in cards) > sum(not p.small for p in best):
                best = cards
        cards = sorted(best, key=lambda p: (p.y, p.x))
        rows, row_y = [], None
        for p in cards:                      # group into rows, then left-to-right
            if row_y is None or p.y - row_y > 40 * s:
                rows.append([])
                row_y = p.y
            rows[-1].append(p)
        return [p for row in rows for p in sorted(row, key=lambda p: p.x)]

    @staticmethod
    def _frame_colours(roi, n=4):
        hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)
        sat = (hsv[..., 1] > 150) & (hsv[..., 2] > 150)
        px = roi[sat]
        if len(px) == 0:
            return []
        q = (px // 8).astype(np.int32)
        keys = q[:, 0] * 1024 + q[:, 1] * 32 + q[:, 2]
        vals, counts = np.unique(keys, return_counts=True)
        out = []
        for k in vals[np.argsort(counts)[::-1][:n]]:
            out.append(np.array([k // 1024, (k // 32) % 32, k % 32], np.int16) * 8 + 4)
        return out

    @staticmethod
    def _rings(roi, colour, size, ox, oy):
        mask = (np.abs(roi.astype(np.int16) - colour).max(axis=2) <= 22).astype(np.uint8)
        _, _, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
        cards = []
        for x, y, w, h, area in stats[1:]:
            if abs(w - h) > 0.18 * size or area < 0.08 * w * h:
                continue
            if 0.86 * size <= w <= 1.15 * size and 0.86 * size <= h <= 1.15 * size:
                cards.append(PetCard(int(x + ox), int(y + oy), int(w), int(h), small=False))
            elif 0.55 * size <= w < 0.86 * size and 0.55 * size <= h < 0.86 * size:
                cards.append(PetCard(int(x + ox), int(y + oy), int(w), int(h), small=True))
        return cards

    # ----------------------------------------------------------------- debug
    def annotate(self, frame, screen: Optional[Screen] = None, slots=None, pets=None, extra=None):
        img = frame.copy()
        red, green, yellow, cyan = (0, 0, 255), (0, 200, 0), (0, 220, 255), (255, 255, 0)

        def text(t, x, y, col):
            cv2.putText(img, t, (int(x), int(y)), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 0, 0), 3)
            cv2.putText(img, t, (int(x), int(y)), cv2.FONT_HERSHEY_SIMPLEX, 0.55, col, 1)

        if screen is not None:
            for k, m in screen.matches.items():
                if m and m.score >= 0.5:
                    cv2.rectangle(img, (m.x, m.y), (m.x + m.w, m.y + m.h), cyan, 1)
                    text(f"{k} {m.score:.2f}", m.x, m.y - 4, cyan)
            text(f"screen: {screen.kind}  scale {self.scale:.3f}", 10, 22, yellow)
        for s in slots or []:
            x, y, w, h = s.rect
            cv2.rectangle(img, (x, y), (x + w, y + h), green, 2)
            text(f"#{s.index + 1} {s.describe()} ({s.score:.2f})", x, y + h + 16, green)
            if s.lock_xy:
                cv2.circle(img, s.lock_xy, 8, red if s.lock == LOCKED else yellow, 2)
        for i, p in enumerate(pets or []):
            col = red if p.small else green
            cv2.rectangle(img, (p.x, p.y), (p.x + p.w, p.y + p.h), col, 2)
            text("SKIP" if p.small else str(i + 1), p.x + 4, p.y + 18, col)
        for m in extra or []:
            if m:
                cv2.rectangle(img, (m.x, m.y), (m.x + m.w, m.y + m.h), yellow, 2)
                text(f"{m.key} {m.score:.2f}", m.x, m.y + m.h + 14, yellow)
        return img
