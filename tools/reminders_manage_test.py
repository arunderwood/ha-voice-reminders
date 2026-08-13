"""Check listing and cancelling reminders, without eating real ones.

This exists because the ad-hoc version of it did not. Driving "cancel all my
reminders" through the pipeline against the live list destroyed a genuine
reminder that happened to be pending at the time; it was restored, but only
because it was still visible in the transcript.

So: snapshot every pending item first, run the destructive cases, then put the
snapshot back and verify it is whole again. Anything this deletes, it restores.

Restored items get new uids, and any that fell due while the test ran will fire
on the next minute tick -- late, but not lost.

    export HA_URL=http://homeassistant.local:8123
    export HA_TOKEN=...        # Profile -> Long-lived access tokens
    python tools/reminders_manage_test.py
"""

import asyncio, json, os, aiohttp
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

TOKEN = os.environ["HA_TOKEN"]
BASE = os.environ.get("HA_URL", "http://localhost:8123")
PIPELINE = os.environ.get("PIPELINE")  # None = the preferred pipeline
ENT = os.environ.get("REMINDER_LIST", "todo.reminders")
TZ = ZoneInfo(os.environ.get("TZ_NAME", "America/Chicago"))

# Distinctive on purpose, so a failed restore is obvious in the real list.
FIXTURES = [
    ("zzz test alpha", 3),
    ("zzz test beta", 5),
    ("zzz test gamma", 7),
]


async def main():
    hdr = {"Authorization": f"Bearer {TOKEN}"}
    async with aiohttp.ClientSession(headers=hdr) as s, \
            s.ws_connect(BASE.replace("http", "ws") + "/api/websocket") as ws:
        await ws.receive_str()
        await ws.send_json({"type": "auth", "access_token": TOKEN})
        while json.loads(await ws.receive_str())["type"] != "auth_ok":
            pass

        async def items(status="needs_action"):
            async with s.post(f"{BASE}/api/services/todo/get_items?return_response",
                              json={"entity_id": ENT, "status": status}) as r:
                return (await r.json())["service_response"][ENT]["items"]

        async def say(text):
            nonlocal mid
            mid += 1
            await ws.send_json({
                "id": mid, "type": "assist_pipeline/run",
                "start_stage": "intent", "end_stage": "intent",
                "input": {"text": text}, # Omit the key entirely when unset: the websocket schema
                # rejects a null, and absent means "the preferred pipeline".
                **({"pipeline": PIPELINE} if PIPELINE else {}),
            })
            speech = None
            while True:
                m = json.loads(await ws.receive_str())
                if m.get("id") != mid:
                    continue
                if m["type"] == "result" and not m["success"]:
                    return f"ERROR {m['error']}"
                if m["type"] != "event":
                    continue
                ev, d = m["event"]["type"], m["event"].get("data") or {}
                if ev == "intent-end":
                    resp = d.get("intent_output", {}).get("response", {})
                    speech = (resp.get("speech", {}).get("plain", {}) or {}).get("speech")
                elif ev == "run-end":
                    return speech

        mid = 100
        fails = 0

        def check(label, ok, detail=""):
            nonlocal fails
            fails += not ok
            print(f"  {'ok  ' if ok else 'FAIL'}  {label}" + ("" if ok else f"   {detail}"))

        # --- snapshot anything real that is already pending
        snapshot = [{"summary": i["summary"], "due": i.get("due"),
                     "description": i.get("description")} for i in await items()]
        print(f"  snapshot: {len(snapshot)} pending item(s) to preserve")
        for i in snapshot:
            print(f"      {i['summary']!r} due={i['due']}")

        try:
            now = datetime.now(TZ)
            for summary, mins in FIXTURES:
                due = (now + timedelta(minutes=mins)).replace(second=0, microsecond=0)
                await s.post(f"{BASE}/api/services/todo/add_item",
                             json={"entity_id": ENT, "item": summary,
                                   "due_datetime": due.isoformat()})

            spoken = await say("what are my reminders")
            print(f"\n  list -> {spoken!r}")
            check("list reports every pending reminder",
                  all(f[0] in (spoken or "") for f in FIXTURES) and
                  str(len(snapshot) + 3) in (spoken or ""),
                  f"expected {len(snapshot) + 3} and all fixtures")

            spoken = await say("cancel the reminder to zzz test beta")
            print(f"  cancel-by-task -> {spoken!r}")
            left = {i["summary"] for i in await items()}
            check("cancel by task removes exactly that one",
                  "zzz test beta" not in left and "zzz test alpha" in left
                  and "zzz test gamma" in left, str(sorted(left)))

            spoken = await say("cancel my next reminder")
            print(f"  cancel-next -> {spoken!r}")
            left_after = {i["summary"] for i in await items()}
            # alpha was the soonest of the fixtures, but a real snapshot item
            # could be sooner still -- so only assert one thing vanished.
            check("cancel next removes exactly one",
                  len(left_after) == len(left) - 1,
                  f"{len(left)} -> {len(left_after)}")

            spoken = await say("cancel all my reminders")
            print(f"  cancel-all -> {spoken!r}")
            check("cancel all empties the list", len(await items()) == 0)

            spoken = await say("what are my reminders")
            print(f"  list when empty -> {spoken!r}")
            # Assert on what must be true, not on wording: the empty-list line
            # is picked at random and one variant never says "reminder" at all.
            check("empty list answers without naming anything",
                  bool(spoken) and not any(f[0] in spoken for f in FIXTURES),
                  repr(spoken))

        finally:
            # --- restore, whatever happened above
            for i in snapshot:
                body = {"entity_id": ENT, "item": i["summary"]}
                if i["due"]:
                    body["due_datetime"] = i["due"]
                # Recurrence lives in the description. Restoring without it
                # silently converts a recurring reminder into a one-off -- the
                # series just stops, and nothing anywhere says so.
                if i.get("description"):
                    body["description"] = i["description"]
                await s.post(f"{BASE}/api/services/todo/add_item", json=body)
            for i in await items():
                if i["summary"].startswith("zzz test "):
                    await s.post(f"{BASE}/api/services/todo/remove_item",
                                 json={"entity_id": ENT, "item": i["summary"]})
            restored = [{"summary": i["summary"], "due": i.get("due"),
                         "description": i.get("description")} for i in await items()]
            same = sorted(map(str, restored)) == sorted(map(str, snapshot))
            print(f"\n  restored {len(restored)} of {len(snapshot)} snapshot item(s)")
            for i in restored:
                print(f"      {i['summary']!r} due={i['due']}")
            check("live list is back exactly as it was", same,
                  f"{restored} != {snapshot}")

        print("\nFAILURES:", fails)

asyncio.run(main())
