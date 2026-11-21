"""Split the report into claims, extract the checkable facts, and decide each one.

Verdict thresholds (seconds) are deliberately asymmetric: a match needs the
whole uncertainty interval inside the tolerance, a conflict needs the whole
interval outside a wider band. Anything in between is "not_settled".
"""
import re
from datetime import datetime

from .clocks import true_time
from .loaders import KIND_PATTERNS, Row

HEDGE = r"(?:about|around|approximately|approx\.?|roughly|circa|some|~)"
TIME_FACT = re.compile(rf"(?:({HEDGE})\s*)?\b(\d{{1,2}}):(\d{{2}})\b(?:\s*([ap])\.?\s?m\b\.?)?", re.I)
DURATION_FACT = re.compile(
    rf"(?:({HEDGE})\s+)?(\d+(?:\.\d+)?)\s*(minutes?|mins?|seconds?|secs?)\s+(after|later than|before)\s+(?:the\s+)?([a-z0-9_ ]+?)\b(?:\.|,|$|\s)",
    re.I)
SPEED_FACT = re.compile(r"\b(\d{2,3})\s*mph\b(?!\s*zone)", re.I)
ZONE_FACT = re.compile(r"\bposted\s+(?:speed\s+)?(?:limit\s+)?(?:of\s+)?(\d{2,3})|\b(\d{2,3})\s*(?:mph\s+)?zone\b|\blimit\s+(?:of\s+)?(\d{2,3})", re.I)
LOCATION_FACT = re.compile(r"\s*\b(?:near|at|by|around)\s+(?:mile\s*marker|milepost|mm)\s*\d+(?:\.\d+)?", re.I)
COUNT_FACT = re.compile(r"\bonly (?:car|vehicle)s?\b|\bno other (?:cars?|vehicles?)\b|\b(?:\d+|two|three|four|several) (?:cars|vehicles)\b|\blane\b", re.I)
CLAIM_KIND_ORDER = ("call911", "on_scene", "dispatched", "stop", "observe")
REFERENCED_KIND = [
    (re.compile(r"stop|pull", re.I), "stop"),
    (re.compile(r"911|call", re.I), "call911"),
    (re.compile(r"dispatch", re.I), "dispatched"),
    (re.compile(r"arriv|scene", re.I), "on_scene"),
    (re.compile(r"observ|sighting", re.I), "observe"),
]
HEADER_SENTENCE = re.compile(r"SYNTHETIC|FICTIONAL|^\s*(?:Incident|Case|Report)\b[^.]*\d{4}-\d{2}-\d{2}", re.I)

# tolerances
EXACT_MATCH = (-30, 60)       # "21:11" means 21:10:30 (rounded) .. 21:11:59 (truncated)
EXACT_CONFLICT = (-90, 120)   # one more minute of slack before calling it wrong
HEDGED_MATCH = 150            # "about 21:14": within 2.5 min
HEDGED_CONFLICT = 300         # more than 5 min away is a real disagreement


# ---------------------------------------------------------------- claims
class Claim:
    def __init__(self, text, sentence_idx):
        self.text, self.sentence_idx = text, sentence_idx


ABBREV = re.compile(r"\b(approx|approx|appx|ca|no|mr|mrs|ms|dr|st|ave|rd|hwy|vs|est)\.(?=\s)|\b([ap])\.m\.(?=\s+[a-z0-9])", re.I)


def _has_fact(text):
    return bool(TIME_FACT.search(text) or DURATION_FACT.search(text) or SPEED_FACT.search(text)
                or LOCATION_FACT.search(text) or COUNT_FACT.search(text))


