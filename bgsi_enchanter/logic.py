"""Pure decision logic: what to lock, when a pet is finished, how to answer the
"reroll your X enchant?" confirmation. No I/O here, so it is unit-testable."""
from collections import Counter
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

LOCKED = "locked"
UNLOCKED = "unlocked"


@dataclass
class Slot:
    index: int                      # 0 = top slot, 1 = bottom slot
    name: Optional[str]             # canonical enchant name, None if unreadable
    raw: str = ""                   # raw OCR text
    score: float = 0.0              # OCR match confidence 0..1
    lock: Optional[str] = None      # LOCKED / UNLOCKED / None (no lock icon shown)
    lock_xy: Optional[Tuple[int, int]] = None   # lock icon centre (frame px)
    rect: Optional[Tuple[int, int, int, int]] = None  # card x, y, w, h (frame px)

    def describe(self) -> str:
        name = self.name or f"?({self.raw})"
        tag = {LOCKED: " [LOCKED]", UNLOCKED: " [unlocked]"}.get(self.lock, "")
        return f"{name}{tag}"


@dataclass
class Plan:
    done: bool
    keep: List[int] = field(default_factory=list)       # slots that satisfy a target
    to_lock: List[int] = field(default_factory=list)
    to_unlock: List[int] = field(default_factory=list)
    stuck: bool = False
    reason: str = ""


def _clean(targets) -> List[str]:
    return [t for t in targets if t][:2]


def evaluate(slots: List[Slot], targets, order_matters=False, unlock_unwanted=True) -> Plan:
    targets = _clean(targets)
    by_index = {s.index: s for s in slots}
    if not targets:
        return Plan(done=True, reason="no targets configured")

    keep = {}
    if order_matters:
        for i, t in enumerate(targets):
            s = by_index.get(i)
            if s is not None and s.name == t:
                keep[i] = t
    else:
        need = Counter(targets)
        # already-locked slots first, so a locked copy is preferred over an unlocked one
        for s in sorted(slots, key=lambda s: (s.lock != LOCKED, s.index)):
            if s.name and need[s.name] > 0:
                keep[s.index] = s.name
                need[s.name] -= 1

    if len(keep) == len(targets):
        # Finished. Locks are only a tool for rerolling the other slot, so nothing
        # is locked (or unlocked) here - just move on to the next pet.
        return Plan(done=True, keep=sorted(keep), reason="target combination present")

    # The game allows only ONE locked enchant per pet. It is used to protect a
    # wanted enchant while the other slot is rerolled.
    to_unlock = []
    if unlock_unwanted:
        to_unlock = [s.index for s in slots if s.lock == LOCKED and s.index not in keep]
    still_locked = [s for s in slots if s.lock == LOCKED and s.index not in to_unlock]
    to_lock = []
    if not to_unlock and not still_locked:
        to_lock = [i for i in sorted(keep) if by_index[i].lock == UNLOCKED][:1]

    stuck, reason = False, ""
    rerollable = [s for s in slots
                  if s.index not in keep and (s.lock != LOCKED or s.index in to_unlock)]
    free_capacity = len(slots) < 2      # "(empty slot)" can still be filled
    if not rerollable and not free_capacity:
        stuck = True
        reason = "the only slot that could be rerolled is locked " \
                 "(enable 'unlock unwanted' or unlock it manually)"
    return Plan(done=False, keep=sorted(keep), to_lock=to_lock, to_unlock=to_unlock,
                stuck=stuck, reason=reason)


def decide_dialog(named: Optional[str], slots: List[Slot], targets, order_matters=False):
    """The 2/2 'Are you sure you want to reroll your <named> Enchant?' dialog.
    Returns ("reroll" | "close", reason)."""
    targets = _clean(targets)
    if not named:
        # The dialog can only name an enchant the crystal rerolls (an unlocked one).
        # If none of those is wanted (and all were read), the name doesn't matter.
        # (Seen in the field: 'Bubbler I | Magnetism', dialog name unreadable.)
        rerolled = [s for s in slots if s.lock != LOCKED]
        if rerolled and all(s.name and s.name not in targets for s in rerolled):
            return "reroll", ("could not read the name in the dialog, but no enchant that gets "
                              f"rerolled is wanted ({', '.join(s.name for s in rerolled)})")
        return "close", "could not read the enchant name in the dialog - playing safe"
    if named not in targets:
        return "reroll", f"{named} is not one of the targets"

    lockable = any(s.lock is not None for s in slots)
    if not lockable:
        return "reroll", f"{named} is the only enchant on the pet and cannot be locked"

    plan = evaluate(slots, targets, order_matters, unlock_unwanted=False)
    matching = [s for s in slots if s.name == named]
    for s in matching:
        if s.index in plan.keep and s.lock != LOCKED:
            return "close", f"{named} is wanted but not locked yet - locking it first"
    if not matching:
        locked_have = sum(1 for s in slots if s.lock == LOCKED and s.name == named)
        if locked_have < targets.count(named):
            return "close", f"dialog names wanted {named} but the slot reader missed it"
    return "reroll", f"{named} is an extra copy / not in a wanted position"
