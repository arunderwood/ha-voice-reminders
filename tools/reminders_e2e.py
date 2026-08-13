"""End-to-end check for the reminder grammar (PLAN.md phase 3 acceptance).

tools/reminders_check.py proves hassil matches the sentences. This proves the
whole path: pipeline -> intent_script -> todo.add_item, with the resulting
due_datetime compared against the expected instant computed independently here
in Python. A grammar that matches but computes 2am instead of 2pm passes the
first check and fails this one.

Runs every utterance through a real Assist pipeline (the preferred one unless
PIPELINE is set), so it also asserts each one stayed on the fast local path
rather than falling through to a conversation agent.

Creates real items in todo.reminders and removes them again at the end.

    export HA_URL=http://homeassistant.local:8123
    export HA_TOKEN=...        # Profile -> Long-lived access tokens
    python tools/reminders_e2e.py
"""

import asyncio, json, os, aiohttp
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

TOKEN = os.environ["HA_TOKEN"]
BASE = os.environ.get("HA_URL", "http://localhost:8123")
PIPELINE = os.environ.get("PIPELINE")  # None = the preferred pipeline
ENT = os.environ.get("REMINDER_LIST", "todo.reminders")
TZ = ZoneInfo(os.environ.get("TZ_NAME", "America/Chicago"))


def absolute(now, hour, minute=0, meridiem=""):
    """Mirror of the SetReminderAbsolute due template.

    Written independently of the Jinja rather than ported line by line -- the
    point is to catch the template being wrong, so agreeing with it by
    construction would defeat the check.
    """
    explicit = meridiem != "" or hour > 12
    if explicit:
        if meridiem == "pm" and hour < 12:
            hours = [hour + 12]
        elif meridiem == "am" and hour == 12:
            hours = [0]
        else:
            hours = [hour]
    else:
        base = hour % 12
        hours = [base, base + 12]

    best = None
    for day in (0, 1):
        for hh in hours:
            c = now.replace(hour=hh, minute=minute, second=0, microsecond=0)
            c += timedelta(days=day)
            if c > now and (explicit or hh >= 6) and (best is None or c < best):
                best = c
    return best


def daypart(now, hour, dayref=0):
    """Mirror of the SetReminderDaypart due template."""
    t = now.replace(hour=hour, minute=0, second=0, microsecond=0)
    t += timedelta(days=dayref)
    if t <= now:
        t += timedelta(days=1)
    return t


def relative(now, count, unit):
    """Mirror of the SetReminderRelative due template."""
    delta = timedelta(hours=count) if unit == "hours" else timedelta(minutes=count)
    return (now + delta).replace(microsecond=0)


# (utterance, expected-due function)
TESTS = [
    ("remind me at 2 pm to do laundry",              lambda n: absolute(n, 2, 0, "pm")),
    ("remind me at 2:30 pm to do laundry",           lambda n: absolute(n, 2, 30, "pm")),
    ("remind me at 14:30 to do laundry",             lambda n: absolute(n, 14, 30)),
    ("remind me at 7 in the morning to take the bins out", lambda n: absolute(n, 7, 0, "am")),
    ("set a reminder for 9 pm to lock the door",     lambda n: absolute(n, 9, 0, "pm")),
    ("remind me to do laundry at 2 pm",              lambda n: absolute(n, 2, 0, "pm")),
    ("remind me to look at the oven at 5 pm",        lambda n: absolute(n, 5, 0, "pm")),
    # Bare hours: must pick the next DAYTIME occurrence. Depending on the time
    # of day these land today or tomorrow, which is why the expectation is
    # computed rather than hard-coded.
    ("remind me at 2 to do laundry",                 lambda n: absolute(n, 2)),
    ("remind me at 6 to start dinner",               lambda n: absolute(n, 6)),
    ("remind me at 11 to take my pills",             lambda n: absolute(n, 11)),
    ("remind me in 20 minutes to check the oven",    lambda n: relative(n, 20, "minutes")),
    ("remind me in 2 hours to check the oven",       lambda n: relative(n, 2, "hours")),
    ("remind me in 90 minutes to stretch",           lambda n: relative(n, 90, "minutes")),
    ("remind me to check the oven in 20 minutes",    lambda n: relative(n, 20, "minutes")),
    ("remind me to do laundry in 2 hours",           lambda n: relative(n, 2, "hours")),
    # Fuzzy dayparts. These are the phrasings the LLM fabricated confirmations
    # for in 2 of 6 attempts, which is why they are on this path instead.
    ("remind me tomorrow morning to call the bank",  lambda n: daypart(n, 8, 1)),
    ("remind me tomorrow afternoon to book the flights", lambda n: daypart(n, 14, 1)),
    ("remind me this evening to take the bins out",  lambda n: daypart(n, 19, 0)),
    ("remind me tonight to lock up",                 lambda n: daypart(n, 19, 0)),
    ("remind me in the morning to call the bank",    lambda n: daypart(n, 8, 0)),
    ("remind me at noon to eat",                     lambda n: daypart(n, 12, 0)),
    ("remind me to call the bank tomorrow morning",  lambda n: daypart(n, 8, 1)),
    # Phone-dictated and typed spellings. faster-whisper writes "2 PM"; a phone
    # writes "2 p.m." or "2:30pm". These reached the LLM until the grammar
    # learned them, which is why reminders worked at the speaker but not in the
    # mobile app.
    ("remind me at 2 p.m. to do laundry",            lambda n: absolute(n, 2, 0, "pm")),
    ("remind me at 2:30pm to do laundry",            lambda n: absolute(n, 2, 30, "pm")),
    ("remind me at 7 a.m. to take the bins out",     lambda n: absolute(n, 7, 0, "am")),
    ("remind me at 2pm to do laundry",               lambda n: absolute(n, 2, 0, "pm")),
]

