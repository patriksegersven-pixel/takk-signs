"""
bm_insights_api.py: live routes for the Black Month "Today's actions" section.
Mounted by app.py behind Basic auth (see BLACKMONTH.md).

    GET  /api/blackmonth/insights/live                 fresh cards + manual events + checklist ticks
    POST /api/blackmonth/insights/cards/{id}/status    {status, by, note?, owner?}
    POST /api/blackmonth/insights/events               {date, kind, market, brand?, title, detail?, by}
    POST /api/blackmonth/insights/checklist/{item_id}  {done: bool, by}

The snapshot (GET /api/blackmonth/insights) is rebuilt by the refresh job; these
routes let the page show decisions and new agent cards without waiting for it.
Writes go only to Firestore `blackmonth_insights` / `blackmonth_events`.
"""
from __future__ import annotations

from fastapi import APIRouter, Body
from fastapi.responses import JSONResponse

import bm_insights as bi

router = APIRouter(prefix="/api/blackmonth/insights")


def _err(e: Exception, code: int = 400) -> JSONResponse:
    return JSONResponse({"ok": False, "error": str(e)}, status_code=code)


@router.get("/live")
def live():
    try:
        cards = bi.list_cards(days=45)
        events = bi.list_events(days=60)
    except Exception as e:                       # noqa: BLE001
        print(f"ERROR /api/blackmonth/insights/live: {type(e).__name__}: {e}", flush=True)
        return _err(Exception("Firestore could not be read — try again shortly"), 503)
    return {"ok": True, "today": bi.bm.today().isoformat(), "cards": cards, "stats": bi.hit_rate(cards),
            "events": [e for e in events if e.get("kind") != "checklist"],
            "checklist_ticks": [e for e in events if e.get("kind") == "checklist"]}


@router.post("/cards/{cid}/status")
def card_status(cid: str, body: dict = Body(...)):
    try:
        c = bi.set_status(cid, str(body.get("status", "")), by=str(body.get("by") or "")[:80],
                          note=str(body.get("note") or "")[:500],
                          owner=(str(body["owner"])[:80] if body.get("owner") is not None else None))
    except KeyError:
        return _err(Exception(f"no card {cid}"), 404)
    except ValueError as e:
        return _err(e)
    return {"ok": True, "card": c}


@router.post("/events")
def add_event(body: dict = Body(...)):
    body = {k: body.get(k) for k in ("date", "kind", "market", "brand", "title", "detail", "by")}
    if body.get("kind") == "checklist":
        return _err(ValueError("use the checklist route"))
    try:
        e = bi.add_event(dict(body, source="manual"))
    except ValueError as err:
        return _err(err)
    return {"ok": True, "event": e}


@router.post("/checklist/{item_id}")
def tick(item_id: str, body: dict = Body(...)):
    item = next((i for i in bi.CHECKLIST if i["id"] == item_id), None)
    if not item:
        return _err(Exception(f"no checklist item {item_id}"), 404)
    done = bool(body.get("done", True))
    try:
        e = bi.add_event({"kind": "checklist", "ref": item_id, "market": "all",
                          "title": ("✓ " if done else "↺ ") + item["title"],
                          "detail": "" if done else "undo", "by": str(body.get("by") or "")[:80]})
    except ValueError as err:
        return _err(err)
    return {"ok": True, "event": e}
