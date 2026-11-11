#!/usr/bin/env python3
"""
refresh_blackmonth.py: the job behind the Black Month tab.

Calls build(ctx) on every section module bm_<key>.py (bm_common.SECTIONS) and
writes each payload to its own Firestore doc. One section failing is logged
and skipped; the page keeps showing that section's previous snapshot.

    python3 refresh_blackmonth.py                      # all sections → Firestore
    python3 refresh_blackmonth.py --section pacing     # one section
    python3 refresh_blackmonth.py --section pacing --dry-run --out /tmp/p.json
                                                       # build, write JSON locally only

`insights` runs last because it reads the other sections' fresh snapshots;
`meta` reads this run's pacing payload (ctx["built"]), so it comes after pacing.
"""
from __future__ import annotations

import argparse
import importlib
import json
import sys
import time
import traceback
from pathlib import Path

import bm_common as bm


def run(sections: list[str], dry_run: bool, out: str | None) -> int:
    ctx = bm.make_ctx()
    # insights reads the others' output, so it always goes last.
    order = sorted(sections, key=lambda s: s == "insights")
    failed = 0
    built = {}
    ctx["built"] = built          # meta reads this run's pacing (margins, headroom)
    for key in order:
        try:
            mod = importlib.import_module(f"bm_{key}")
        except ModuleNotFoundError as e:
            if e.name == f"bm_{key}":
                print(f"[bm] {key}: no module yet, skipped", flush=True)
                continue
            raise
        t0 = time.time()
        try:
            payload = bm.envelope(key, mod.build(ctx))
            raw = json.dumps(payload, default=str)
            payload = json.loads(raw)          # dates → strings, proves serialisable
            if len(raw) > 900_000:
                raise ValueError(f"payload {len(raw)} bytes exceeds the 900 kB budget")
            if not dry_run:
                bm.write_section(key, payload)
            built[key] = payload
            print(f"[bm] {key}: ok, {len(raw)/1024:.0f} kB in {time.time()-t0:.1f}s", flush=True)
        except Exception:
            failed += 1
            print(f"[bm] {key}: FAILED\n{traceback.format_exc()}", flush=True)
    if out:
        Path(out).write_text(json.dumps(built if len(built) != 1 else next(iter(built.values())),
                                        indent=1, default=str))
        print(f"[bm] wrote {out}", flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--section", action="append", choices=bm.SECTIONS)
    ap.add_argument("--dry-run", action="store_true", help="do not write Firestore")
    ap.add_argument("--out", help="also write the built payload(s) to this JSON file")
    a = ap.parse_args()
    sys.exit(run(a.section or bm.SECTIONS, a.dry_run, a.out))