EXPECTED_TASK = {
    "remind me to look at the oven at 5 pm": "look at the oven",
}


async def main():
    hdr = {"Authorization": f"Bearer {TOKEN}"}
    async with aiohttp.ClientSession(headers=hdr) as s, \
            s.ws_connect(BASE.replace("http", "ws") + "/api/websocket") as ws:
        await ws.receive_str()
        await ws.send_json({"type": "auth", "access_token": TOKEN})
        while json.loads(await ws.receive_str())["type"] != "auth_ok":
            pass

        async def get_items(status=None):
            body = {"entity_id": ENT}
            if status:
                body["status"] = status
            async with s.post(f"{BASE}/api/services/todo/get_items?return_response",
                              json=body) as r:
                out = await r.json()
            return out["service_response"][ENT]["items"]

        async def remove(summary):
            await s.post(f"{BASE}/api/services/todo/remove_item",
                         json={"entity_id": ENT, "item": summary})

        before = {i["uid"] for i in await get_items()}
        # `before` grows as the run proceeds, so keep a fixed record of what was
        # already there. Anything in here is a real reminder this test must not
        # touch, and must not report as its own mess.
        preexisting = set(before)
        mid, fails, created, created_uids = 0, 0, [], []

        for text, expect in TESTS:
            mid += 1
            # now() is read as close to the intent as possible; the relative
            # cases are compared with a tolerance to cover the gap.
            now = datetime.now(TZ).replace(microsecond=0)
            await ws.send_json({
                "id": mid, "type": "assist_pipeline/run",
                "start_stage": "intent", "end_stage": "intent",
                "input": {"text": text}, # Omit the key entirely when unset: the websocket schema
                # rejects a null, and absent means "the preferred pipeline".
                **({"pipeline": PIPELINE} if PIPELINE else {}),
            })
            local, speech = None, None
            while True:
                m = json.loads(await ws.receive_str())
                if m.get("id") != mid:
                    continue
                if m["type"] == "result" and not m["success"]:
                    speech = f"ERROR {m['error']}"
                    break
                if m["type"] != "event":
                    continue
                ev, d = m["event"]["type"], m["event"].get("data") or {}
                if ev == "intent-end":
                    local = d.get("processed_locally")
                    resp = d.get("intent_output", {}).get("response", {})
                    speech = (resp.get("speech", {}).get("plain", {}) or {}).get("speech")
                elif ev == "run-end":
                    break

            items = await get_items()
            new = [i for i in items if i["uid"] not in before]
            before |= {i["uid"] for i in new}

            problems = []
            if local is not True:
                problems.append(f"went to the LLM (processed_locally={local})")
            if len(new) != 1:
                problems.append(f"created {len(new)} items, expected 1")
            else:
                item = new[0]
                created.append(item["summary"])
                created_uids.append(item["uid"])
                want_task = EXPECTED_TASK.get(text)
                if want_task and item["summary"] != want_task:
                    problems.append(f"task {item['summary']!r} != {want_task!r}")
                got = datetime.fromisoformat(item["due"])
                want = expect(now)
                skew = abs((got - want).total_seconds())
                if skew > 90:
                    problems.append(f"due {got.isoformat()} != {want.isoformat()} ({skew:.0f}s off)")
            if not speech:
                problems.append("no spoken response")

            fails += bool(problems)
            print(f"  {'ok  ' if not problems else 'FAIL'}  {text!r}")
            if len(new) == 1:
                print(f"          due={new[0]['due']}  task={new[0]['summary']!r}")
            print(f"          says: {speech!r}")
            for p in problems:
                print(f"          !! {p}")

        # By UID. Removing by summary deletes whichever item matches first,
        # which may be a REAL reminder of the user's that happens to share the
        # name -- 'call the bank' and 'look at the oven' are both fixtures here
        # and both things a person actually says. That is not hypothetical: it
        # happened, and the test then reported its own survivors as leftovers.
        print(f"\n  cleaning up {len(created_uids)} items")
        for uid in created_uids:
            await remove(uid)
        # Only needs_action counts as a leftover. Completed items are real
        # reminders that have already fired and are waiting for the 04:00
        # prune -- reporting those as leftovers is just noise.
        left = await get_items("needs_action")
        done = await get_items("completed")
        mine = [i for i in left if i["uid"] not in preexisting]
        theirs = [i for i in left if i["uid"] in preexisting]
        print(f"  leftovers from this run: {len(mine)}"
              f"  (untouched real reminders: {len(theirs)},"
              f" completed awaiting prune: {len(done)})")
        for i in mine:
            print(f"      LEFTOVER: {i['summary']!r} due={i.get('due')}")
        for i in theirs:
            print(f"      preserved: {i['summary']!r} due={i.get('due')}")
        fails += len(mine) > 0
        print("\nFAILURES:", fails)

asyncio.run(main())