def split_claims(report):
    flat = re.sub(r"\s+", " ", report).strip()
    # hide abbreviation dots so "approx. 2 minutes" is not a sentence end (same length: offsets stay valid)
    masked = ABBREV.sub(lambda m: m.group(0).replace(".", "\x00"), flat)
    claims, pos = [], 0
    for si, sm in enumerate(re.split(r"(?<=[.!?])\s+(?=[A-Z0-9\"'])", masked)):
        start = flat.index(sm.replace("\x00", "."), pos)
        pos = start + len(sm)
        s = flat[start:pos]
        if HEADER_SENTENCE.search(s):
            continue
        s = s.rstrip(".!? ")
        # clause spans split on ", "; a clause with no checkable fact is glued to its neighbour,
        # so "At 21:14, Officer Lee observed the car" stays one claim
        spans, i = [], 0
        for m in re.finditer(r",\s+", s):
            spans.append([i, m.start()])
            i = m.end()
        spans.append([i, len(s)])
        merged = []
        for sp in spans:
            if merged and (not _has_fact(s[sp[0]:sp[1]]) or not _has_fact(s[merged[-1][0]:merged[-1][1]])):
                merged[-1][1] = sp[1]
            else:
                merged.append(sp)
        for a, b in merged:
            clause = s[a:b]
            loc = LOCATION_FACT.search(clause)
            parts = [clause]
            if loc and _has_fact(clause[:loc.start()] + clause[loc.end():]):
                parts = [clause[:loc.start()], clause[loc.start():loc.end()].strip(), clause[loc.end():]]
            for p in parts:
                p = p.strip(" ,")
                if p and (len(p.split()) >= 3 or LOCATION_FACT.fullmatch(" " + p)):
                    claims.append(Claim(p, si))
    return claims


def claim_kind(text, exclude=None):
    for k in CLAIM_KIND_ORDER:
        if k == exclude:
            continue
        pat = KIND_PATTERNS[k]
        if k == "stop":
            pat = re.compile(r"\bstopp?ed\b|\bpull(?:ed|ing)? (?:in|over|into)\b|\bparked\b", re.I)
        if pat.search(text):
            return k
    return None


# ---------------------------------------------------------------- evidence helpers
def _quote_for(r: Row, prefer=None):
    if prefer and prefer in r.raw:
        return prefer
    if r.file.lower().endswith(".json"):
        return r.time_field_raw or r.raw.strip()
    line = next((ln for ln in r.raw.splitlines() if ln.strip()), r.raw)
    return line.strip()


class Evidence:
    def __init__(self, verdict, row=None, quote=None, why=""):
        self.verdict, self.row, self.quote, self.why = verdict, row, quote, why


def _candidates(rows, clocks, kind, circular_ok=False):
    """Rows recording `kind`, on an aligned clock, excluding cameras for vehicle events
    and excluding rows whose own clock was set from this very event (circular)."""
    out = []
    for r in rows:
        if kind not in r.kinds or r.suspicious or r.t is None:
            continue
        if r.source == "camera":
            continue  # a camera row has no vehicle identity
        clk = clocks.get(r.device)
        if clk is None:
            continue
        if not circular_ok and kind in clk.anchor_kinds:
            continue
        tt = true_time(r, clocks)
        out.append((r, tt[0], tt[1]))
    return out


def _exists(rows, kind):
    return any(kind in r.kinds and not r.suspicious for r in rows if r.source != "camera")


def _collapse(cands):
    """Several rows for one event: keep the most precise if they all agree, else None."""
    lo = max(t - u for _, t, u in cands)
    hi = min(t + u for _, t, u in cands)
    if lo > hi + 1:
        return None
    return [min(cands, key=lambda c: c[2])]


