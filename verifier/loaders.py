"""Read every record file into a flat list of Row objects.

A Row keeps the exact raw text of its row (used later to verify quotes) and
whatever we could understand from it: time, device clock, event kinds, speeds.
Record text is only ever matched against fixed patterns; it is never executed
or treated as an instruction.
"""
import csv
import io
import json
import os
import re
from dataclasses import dataclass, field

from .timeparse import TimeContext, parse_ts, parse_vtt_time, find_tz_hint

TIME_COL = re.compile(r"time|timestamp|logged|datetime|date|^ts$|^at$", re.I)
DEVICE_COL = re.compile(r"device|camera|^cam", re.I)
SPEED_COL = re.compile(r"speed", re.I)
ZONE_COL = re.compile(r"zone|limit|posted", re.I)
SKIP_TEXT_COL = re.compile(r"^id$|_id$|author|officer|^by$|name|unit", re.I)
JSON_TEXT_KEYS = re.compile(r"^(type|event|event_type|description|desc|note|notes|text|message|status|remarks?)$", re.I)

KIND_PATTERNS = {
    "call911": re.compile(r"\b911\b|911_call|\bcaller\b|\bcall (received|logged|in)\b", re.I),
    "dispatched": re.compile(r"\bdispatched\b|unit_dispatched|\bassigned\b", re.I),
    "on_scene": re.compile(r"on[_ ]scene|\barrived\b|\barrival\b", re.I),
    "stop": re.compile(r"\bstopp?ed\b|\bstop\b|\bpull(?:ed|ing)? (?:in|over|into)\b|\bparked\b", re.I),
    "observe": re.compile(r"\bobserv|\bspotted\b|\bfirst saw\b|\bsighted\b", re.I),
}
# Speech is the officer talking: relaying a caller is not the call itself.
SPEECH_KINDS = {"stop", "observe"}

SPEED_IN_ZONE = re.compile(r"\b(\d{2,3})\s*(?:mph)?\s+in\s+an?\s+(\d{2,3})\b", re.I)
SPEED_MPH = re.compile(r"\b(\d{2,3})\s*mph\b", re.I)
ZONE_TEXT = re.compile(r"\b(\d{2,3})\s*(?:mph\s+)?zone\b|\bposted\s+(\d{2,3})|\blimit\s+(?:of\s+)?(\d{2,3})", re.I)

# Rows that address a reader instead of recording an event. They are kept for
# citation purposes but never used as evidence.
INSTRUCTION_LIKE = re.compile(
    r"ignore (?:all|any|the|previous|prior)|disregard|system prompt|\bas an ai\b|"
    r"you (?:must|should|are to) (?:output|mark|return|report|treat)|"
    r"mark (?:this|all|every|each)\b.{0,40}\b(?:match|conflict)|"
    r"\"?finding\"?\s*[:=]|\bverdict\b|\bnew instructions?\b",
    re.I,
)


@dataclass
class Row:
    file: str
    row: int
    raw: str
    source: str            # "camera" | "log" | "speech"
    device: str            # one clock per device
    t: float = None        # seconds (absolute) or seconds from file start (speech)
    prec: float = 1.0
    relative: bool = False
    text: str = ""
    speed: int = None
    zone: int = None
    kinds: set = field(default_factory=set)
    suspicious: bool = False
    time_field_raw: str = ""   # exact text of the timestamp as it appears in the raw row

    @property
    def cite(self):
        return {"file": self.file, "row": self.row}


def read_bytes(path):
    with open(path, "rb") as fh:
        return fh.read()


def decode_bytes(data):
    """UTF-8 (with or without BOM) first, then Windows-1252, never fail."""
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        try:
            return data.decode("cp1252")
        except UnicodeDecodeError:
            return data.decode("latin-1")


