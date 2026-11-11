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

Each build runs in a daemon thread with a time limit (BM_SECTION_TIMEOUT, 300 s).
A section that overruns is logged as failed and skipped, so one stalled API call
can't use up the job's 1200 s and leave every later section stale.
"""
from __future__ import annotations

import argparse
import importlib
import json
import os
import sys
import threading
import time
import traceback
from pathlib import Path

import bm_common as bm

SECTION_TIMEOUT = float(os.environ.get("BM_SECTION_TIMEOUT", "300"))


class SectionTimeout(Exception):
    pass


def _build_with_limit(mod, ctx, limit: float):
    """mod.build(ctx) in a daemon thread; raise SectionTimeout if it overruns.
    The stuck thread is abandoned (its late result is never written)."""
    box: dict = {}

    def target():
        try:
            box["v"] = mod.build(ctx)
        except BaseException as e:          # re-raised in the main thread
            box["e"] = e

    t = threading.Thread(target=target, daemon=True)
    t.start()
    t.join(limit)
    if t.is_alive():
        raise SectionTimeout(f"build still running after {limit:.0f}s, skipped")
    if "e" in box:
        raise box["e"]
    return box["v"]


def run(sections: list[str], dry_run: bool, out: str | None) -> int:
    ctx = bm.make_ctx()
    # insights reads the others' output, so it always goes last.
    order = sorted(sections, key=lambda s: s == "insights")
    failed = 0
    timed_out = False
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
            payload = bm.envelope(key, _build_with_limit(mod, ctx, SECTION_TIMEOUT))
            raw = json.dumps(payload, default=str)
            payload = json.loads(raw)          # dates → strings, proves serialisable
            if len(raw) > 900_000:
                raise ValueError(f"payload {len(raw)} bytes exceeds the 900 kB budget")
            if not dry_run:
                bm.write_section(key, payload)
            built[key] = payload
            print(f"[bm] {key}: ok, {len(raw)/1024:.0f} kB in {time.time()-t0:.1f}s", flush=True)
        except SectionTimeout as e:
            failed += 1
            timed_out = True
            print(f"[bm] {key}: FAILED, {e}", flush=True)
        except Exception:
            failed += 1
            print(f"[bm] {key}: FAILED\n{traceback.format_exc()}", flush=True)
    if out:
        Path(out).write_text(json.dumps(built if len(built) != 1 else next(iter(built.values())),
                                        indent=1, default=str))
        print(f"[bm] wrote {out}", flush=True)
    return 1 if failed else 0, timed_out


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--section", action="append", choices=bm.SECTIONS)
    ap.add_argument("--dry-run", action="store_true", help="do not write Firestore")
    ap.add_argument("--out", help="also write the built payload(s) to this JSON file")
    a = ap.parse_args()
    code, timed_out = run(a.section or bm.SECTIONS, a.dry_run, a.out)
    if timed_out:
        # An abandoned thread may sit in a gRPC call that blocks interpreter
        # shutdown; leave without waiting for it.
        sys.stdout.flush(); sys.stderr.flush()
        os._exit(code)
    sys.exit(code)
