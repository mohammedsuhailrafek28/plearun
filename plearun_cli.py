"""Verified findings: check an incident report's claims against machine records.

Usage: python3 plearun_cli.py REPORT RECORDS_DIR [--explain] > findings.json
Runs fully offline. --explain prints the reasoning (clock offsets, per-claim evidence) to stderr.
"""
import json
import os
import sys

from verifier.loaders import LOADERS, decode_bytes, load_records, read_bytes
from verifier.clocks import align
from verifier.judge import evaluate
from verifier.timeparse import first_date


def verify_quote(records_dir, cite, quote):
    """Independent re-read from disk: the quote must be an exact substring of the cited row's raw text."""
    if not quote or not cite:
        return False
    path = os.path.join(records_dir, cite["file"])
    if not os.path.isfile(path):
        return False
    ext = os.path.splitext(path)[1].lower()
    from verifier.timeparse import TimeContext
    rows = LOADERS[ext](path, TimeContext(base_date=first_date("1970-01-01")))
    row = next((r for r in rows if r.row == cite["row"]), None)
    return bool(row) and quote in row.raw and quote in decode_bytes(read_bytes(path))


def run(report_path, records_dir, explain=False):
    log = (lambda m: print(m, file=sys.stderr)) if explain else (lambda m: None)
    report = decode_bytes(read_bytes(report_path))
    base_date = first_date(report)
    if base_date is None:  # fall back to the first date found in the records
        for f in sorted(os.listdir(records_dir)):
            base_date = first_date(decode_bytes(read_bytes(os.path.join(records_dir, f))))
            if base_date:
                break
    rows, ctx = load_records(records_dir, base_date)
    log(f"loaded {len(rows)} rows; local zone {ctx.tz}; incident date {base_date}")
    clocks, _ = align(rows, log)
    findings = []
    for claim, ev in evaluate(report, rows, clocks, ctx, log):
        item = {"claim": claim.text, "finding": ev.verdict}
        if ev.verdict in ("match", "conflict"):
            cite = ev.row.cite
            if verify_quote(records_dir, cite, ev.quote):
                item["cite"], item["quote"] = cite, ev.quote
            else:
                log(f"  quote failed verification for {claim.text!r}; downgraded to not_settled")
                item["finding"] = "not_settled"
        findings.append(item)
    return {"findings": findings}


def main(argv):
    args = [a for a in argv[1:] if not a.startswith("--")]
    if len(args) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    out = run(args[0], args[1], explain="--explain" in argv)
    json.dump(out, sys.stdout, indent=2, ensure_ascii=False)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