def classify(row):
    if INSTRUCTION_LIKE.search(row.text):
        row.suspicious = True
        return
    allowed = SPEECH_KINDS if row.source == "speech" else KIND_PATTERNS.keys()
    for kind, pat in KIND_PATTERNS.items():
        if kind in allowed and pat.search(row.text):
            row.kinds.add(kind)
    if row.source == "camera" and row.speed == 0:
        row.kinds.add("stop")
    if row.source != "camera":
        m = SPEED_IN_ZONE.search(row.text)
        if m:
            row.speed, row.zone = int(m.group(1)), int(m.group(2))
        else:
            m = SPEED_MPH.search(row.text)
            if m:
                row.speed = int(m.group(1))
            z = ZONE_TEXT.search(row.text)
            if z:
                row.zone = int(next(g for g in z.groups() if g))


def _int_or_none(v):
    try:
        return int(float(str(v).strip()))
    except (TypeError, ValueError):
        return None


def load_csv(path, ctx):
    name = os.path.basename(path)
    text = decode_bytes(read_bytes(path))
    lines = text.splitlines()
    start = 0
    while start < len(lines) and (not lines[start].strip() or lines[start].lstrip().startswith("#")):
        start += 1
    body_lines = lines[start:]
    reader = csv.reader(io.StringIO("\n".join(body_lines)))
    try:
        header = [h.strip() for h in next(reader)]
    except StopIteration:
        return []
    tcols = [i for i, h in enumerate(header) if TIME_COL.search(h)]
    tcol = tcols[0] if tcols else None
    datecol = next((i for i in tcols if re.fullmatch(r"date", header[i], re.I)), None)
    if datecol is not None and len(tcols) > 1:
        tcol = next(i for i in tcols if i != datecol)
    dcol = next((i for i, h in enumerate(header) if DEVICE_COL.search(h)), None)
    scol = next((i for i, h in enumerate(header) if SPEED_COL.search(h)), None)
    zcol = next((i for i, h in enumerate(header) if ZONE_COL.search(h)), None)
    used = {tcol, dcol, scol, zcol, datecol}
    text_cols = [i for i, h in enumerate(header) if i not in used and not SKIP_TEXT_COL.search(h)]
    source = "camera" if scol is not None else "log"

    rows, n, prev_line = [], 0, reader.line_num
    for rec in reader:
        raw = "\n".join(body_lines[prev_line:reader.line_num]).strip("\r\n")
        prev_line = reader.line_num
        if not rec or not any(c.strip() for c in rec) or rec[0].lstrip().startswith("#"):
            continue
        n += 1
        get = lambda i: rec[i].strip() if i is not None and i < len(rec) else ""
        r = Row(file=name, row=n, raw=raw, source=source,
                device=(get(dcol) or name) if source == "camera" else name)
        tval = get(tcol)
        parsed = parse_ts(tval, ctx)
        if datecol is not None and datecol != tcol and get(datecol):
            parsed = parse_ts(f"{get(datecol)} {tval}", ctx) or parsed
        if parsed:
            r.t, r.prec, r.relative = parsed
            r.time_field_raw = tval
        r.text = " ".join(get(i) for i in text_cols)
        if source == "camera":
            r.speed, r.zone = _int_or_none(get(scol)), _int_or_none(get(zcol))
        classify(r)
        rows.append(r)
    return dedupe(rows)


def _json_dict_spans(text):
    """Character spans of every JSON object that is a direct element of an array, in document order."""
    spans, stack, i, n = [], [], 0, len(text)
    while i < n:
        c = text[i]
        if c == '"':
            i += 1
            while i < n and text[i] != '"':
                i += 2 if text[i] == "\\" else 1
        elif c in "{[":
            stack.append((c, i))
        elif c in "}]":
            if stack:
                kind, s = stack.pop()
                if kind == "{" and stack and stack[-1][0] == "[":
                    spans.append((s, i + 1))
        i += 1
    return sorted(spans)


def _json_array_dicts(obj, out):
    """Pre-order walk collecting dicts that are direct elements of lists (same order as the spans)."""
    if isinstance(obj, dict):
        for v in obj.values():
            _json_array_dicts(v, out)
    elif isinstance(obj, list):
        for v in obj:
            if isinstance(v, dict):
                out.append(v)
            _json_array_dicts(v, out)