def judge_time(kind, claimed, hedged, rows, clocks, ctx, first=False):
    cands = _candidates(rows, clocks, kind)
    if not cands:
        if _exists(rows, kind):
            return Evidence("not_settled", why=f"{kind} rows exist but none on an independently aligned clock")
        return Evidence("not_checkable", why=f"no record of a {kind} event")
    if first:
        cands = [min(cands, key=lambda c: c[1])]
    verdicts = []
    for r, t, u in cands:
        d = t - claimed
        if hedged:
            v = "match" if abs(d) + u <= HEDGED_MATCH else "conflict" if abs(d) - u > HEDGED_CONFLICT else "not_settled"
        else:
            if d - u >= EXACT_MATCH[0] and d + u <= EXACT_MATCH[1]:
                v = "match"
            elif d + u < EXACT_CONFLICT[0] or d - u > EXACT_CONFLICT[1]:
                v = "conflict"
            else:
                v = "not_settled"
        verdicts.append((v, r, d, u))
    desc = "; ".join(f"{r.file}#{r.row} {v} (diff {d:+.0f}s +/-{u:.0f}s)" for v, r, d, u in verdicts)
    kinds = {v for v, *_ in verdicts}
    if kinds == {"conflict"} or kinds == {"match"} or ("match" in kinds and "conflict" not in kinds):
        target = "match" if "match" in kinds else "conflict"
        best = min((x for x in verdicts if x[0] == target), key=lambda x: x[3])
        return Evidence(target, best[1], _quote_for(best[1]), desc)
    return Evidence("not_settled", why=desc)


def judge_duration(kind_a, kind_b, claimed, hedged, rows, clocks):
    a = _candidates(rows, clocks, kind_a)
    b = _candidates(rows, clocks, kind_b)
    if not a or not b:
        exists = _exists(rows, kind_a) and _exists(rows, kind_b)
        return Evidence("not_settled" if exists else "not_checkable",
                        why=f"need aligned {kind_a} and {kind_b} rows (have {len(a)}, {len(b)})")
    a, b = _collapse(a), _collapse(b)
    if a is None or b is None:
        return Evidence("not_settled", why=f"records disagree about when {kind_a} or {kind_b} happened")
    (ra, ta, ua), (rb, tb, ub) = a[0], b[0]
    measured, u = ta - tb, ua + ub
    tol = max(90, 0.25 * claimed) if hedged else max(30, 0.1 * claimed)
    diff = measured - claimed
    why = f"{ra.file}#{ra.row} - {rb.file}#{rb.row} = {measured:.0f}s vs claimed {claimed:.0f}s (+/-{u:.0f}s, tol {tol:.0f}s)"
    if abs(diff) + u <= tol:
        return Evidence("match", ra, _quote_for(ra), why)
    if abs(diff) - u > 2 * tol:
        return Evidence("conflict", ra, _quote_for(ra), why)
    return Evidence("not_settled", why=why)


def judge_speed(speed, zone, rows, clocks, context_time):
    statements = [r for r in rows if r.source != "camera" and not r.suspicious and r.speed is not None]
    if statements:
        speeds = {r.speed for r in statements}
        if speeds == {speed}:
            for r in statements:
                if zone is not None and r.zone is not None and r.zone != zone:
                    return Evidence("conflict", r, _quote_for(r), f"speed agrees but zone {r.zone} != {zone}")
            r = next((s for s in statements if zone is None or s.zone == zone), statements[0])
            m = re.search(rf"\b{speed}\b[^\n]*?\b{zone}\b", r.raw) if zone is not None else re.search(rf"\b{speed}\b\s*(?:mph)?", r.raw)
            return Evidence("match", r, _quote_for(r, m.group(0) if m else None), "speed stated in record")
        if len(statements) == 1 and speed not in speeds:
            r = statements[0]
            if abs(r.speed - speed) >= 3:
                m = re.search(rf"\b{r.speed}\b[^\n]*", r.raw)
                return Evidence("conflict", r, _quote_for(r, m.group(0).strip() if m else None),
                                f"only speed statement says {r.speed}")
        return Evidence("not_settled", why=f"speed statements disagree: {sorted(speeds)}")
    # Camera fallback: only a match, only if exactly one aligned reading falls in the window.
    if context_time is not None:
        t0, u0 = context_time
        hits = []
        for r in rows:
            if r.source != "camera" or r.speed in (None, 0) or r.suspicious:
                continue
            tt = true_time(r, clocks)
            if tt and abs(tt[0] - t0) <= u0 + tt[1] + 30:
                hits.append(r)
        if len(hits) == 1 and hits[0].speed == speed and (zone is None or hits[0].zone in (None, zone)):
            return Evidence("match", hits[0], _quote_for(hits[0]), "single camera reading in window")
        if hits:
            return Evidence("not_settled", why=f"{len(hits)} camera readings in window; vehicle not identifiable")
    if any(r.source == "camera" for r in rows):
        return Evidence("not_settled", why="camera rows exist but carry no vehicle identity")
    return Evidence("not_checkable", why="no speed record")


