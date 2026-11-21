# Verified Findings: Plearun take-home

    python3 plearun_cli.py REPORT RECORDS_DIR [--explain] > findings.json
    make test        # offline, standard library only (Python 3.11+)

`--explain` prints the clock offsets and the evidence behind every verdict to stderr.

## How it works
1. **Load** (`verifier/loaders.py`). CSV: UTF-8/BOM, UTF-16, then Windows-1252; `#` marker lines skipped; delimiter sniffed; columns found by name (separate Date/Time columns are joined). JSON or JSON Lines: every object inside an array that has a parseable time. WebVTT: cues numbered from 1 (header, `NOTE`s and cue IDs ignored). Every row keeps its **exact raw text**. Duplicate readings (the same camera row exported as ISO and as epoch) count once. Row numbers: CSV = data row (1 = first row after the header), JSON = position among objects inside arrays, WebVTT = cue number.
2. **Time** (`timeparse.py`). Epoch ms/s, ISO with offset, naive datetimes and `HH:MM` all become Unix seconds with a precision (1 s or 60 s). Naive and epoch values take the UTC offset that most ISO strings in the pack use.
3. **Clocks** (`clocks.py`). The dispatch export is the reference. Another device gets an offset only from an event kind that appears **exactly once** in both devices (911 call, dispatch, on scene, stop). Several anchors must agree within their uncertainty or the device stays unaligned. Unaligned devices never decide a verdict. Sample: notes +41 s (from the 911 call), bodycam aligned via the stop, CAM-03 ~-1 s and CAM-07 ~+89 s via their single zero-speed rows.
4. **Claims** (`judge.py`). The report is split into sentences and clauses, so each claim is copied verbatim. Facts pulled out: event time (exact, "about", with seconds, 12-hour, or "between A and B"), interval ("N minutes after", "within N minutes of"), speed with comparators ("over 55 mph") and the posted limit, location, and lane/count. Each time is paired with the event word nearest to it.
5. **Verdict.** A match needs the whole uncertainty interval inside the tolerance. A conflict needs it entirely outside a wider band (exact `21:11` → match if within −30..+60 s, conflict beyond −90..+120 s; "about" → match ≤150 s, conflict >300 s). Anything in between is `not_settled`. A row never counts as evidence for the same event that set its own clock.
6. **Quote check** (`plearun_cli.py`, `loaders.raw_rows`). Before a match or conflict is written, the cited file is re-read from disk by a separate raw-text reader, and the quote must be an exact substring of that row. If not, the finding is downgraded to `not_settled`.

## Three hardest decisions
- **The 911 call is `not_settled`, not a conflict.** The log says 21:10:19 and the report says 21:11. That is 41 s, the same skew the notes clock shows. It looks like the report writer copied the notes clock. A confident conflict over sub-minute rounding is exactly the false conflict the brief warns about.
- **Only evidence about the vehicle counts.** Camera rows carry no vehicle or lane identity. The 58 mph row at 21:07 is a different car, so cameras never produce a conflict, and they only support a speed match when exactly one aligned reading falls in the window. The 58-in-a-45 match comes from the officer's bodycam statement instead. The officer's own driving speed ("I'm doing 80") is ignored, and rows more than 6 h from a claim are treated as another incident, not a contradiction.
- **One anchor is enough, but only if it is unique on both sides.** Requiring two anchors would leave every device unaligned in this pack. Accepting the nearest similar event would make it easy to mislead the tool. Uniqueness plus a consistency check is the middle ground.

## What it gets wrong
- Claim and event recognition is keyword-based. Unusual wording ("arrived at the forecourt") can be missed, which shows up as `not_checkable`, not as a wrong verdict.
- It assumes one wall-clock zone for the whole incident and does not handle a report that crosses midnight. "2116 hrs" is not parsed.
- The bodycam's "58 in a 45" is the officer confirming himself. It is a record, but not an independent one.

## What breaks first on real records
Event vocabulary (real CAD codes such as `10-97` or `ONSCN`), several incidents mixed in one export, camera logs with plate/lane fields worth using, and anchor events that repeat (two 911 calls), which correctly but unhelpfully leave a device unaligned.

## Tools used
- **Claude (Anthropic)**: wrote the implementation, the tests and this README with me in a chat session. I reviewed the design decisions and can walk through every line.
- Python 3 standard library only. No network, no LLM at runtime. The optional `mock_llm.py` path is not used.