def load_json(path, ctx):
    name = os.path.basename(path)
    text = decode_bytes(read_bytes(path))
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return []
    dicts = []
    _json_array_dicts(data, dicts)
    spans = _json_dict_spans(text)
    if len(spans) != len(dicts):  # should not happen; fall back to re-serialised text
        spans = None
    rows = []
    for idx, d in enumerate(dicts):
        # row = 1-based position among all objects inside arrays, in document order
        tkey = next((k for k in d if TIME_COL.search(k) and parse_ts(d[k], ctx)), None)
        if tkey is None:
            continue
        raw = text[spans[idx][0]:spans[idx][1]] if spans else json.dumps(d)
        r = Row(file=name, row=idx + 1, raw=raw, source="log", device=name)
        r.t, r.prec, r.relative = parse_ts(d[tkey], ctx)
        # the exact key/value text, e.g.  "logged": "2026-03-02T21:17:19-05:00"
        m = re.search(r'"%s"\s*:\s*(?:"(?:[^"\\]|\\.)*"|-?[\d.eE+]+)' % re.escape(tkey), raw)
        r.time_field_raw = m.group(0) if m else ""
        r.text = " ".join(str(v) for k, v in d.items() if JSON_TEXT_KEYS.match(k) and isinstance(v, (str, int)))
        for k in d:
            if SPEED_COL.search(k):
                r.speed = _int_or_none(d[k])
            elif ZONE_COL.search(k):
                r.zone = _int_or_none(d[k])
        classify(r)
        rows.append(r)
    return rows


def load_vtt(path, ctx):
    name = os.path.basename(path)
    text = decode_bytes(read_bytes(path)).replace("\r\n", "\n")
    blocks = re.split(r"\n\s*\n", text)
    rows, n = [], 0
    for b in blocks:
        lines = [ln for ln in b.split("\n") if ln.strip()]
        ti = next((i for i, ln in enumerate(lines) if "-->" in ln), None)
        if ti is None:
            continue  # WEBVTT header, NOTE, STYLE blocks
        start_s, _, rest = lines[ti].partition("-->")
        start = parse_vtt_time(start_s)
        end = parse_vtt_time(rest.split()[0]) if rest.split() else None
        if start is None:
            continue
        n += 1
        payload = "\n".join(lines[ti + 1:])
        r = Row(file=name, row=n, raw=payload, source="speech", device=name,
                t=start, prec=max(1.0, (end or start) - start), relative=True, text=payload)
        classify(r)
        rows.append(r)
    return rows


def dedupe(rows):
    """The same reading exported twice in two timestamp formats counts once (first row kept)."""
    seen, out = set(), []
    for r in rows:
        key = (r.device, round(r.t) if r.t is not None else r.raw, r.speed, r.zone, r.text)
        if key in seen:
            continue
        seen.add(key)
        out.append(r)
    return out


LOADERS = {".csv": load_csv, ".tsv": load_csv, ".json": load_json, ".vtt": load_vtt}


def load_records(records_dir, base_date=None):
    paths = sorted(
        os.path.join(records_dir, f) for f in os.listdir(records_dir)
        if os.path.splitext(f)[1].lower() in LOADERS
    )
    # first pass: learn the local UTC offset from any ISO timestamps in the files
    sample = []
    for p in paths:
        sample += re.findall(r"\d{4}-\d{2}-\d{2}[T ][\d:.]+(?:Z|[+-]\d{2}:?\d{2})",
                             decode_bytes(read_bytes(p)))
    ctx = TimeContext(tz=find_tz_hint(sample), base_date=base_date)
    rows = []
    for p in paths:
        try:
            rows += LOADERS[os.path.splitext(p)[1].lower()](p, ctx)
        except Exception as exc:  # one unreadable file must not take down the others
            import sys
            print(f"skipped {os.path.basename(p)}: {exc}", file=sys.stderr)
    return rows, ctx
