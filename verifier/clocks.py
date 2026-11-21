"""Estimate a clock offset for every device from events seen by more than one source.

offset(D) = D's clock reading - true time.   true = t - offset
The reference device (the dispatch/CAD export) defines "true" time.

An anchor is an event kind that appears exactly once in both devices
(e.g. the single 911 call in dispatch and the single "caller" note).
A device with no anchor, or with anchors that disagree, is left unaligned:
its timestamps are then never used to decide a match or conflict.
"""
from collections import defaultdict
from dataclasses import dataclass, field

LOG_ANCHORS = ("call911", "dispatched", "on_scene", "stop")
REFERENCE_KINDS = {"call911", "dispatched", "on_scene"}
MAX_OFFSET = {"camera": 15 * 60, "log": 6 * 3600}


@dataclass
class Clock:
    offset: float
    unc: float                      # +/- seconds on the offset itself
    anchor_kinds: set = field(default_factory=set)
    note: str = ""


def _usable(r):
    return r.t is not None and not r.suspicious


def pick_reference(by_dev):
    def score(dev):
        rows = by_dev[dev]
        strong = sum(1 for r in rows if _usable(r) and not r.relative and r.prec <= 1 and r.kinds & REFERENCE_KINDS)
        name = dev.lower()
        return (strong, "dispatch" in name or "cad" in name, sum(1 for r in rows if _usable(r) and not r.relative))
    cands = [d for d, rows in by_dev.items() if rows[0].source != "camera"] or list(by_dev)
    best = max(cands, key=score)
    return best if score(best)[2] > 0 else None


def _unique(rows, kind):
    hits = [r for r in rows if kind in r.kinds and _usable(r)]
    return hits[0] if len(hits) == 1 else None


def align(rows, log=lambda *_: None):
    by_dev = defaultdict(list)
    for r in rows:
        by_dev[r.device].append(r)
    ref = pick_reference(by_dev)
    clocks = {}
    if ref is None:
        log("no device with absolute timestamps; nothing aligned")
        return clocks, None
    clocks[ref] = Clock(0.0, 0.0, note="reference clock")
    log(f"reference clock: {ref}")

    def attempt(dev):
        source = by_dev[dev][0].source
        kinds = ("stop",) if source == "camera" else LOG_ANCHORS
        if source == "speech":
            kinds = ("stop",)
        estimates = []
        for other, clk in clocks.items():
            if by_dev[other][0].source == "camera":
                continue  # a camera's stopped car may not be our car: never chain through cameras
            for k in kinds:
                a, b = _unique(by_dev[dev], k), _unique(by_dev[other], k)
                if not a or not b:
                    continue
                if k == "stop" and "stop" in clk.anchor_kinds:
                    continue  # that clock was itself set by the stop: no new information
                if k == "stop" and b.source == "camera":
                    continue
                true_b = b.t - clk.offset
                off = a.t - true_b
                unc = a.prec / 2 + b.prec / 2 + clk.unc
                estimates.append((off, unc, k, f"{k}: {a.file} row {a.row} vs {b.file} row {b.row}"))
        if not estimates:
            return None
        offs = [e[0] for e in estimates]
        spread = max(offs) - min(offs)
        tol = 2 * max(e[1] for e in estimates) + 2
        if spread > tol:
            log(f"{dev}: anchors disagree by {spread:.0f}s (> {tol:.0f}s); left unaligned")
            return "bad"
        off = sum(offs) / len(offs)
        if not by_dev[dev][0].relative and abs(off) > MAX_OFFSET.get(source, MAX_OFFSET["log"]):
            log(f"{dev}: implausible offset {off:+.0f}s; left unaligned")
            return "bad"
        return Clock(off, max(e[1] for e in estimates), {e[2] for e in estimates},
                     "; ".join(e[3] for e in estimates))

    # logs and speech first, cameras last (cameras only via a non-camera stop)
    order = sorted((d for d in by_dev if d != ref), key=lambda d: by_dev[d][0].source == "camera")
    rejected = set()
    changed = True
    while changed:
        changed = False
        for dev in order:
            if dev in clocks or dev in rejected:
                continue
            res = attempt(dev)
            if res == "bad":
                rejected.add(dev)
            elif res:
                clocks[dev] = res
                changed = True
                rel = " (relative clock)" if by_dev[dev][0].relative else ""
                log(f"aligned {dev}{rel}: offset {res.offset:+.1f}s +/-{res.unc:.0f}s via {res.note}")
    for dev in by_dev:
        if dev not in clocks and dev not in rejected:
            log(f"{dev}: no shared event with an aligned clock; left unaligned")
    return clocks, ref


def true_time(r, clocks):
    """(true_seconds, uncertainty) or None when the row's clock is not aligned."""
    clk = clocks.get(r.device)
    if clk is None or r.t is None:
        return None
    return r.t - clk.offset, clk.unc + r.prec / 2
