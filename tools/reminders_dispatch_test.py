"""Watch a reminder actually fire (PLAN.md phase 4 acceptance).

Sets a reminder a little way out, then subscribes to call_service events and
records what the dispatch automation really did -- rather than inferring it from
the item having vanished. Proves all four parts of the acceptance criterion:

    fires once  ·  speaks  ·  pushes  ·  does not repeat

"Does not repeat" is the one that needs patience: it keeps listening for two
further minute-ticks after the reminder fires, because a dispatcher that marks
items complete too late re-announces every single minute and looks perfect for
the first sixty seconds.

This makes the satellite speak out loud. That is the test.

    export HA_URL=http://homeassistant.local:8123
    export HA_TOKEN=...        # Profile -> Long-lived access tokens
    python tools/reminders_dispatch_test.py

    LEAD=20 WATCH=180 ... to change the timings.
"""

import asyncio, json, os, aiohttp
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

TOKEN = os.environ["HA_TOKEN"]
BASE = os.environ.get("HA_URL", "http://localhost:8123")
ENT = os.environ.get("REMINDER_LIST", "todo.reminders")
TZ = ZoneInfo(os.environ.get("TZ_NAME", "America/Chicago"))
TASK = "test the dispatcher"
LEAD = int(os.environ.get("LEAD", "20"))     # seconds until the reminder is due
WATCH = int(os.environ.get("WATCH", "210"))  # seconds to keep listening after


async def main():
    hdr = {"Authorization": f"Bearer {TOKEN}"}
    async with aiohttp.ClientSession(headers=hdr) as s, \
            s.ws_connect(BASE.replace("http", "ws") + "/api/websocket") as ws:
        await ws.receive_str()
        await ws.send_json({"type": "auth", "access_token": TOKEN})
        while json.loads(await ws.receive_str())["type"] != "auth_ok":
            pass

        async def items(status=None):
            body = {"entity_id": ENT}
            if status:
                body["status"] = status
            async with s.post(f"{BASE}/api/services/todo/get_items?return_response",
                              json=body) as r:
                return (await r.json())["service_response"][ENT]["items"]

        start = [i for i in await items()]
        if start:
            print(f"  note: {len(start)} pre-existing items on {ENT}")

        before_uids = {i["uid"] for i in await items()}
        due = (datetime.now(TZ) + timedelta(seconds=LEAD)).replace(microsecond=0)
        async with s.post(f"{BASE}/api/services/todo/add_item",
                          json={"entity_id": ENT, "item": TASK,
                                "due_datetime": due.isoformat()}) as r:
            print(f"  added {TASK!r} due {due.isoformat()} (HTTP {r.status})")

        # Track the item by uid, not by summary. Snooze re-adds a reminder under
        # the SAME summary, so a snoozed "test the dispatcher" is indistinguish-
        # able from this one by name -- which is exactly how this test once
        # reported a phantom failure.
        mine_uid = next((i["uid"] for i in await items()
                         if i["uid"] not in before_uids), None)
        print(f"  tracking uid {mine_uid}")

        await ws.send_json({"id": 1, "type": "subscribe_events",
                            "event_type": "call_service"})
        print(f"  listening {WATCH}s for what the dispatcher does...\n")

        announces, notifies, completes = [], [], []
        deadline = asyncio.get_event_loop().time() + WATCH
        while asyncio.get_event_loop().time() < deadline:
            try:
                raw = await asyncio.wait_for(ws.receive_str(),
                                             timeout=max(1, deadline - asyncio.get_event_loop().time()))
            except asyncio.TimeoutError:
                break
            m = json.loads(raw)
            if m.get("type") != "event" or m.get("id") != 1:
                continue
            d = m["event"]["data"]
            dom, svc, sd = d.get("domain"), d.get("service"), d.get("service_data", {})
            stamp = m["event"]["time_fired"][11:19]

            if dom == "assist_satellite" and svc == "announce":
                announces.append((stamp, sd))
                print(f"  [{stamp}] ANNOUNCE -> {sd.get('entity_id')}")
                print(f"             {sd.get('message')!r}")
            elif dom == "notify" and svc.startswith("mobile_app"):
                notifies.append((stamp, sd))
                print(f"  [{stamp}] NOTIFY   -> {svc}: "
                      f"{sd.get('title')!r} / {sd.get('message')!r}")
            elif dom == "todo" and svc == "update_item" and sd.get("status") == "completed":
                completes.append((stamp, sd))
                print(f"  [{stamp}] COMPLETE -> item={sd.get('item')}")

        left = await items("needs_action")
        done = await items("completed")
        mine_open = [i for i in left if i["uid"] == mine_uid]
        mine_done = [i for i in done if i["uid"] == mine_uid]

        print("\n  --- results ---")
        checks = [
            ("fired exactly once (announce)", len(announces) == 1, f"{len(announces)} announces"),
            ("pushed exactly once", len(notifies) == 1, f"{len(notifies)} notifications"),
            ("completed exactly once", len(completes) == 1, f"{len(completes)} completions"),
            ("no longer needs_action", not mine_open, f"{len(mine_open)} still open"),
            ("is on the completed list", bool(mine_done), "not found as completed"),
        ]
        fails = 0
        for label, ok, detail in checks:
            fails += not ok
            print(f"  {'ok  ' if ok else 'FAIL'}  {label}" + ("" if ok else f"  ({detail})"))

        # By uid here too -- removing by summary could delete a real reminder
        # that happens to share the name.
        for i in mine_done + mine_open:
            await s.post(f"{BASE}/api/services/todo/remove_item",
                         json={"entity_id": ENT, "item": i["uid"]})
        print(f"\n  cleaned up {len(mine_done) + len(mine_open)} test items")
        print("\nFAILURES:", fails)

asyncio.run(main())
