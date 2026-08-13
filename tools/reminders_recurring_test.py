"""Prove a recurring reminder schedules its own successor (and only one).

Recurrence is stored in the to-do item's description, because local_todo has no
recurrence field. When an item fires, the dispatcher reads the marker back and
adds the next occurrence. Two things can go wrong and neither is visible from
the outside:

  * the successor never appears -- the series silently stops after one firing
  * the successor is computed from `now` rather than from the item's due time,
    so a series that fires a few seconds late drifts later every single day

and one that is visible far too late:

  * after downtime, an overdue series produces one reminder per missed day
    instead of one reminder

All three are checked here. Snapshots and restores anything real on the list.

    export HA_URL=http://homeassistant.local:8123
    export HA_TOKEN=...        # Profile -> Long-lived access tokens
    python tools/reminders_recurring_test.py
"""

import asyncio, json, os, aiohttp
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

TOKEN = os.environ["HA_TOKEN"]
BASE = os.environ.get("HA_URL", "http://localhost:8123")
ENT = os.environ.get("REMINDER_LIST", "todo.reminders")
TZ = ZoneInfo(os.environ.get("TZ_NAME", "America/Chicago"))
LEAD = int(os.environ.get("LEAD", "20"))
WATCH = int(os.environ.get("WATCH", "150"))


async def main():
    hdr = {"Authorization": f"Bearer {TOKEN}"}
    async with aiohttp.ClientSession(headers=hdr) as s:
        async def items(status=None):
            body = {"entity_id": ENT}
            if status:
                body["status"] = status
            async with s.post(f"{BASE}/api/services/todo/get_items?return_response",
                              json=body) as r:
                return (await r.json())["service_response"][ENT]["items"]

        async def add(summary, due, desc):
            await s.post(f"{BASE}/api/services/todo/add_item",
                         json={"entity_id": ENT, "item": summary,
                               "due_datetime": due.isoformat(), "description": desc})

        async def drop(uids):
            for u in uids:
                await s.post(f"{BASE}/api/services/todo/remove_item",
                             json={"entity_id": ENT, "item": u})

        snapshot = [{"summary": i["summary"], "due": i.get("due"),
                     "description": i.get("description")} for i in await items()]
        print(f"  snapshot: {len(snapshot)} pending item(s) preserved")
        preexisting = {i["uid"] for i in await items()}
        fails = 0

        def check(label, ok, detail=""):
            nonlocal fails
            fails += not ok
            print(f"  {'ok  ' if ok else 'FAIL'}  {label}" + ("" if ok else f"   {detail}"))

        try:
            # --- 1. a series fires and schedules exactly one successor
            due = (datetime.now(TZ) + timedelta(seconds=LEAD)).replace(microsecond=0)
            await add("zzz recurring probe", due, "repeat:daily")
            # The original is pending from the instant it is added, so waiting
            # for "a pending item with this summary" finds the original and
            # calls it the successor. Wait for the ORIGINAL UID to complete.
            orig_uid = next(i["uid"] for i in await items("needs_action")
                            if i["summary"] == "zzz recurring probe")
            print(f"\n  added a daily series due {due.isoformat()} (uid {orig_uid[:8]}), waiting...")
            deadline = asyncio.get_event_loop().time() + WATCH
            successor = None
            while asyncio.get_event_loop().time() < deadline:
                await asyncio.sleep(10)
                if any(i["uid"] == orig_uid for i in await items("completed")):
                    successor = next((i for i in await items("needs_action")
                                      if i["summary"] == "zzz recurring probe"
                                      and i["uid"] != orig_uid), None)
                    break
            done = [i for i in await items("completed") if i["uid"] == orig_uid]

            check("the fired occurrence was completed", bool(done))
            check("exactly one successor was scheduled", successor is not None
                  and len([i for i in await items("needs_action")
                           if i["summary"] == "zzz recurring probe"]) == 1)
            if successor:
                got = datetime.fromisoformat(successor["due"])
                want = due + timedelta(days=1)
                # Computed from the DUE time, so it must land exactly a day on
                # -- not a day after whenever the dispatcher happened to run.
                check("successor is due exactly one day after the original",
                      abs((got - want).total_seconds()) < 2,
                      f"{got.isoformat()} vs {want.isoformat()}")
                check("successor kept the repeat marker",
                      successor.get("description") == "repeat:daily",
                      repr(successor.get("description")))

            # --- 2. catch-up: an overdue series must not fire once per missed day
            stale_due = (datetime.now(TZ) - timedelta(days=3)).replace(microsecond=0)
            await add("zzz catchup probe", stale_due, "repeat:daily")
            print(f"\n  added a series 3 days overdue ({stale_due.isoformat()}), waiting...")
            stale_uid = next(i["uid"] for i in await items("needs_action")
                             if i["summary"] == "zzz catchup probe")
            deadline = asyncio.get_event_loop().time() + WATCH
            catch = None
            while asyncio.get_event_loop().time() < deadline:
                await asyncio.sleep(10)
                if any(i["uid"] == stale_uid for i in await items("completed")):
                    catch = next((i for i in await items("needs_action")
                                  if i["summary"] == "zzz catchup probe"
                                  and i["uid"] != stale_uid), None)
                    break
            if catch:
                got = datetime.fromisoformat(catch["due"])
                check("overdue series jumps to a FUTURE occurrence",
                      got > datetime.now(TZ), got.isoformat())
                check("and lands on the original time of day",
                      (got.hour, got.minute) == (stale_due.hour, stale_due.minute),
                      f"{got.hour}:{got.minute:02d} vs {stale_due.hour}:{stale_due.minute:02d}")
                check("only one catch-up occurrence exists",
                      len([i for i in await items("needs_action")
                           if i["summary"] == "zzz catchup probe"]) == 1)
            else:
                check("overdue series produced a successor", False, "none appeared")

        finally:
            for i in await items():
                if i["summary"].startswith("zzz "):
                    await drop([i["uid"]])
            now_uids = {i["uid"] for i in await items()}
            restored = [{"summary": i["summary"], "due": i.get("due"),
                         "description": i.get("description")} for i in await items()]
            same = sorted(map(str, restored)) == sorted(map(str, snapshot))
            print(f"\n  live list back as it was: {same}")
            for i in restored:
                print(f"      {i['summary']!r} due={i['due']}")
            check("nothing real was disturbed", same)

        print("\nFAILURES:", fails)

asyncio.run(main())