# ---------------------------------------------------------------- driver
COMBINE_ORDER = ("conflict", "not_settled", "match", "not_checkable")


def _claimed_seconds(h, m, ctx):
    d = ctx.base_date
    return datetime(d.year, d.month, d.day, h, m, tzinfo=ctx.tz).timestamp()


def evaluate(report, rows, clocks, ctx, log=lambda *_: None):
    claims = split_claims(report)
    sentence_time = {}  # sentence index -> (true time, unc) of its time fact, for same-sentence context
    results = []
    for c in claims:
        facts = []
        tm = TIME_FACT.search(c.text)
        du = DURATION_FACT.search(c.text)
        if du:
            n = float(du.group(2)) * (60 if du.group(3).lower().startswith("min") else 1)
            ref_word = du.group(5)
            kind_b = next((k for p, k in REFERENCED_KIND if p.search(ref_word)), None)
            kind_a = claim_kind(c.text[:du.start()] + " " + c.text[du.end():], exclude=kind_b) or claim_kind(c.text, exclude=kind_b)
            if du.group(4).lower() == "before":
                kind_a, kind_b = kind_b, kind_a
            if kind_a and kind_b:
                facts.append(("duration", judge_duration(kind_a, kind_b, n, bool(du.group(1)), rows, clocks)))
            else:
                facts.append(("duration", Evidence("not_checkable", why="cannot tell which events")))
        elif tm and ctx.base_date:
            kind = claim_kind(c.text)
            hour = int(tm.group(2))
            if tm.group(4):
                hour = hour % 12 + (12 if tm.group(4).lower() == "p" else 0)
            claimed = _claimed_seconds(hour, int(tm.group(3)), ctx)
            if kind:
                ev = judge_time(kind, claimed, bool(tm.group(1)), rows, clocks, ctx,
                                first=bool(re.search(r"\bfirst\b", c.text, re.I)))
                facts.append(("time", ev))
                if ev.verdict == "match" and ev.row is not None:
                    sentence_time[c.sentence_idx] = true_time(ev.row, clocks)
            else:
                facts.append(("time", Evidence("not_checkable", why="time given but event unclear")))
        sp = SPEED_FACT.search(c.text)
        if sp:
            z = ZONE_FACT.search(c.text)
            zone = int(next(g for g in z.groups() if g)) if z else None
            facts.append(("speed", judge_speed(int(sp.group(1)), zone, rows, clocks, sentence_time.get(c.sentence_idx))))
        if LOCATION_FACT.fullmatch(" " + c.text) or (not facts and LOCATION_FACT.search(c.text)):
            facts.append(("location", Evidence("not_checkable", why="no record carries a location")))
        if COUNT_FACT.search(c.text):
            facts.append(("count", Evidence("not_checkable", why="no record identifies lanes or individual vehicles")))
        if not facts:
            facts.append(("other", Evidence("not_checkable", why="no checkable time, speed or count")))
        # combine facts of one claim: any conflict wins, then uncertainty, then match
        checkable = [f for f in facts if f[1].verdict != "not_checkable"] or facts
        final = min(checkable, key=lambda f: COMBINE_ORDER.index(f[1].verdict))
        log(f"- {c.text!r}: " + " | ".join(f"{k}={e.verdict} [{e.why}]" for k, e in facts))
        results.append((c, final[1]))
    return results
