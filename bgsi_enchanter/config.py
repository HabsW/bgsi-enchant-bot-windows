"""Settings, persisted to config.json next to the package."""
import copy
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config.json"
TEMPLATE_DIR = ROOT / "templates"
DEBUG_DIR = ROOT / "debug"

SECRET_ENCHANTS = ["Ultra Roller", "Secret Hunter", "Shiny Seeker", "Determination"]

# Used only to recognise (and log) what is in a slot. Anything you type as a target
# is added automatically, so this list does not have to be complete.
OTHER_ENCHANTS = (
    [f"Bubbler {r}" for r in ("I", "II", "III", "IV", "V")]
    + [f"Looter {r}" for r in ("I", "II", "III", "IV", "V")]
    + [f"Gleaming {r}" for r in ("I", "II", "III")]
    + [f"Team Up {r}" for r in ("I", "II", "III", "IV", "V")]
    + ["Infinity", "Magnetism"]
)

DEFAULTS = {
    # What every eligible pet should end up with: 1 or 2 different names
    # (a pet can only have one of each enchant).
    "targets": ["Ultra Roller", "Secret Hunter"],
    # false: the two targets may be in either slot.  true: targets[0] must be the
    # top slot and targets[1] the bottom slot.
    "order_matters": False,
    # Unlock locked enchants that are not targets (otherwise they block rerolling).
    "unlock_unwanted": True,
    # Re-enchant every pet once per run, even if it already has the combination:
    # unlock everything, reroll all slots with one crystal, then roll the
    # combination again. Old enchants can be worn out (e.g. 100k of 1.5M left)
    # while the enchant book still shows them at full, so the bot can't tell.
    "reenchant_once": True,
    # Safety limit: stop after this many Shadow Crystals in total (0 = unlimited).
    "max_crystals_total": 600,
    # On any error: wait error_retry_wait seconds, look again and carry on - up to
    # error_retries times in a row (4 x 0.25 s = 1 s) before stopping.
    "error_retries": 4,
    "error_retry_wait": 0.25,
    # 1-based positions in the "Currently Equipped" grid to process. Empty = all.
    "only_pets": [],
    # Extra passes over all pets to verify / fix (pets can be re-sorted by the game).
    "passes": 3,
    # Kill switch (F1-F12, ESCAPE, PAUSE, END, HOME) - works while Roblox has focus.
    "stop_hotkey": "F8",
    # Stop as soon as the real mouse is moved/clicked (the bot owns the pointer).
    "stop_on_mouse_move": True,
    # Roblox window: entries ending in .exe match the process (also when started via
    # Bloxstrap/Fishstrap), others must equal the window title (Microsoft Store app).
    "window_match": ["RobloxPlayerBeta.exe", "Roblox"],
    # Full path to tesseract.exe if it is not found automatically.
    "tesseract_path": "",
    # Timings (seconds). Roblox on Windows reads the cursor once per frame, so a
    # click must span several frames: hover -> press -> hold -> release -> stay.
    "click_hover": 0.05,          # pointer rests on a button this long before pressing
    "click_hover_resting": 0.03,  # ... when it was already waiting on that spot
    "click_hold": 0.04,           # button held down this long
    "click_release_hold": 0.04,   # stay on the button this long after releasing
    # Before pressing a button the bot waits until the game draws it hovered (=
    # Roblox has really seen the pointer there), wiggling the pointer every
    # hover_nudge_every s; after hover_timeout s it presses anyway.
    "hover_timeout": 0.35,
    "hover_nudge_every": 0.05,
    "after_click": 0.0,           # the bot polls the screen right after anyway
    # FIRST time a dialog/window is seen it must be unmoved this long (real time)
    # before its buttons are clicked; its resting place is then remembered...
    "settle_time": 0.12,
    # ...and later it is clicked this long after it ARRIVES at that place.
    "arrive_grace": 0.06,
    # Press a dialog button while it is still sliding in (once it is safely under
    # the pointer). Switches itself off for the run if the game ignores one.
    "early_press": True,
    # the slot cards must be unchanged this long before they are read (animations)
    "stable_time": 0.05,
    # no reaction to a click after this long -> check the screen and click again,
    # up to click_retries times (each missed click costs at most ~1 s, never more)
    "react_timeout": 1.0,
    "click_retries": 3,
    # a dialog still fully open this long after its button was clicked = missed click
    "retry_after": 0.8,
    # the 2/2 dialog is expected but has not appeared after this long -> carry on
    "expect_dialog_wait": 2.0,
    # max wait for a clicked lock icon to change before clicking it again
    "lock_timeout": 1.5,
    # A reroll is detected instantly (crystal counter / slot change). These are only
    # fallbacks for when nothing visibly changes (e.g. the same enchant rolled again).
    "result_fallback": 1.2,
    "result_wait": 0.45,      # used only if the crystal counter can't be found
    "dialog_timeout": 4.0,
    "screen_timeout": 6.0,
    # Save annotated screenshots to debug/ when something unexpected happens.
    "save_debug_images": True,
    # debug/ only ever keeps this many (newest) error screenshots.
    "keep_error_screenshots": 10,
    # Extra enchant names for recognition (optional).
    "extra_enchant_names": [],
}


# Bump when timing defaults change: an older config.json keeps the user's own
# choices (USER_KEYS) but gets the new timings.
CONFIG_VERSION = 4
DEFAULTS["config_version"] = CONFIG_VERSION
USER_KEYS = {"targets", "order_matters", "unlock_unwanted", "reenchant_once", "max_crystals_total", "only_pets",
             "passes", "stop_hotkey", "stop_on_mouse_move", "window_match", "tesseract_path",
             "error_retries", "error_retry_wait", "save_debug_images", "keep_error_screenshots",
             "extra_enchant_names"}


def load_config(path: Path = CONFIG_PATH) -> dict:
    cfg = copy.deepcopy(DEFAULTS)
    if path.exists():
        try:
            user = json.loads(path.read_text())
            keep = DEFAULTS if user.get("config_version", 1) >= CONFIG_VERSION else USER_KEYS
            cfg.update({k: v for k, v in user.items() if k in keep and k != "config_version"})
        except (OSError, json.JSONDecodeError, AttributeError) as exc:
            print(f"[config] could not read {path}: {exc} - using defaults")
    cfg["targets"] = clean_targets(cfg["targets"])
    return cfg


def clean_targets(targets) -> list:
    """Strip, drop empties and duplicates (a pet can only have one of each enchant), max 2."""
    out = []
    for t in targets:
        t = (t or "").strip()
        if t and t.lower() not in (o.lower() for o in out):
            out.append(t)
    return out[:2]


def save_config(cfg: dict, path: Path = CONFIG_PATH) -> None:
    path.write_text(json.dumps({k: cfg[k] for k in DEFAULTS}, indent=2) + "\n")


def known_names(cfg: dict) -> list:
    names = list(SECRET_ENCHANTS) + OTHER_ENCHANTS + list(cfg.get("extra_enchant_names", []))
    for t in cfg.get("targets", []):
        if t and t not in names:
            names.append(t)
    return names
