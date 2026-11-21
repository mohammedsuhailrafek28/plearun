# Verified Findings — Plearun take-home

An offline evidence-checking CLI for incident reports. It accounts for unsynchronized clocks and attaches verified source quotes to decisive findings.

**Candidate:** [@mohammedsuhailrafek28](https://github.com/mohammedsuhailrafek28) · **Data:** fictional assignment records · **Dependencies:** Python standard library

## Run

Python 3.11+; no third-party dependencies.

```sh
python3 plearun_cli.py reports/report_fictional_02.txt records > findings.json
make test
```

Add `--explain` for clock offsets and verdict reasoning on stderr. On Windows, use `python` if needed; without Make, run `python -m unittest discover -s tests -v`.

## How it works

1. **Load:** parse CSV, nested JSON/JSON Lines, and WebVTT; handle mixed encodings and timestamps, preserve raw text, and deduplicate equivalent readings.
2. **Align:** use dispatch as the reference. Infer clock offsets from event kinds occurring exactly once in each source. Inconsistent anchors leave devices unaligned; unaligned devices cannot decide verdicts.
3. **Evaluate:** extract times, intervals, speeds, locations, and counts. Apply conservative tolerance bands and timestamp precision. Ambiguous evidence yields `not_settled`; unsupported claims yield `not_checkable`. An event cannot verify itself through its own clock anchor.
4. **Verify:** independently re-read each cited file and require the quote to be an exact substring of the cited raw row before emitting `match` or `conflict`. Otherwise downgrade to `not_settled`.

## Three hardest decisions

- **Leave the 911 time unsettled:** the 41-second discrepancy overlaps notes-clock skew and minute rounding, so it does not justify a confident conflict.
- **Exclude unrelated speeds:** camera rows lack vehicle/lane identity. The sample's 58-in-a-45 match uses the officer's bodycam statement; the officer's own driving speed is excluded.
- **Allow one unique clock anchor:** requiring two leaves the sample devices unaligned. Uniqueness on both sides, consistency checks across available anchors, and excluding self-verification limit overconfidence.

## What it gets wrong

Keyword recognition misses unfamiliar wording. The tool assumes one wall-clock zone and does not handle reports crossing midnight or compact times such as `2116 hrs`. The officer's bodycam statement is recorded evidence, but not independent corroboration.

## What breaks first on real records

Real CAD codes, mixed incidents in one export, repeated anchor events (which correctly but unhelpfully leave a device unaligned), and camera identity/lane fields that need richer parsing and event association.

## Process

Built with an AI coding assistant as repeated review rounds: build, attack the tool with inputs the hidden cases might use, fix, and lock each fix in as a regression test (38 tests). The rounds caught real false conflicts before submission: "over 55 mph" against a recorded 58, a posted "45 mph limit" read as the vehicle's speed, the officer's own driving speed counted against the suspect's, and a 911 entry from another day treated as a contradiction. They also exposed silent failures (UTF-16, JSON Lines, semicolon CSVs, 12-hour times, "within N minutes of"). Large-file runtime dropped from 10.6 s to 3.8 s on a 24 MB export.

## Attribution and tools

- **Claude (Anthropic):** AI coding assistance used to generate the implementation, tests, and original README.
- **Runtime:** Python standard library only; no network or LLM calls. The optional `mock_llm.py` path is unused. The assignment brief and supplied fixtures are provided materials.
