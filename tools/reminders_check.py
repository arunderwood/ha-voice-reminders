"""Check the reminder grammar against hassil without restarting Home Assistant.

Custom sentences only load at startup, so iterating on them through push.sh
--restart costs a minute a try. This runs the same matcher default_agent uses,
against the same stock intent data, in about a second.

Run it inside the container (hassil and the intent data are HA's own):

    python -m venv .venv && . .venv/bin/activate
    pip install -r requirements-dev.txt
    python tools/reminders_check.py

In practice push the sentences file first, then:

    python -m venv .venv && . .venv/bin/activate
    pip install -r requirements-dev.txt
    python tools/reminders_check.py

Checks two things the grammar can get wrong in ways that are invisible until you
are standing in the room talking to a speaker:
  - which intent won, including whether the built-in HassStartTimer stole a
    task-first phrasing (see the header of the sentences file)
  - what landed in each slot, especially the {task} wildcard's boundaries
"""

import json, os, yaml
from hassil import Intents, TextSlotList
from hassil.recognize import recognize_best
from hassil.util import merge_dict

def _stock_intents_path():
    """Locate the shipped English intent data.

    Prefers the installed `home_assistant_intents` package, so these run in a
    plain venv (`pip install -r requirements-dev.txt`) with no Home Assistant
    and no container. Falls back to the path inside the HA container image.
    """
    try:
        import home_assistant_intents as _hai
        import os as _os
        p = _os.path.join(_os.path.dirname(_hai.__file__), "data", "en.json")
        if _os.path.exists(p):
            return p
    except ImportError:
        pass
    import glob as _glob
    hits = _glob.glob("/usr/local/lib/python3*/site-packages/"
                      "home_assistant_intents/data/en.json")
    if hits:
        return hits[0]
    raise SystemExit("cannot find home_assistant_intents data; "
                     "pip install -r requirements-dev.txt")


STOCK = _stock_intents_path()
NEW = os.environ.get("SENTENCES",
                     os.path.join(os.path.dirname(__file__), "..",
                                  "custom_sentences", "en", "reminders.yaml"))
METADATA_CUSTOM_SENTENCE = "hass_custom_sentence"  # default_agent.py

base = json.load(open(STOCK))
custom = yaml.safe_load(open(NEW))

# Replicate default_agent's tagging of custom sentences before merging.
for intent_dict in custom.get("intents", {}).values():
    for intent_data in intent_dict.get("data", []):
        intent_data.setdefault("metadata", {})[METADATA_CUSTOM_SENTENCE] = True

merge_dict(base, custom)
intents = Intents.from_dict(base)

slot_lists = {
    "area": TextSlotList.from_strings(["kitchen", "living room", "bedroom"]),
    "floor": TextSlotList.from_strings(["upstairs", "downstairs"]),
    "name": TextSlotList.from_strings(["Kitchen Lights", "Desk Lamp", "Fan"]),
}

A, R = "SetReminderAbsolute", "SetReminderRelative"
D = "SetReminderDaypart"
W, S = "SetReminderWeekday", "SnoozeReminder"

# (text, expected intent, expected slot subset)
TESTS = [
    # --- absolute, time first
    ("remind me at 2 pm to do laundry", A, {"hour": 2, "meridiem": "pm", "task": "do laundry"}),
    ("remind me at 2 to do laundry", A, {"hour": 2, "task": "do laundry"}),
    ("remind me at 2:30 pm to do laundry", A, {"hour": 2, "minute": 30, "meridiem": "pm", "task": "do laundry"}),
    ("remind me at 14:30 to do laundry", A, {"hour": 14, "minute": 30, "task": "do laundry"}),
    ("remind me at 7 in the morning to take the bins out", A, {"hour": 7, "meridiem": "am", "task": "take the bins out"}),
    ("set a reminder for 9 pm to lock the door", A, {"hour": 9, "meridiem": "pm", "task": "lock the door"}),
    ("create a reminder at 6 am to leave", A, {"hour": 6, "meridiem": "am", "task": "leave"}),
    # --- absolute, task first
    ("remind me to do laundry at 2 pm", A, {"hour": 2, "meridiem": "pm", "task": "do laundry"}),
    ("set a reminder to lock the door at 9 pm", A, {"hour": 9, "meridiem": "pm", "task": "lock the door"}),
    # The task itself contains "at" -- the wildcard must not stop early.
    ("remind me to look at the oven at 5 pm", A, {"hour": 5, "meridiem": "pm", "task": "look at the oven"}),
    # --- relative, time first
    ("remind me in 20 minutes to check the oven", R, {"count": 20, "unit": "minutes", "task": "check the oven"}),
    ("remind me in 2 hours to check the oven", R, {"count": 2, "unit": "hours", "task": "check the oven"}),
    ("remind me in 1 minute to stir the pot", R, {"count": 1, "unit": "minutes", "task": "stir the pot"}),
    ("remind me in 90 minutes to stretch", R, {"count": 90, "unit": "minutes", "task": "stretch"}),
    # --- relative, task first: these are the ones HassStartTimer tries to steal
    ("remind me to check the oven in 20 minutes", R, {"count": 20, "unit": "minutes", "task": "check the oven"}),
    ("remind me to do laundry in 2 hours", R, {"count": 2, "unit": "hours", "task": "do laundry"}),
    ("set a reminder to stretch in 30 minutes", R, {"count": 30, "unit": "minutes", "task": "stretch"}),
    # --- fuzzy dayparts, kept off the LLM path on purpose
    ("remind me tomorrow morning to call the bank", D, {"dayref": "1", "hour": "8", "task": "call the bank"}),
    ("remind me tomorrow afternoon to book the flights", D, {"dayref": "1", "hour": "14", "task": "book the flights"}),
    ("remind me tomorrow evening to take the bins out", D, {"dayref": "1", "hour": "19", "task": "take the bins out"}),
    ("remind me this evening to take the bins out", D, {"dayref": "0", "hour": "19", "task": "take the bins out"}),
    ("remind me this afternoon to call back", D, {"dayref": "0", "hour": "14", "task": "call back"}),
    ("remind me tonight to lock up", D, {"dayref": "0", "hour": "19", "task": "lock up"}),
    ("remind me in the morning to call the bank", D, {"dayref": "0", "hour": "8", "task": "call the bank"}),
    ("remind me at noon to eat", D, {"hour": "12", "task": "eat"}),
    ("set a reminder tomorrow morning to call the bank", D, {"dayref": "1", "hour": "8", "task": "call the bank"}),
    ("remind me to call the bank tomorrow morning", D, {"dayref": "1", "hour": "8", "task": "call the bank"}),
    ("remind me to lock up tonight", D, {"dayref": "0", "hour": "19", "task": "lock up"}),

    # --- typed and phone-dictated spellings. The satellite hears
    # faster-whisper's "2 PM"; a phone keyboard or on-device dictation produces
    # "2pm", "2 p.m." or "2:30pm". Same words, and they must take the same path
    # -- falling through to the LLM here is what made reminders look broken in
    # the mobile app while working at the speaker.
    ("remind me at 2pm to do laundry", A, {"hour": 2, "meridiem": "pm", "task": "do laundry"}),
    ("remind me at 2 p.m. to do laundry", A, {"hour": 2, "meridiem": "pm", "task": "do laundry"}),
    ("remind me at 2:30pm to do laundry", A, {"hour": 2, "minute": 30, "meridiem": "pm", "task": "do laundry"}),
    ("remind me at 2:30 p.m. to do laundry", A, {"hour": 2, "minute": 30, "meridiem": "pm", "task": "do laundry"}),
    ("remind me at 7 a.m. to take the bins out", A, {"hour": 7, "meridiem": "am", "task": "take the bins out"}),
    ("remind me at 7am to take the bins out", A, {"hour": 7, "meridiem": "am", "task": "take the bins out"}),
    ("remind me in 20min to check the oven", R, {"count": 20, "unit": "minutes", "task": "check the oven"}),

    # --- verb variants. "Set reminder" without the article is what gets typed,
    # and it used to match a built-in {name} sentence and answer "There is no
    # reminder for 9 p.m. to check."
    ("Set reminder for 9 p.m. to check the transfer.", A, {"hour": 9, "meridiem": "pm", "task": "check the transfer"}),
    ("set reminder at 9 pm to check the transfer", A, {"hour": 9, "meridiem": "pm", "task": "check the transfer"}),
    ("set me a reminder for 9 pm to check the transfer", A, {"hour": 9, "meridiem": "pm", "task": "check the transfer"}),
    ("add reminder for 9 pm to check the transfer", A, {"hour": 9, "meridiem": "pm", "task": "check the transfer"}),
    ("schedule a reminder for 9 pm to check the transfer", A, {"hour": 9, "meridiem": "pm", "task": "check the transfer"}),
    ("create reminder in 20 minutes to check the oven", R, {"count": 20, "unit": "minutes", "task": "check the oven"}),
    ("set reminder tomorrow morning to call the bank", D, {"dayref": "1", "hour": "8", "task": "call the bank"}),

    # --- dot-separated times. "10.30" is an ordinary way to write half past
    # ten, and it matched nothing at all -- not a wrong time, no match, so the
    # reply was the no_intent line.
    ("Set a reminder for 10.30 to check the laundry.", A, {"hour": 10, "minute": 30, "task": "check the laundry"}),
    ("Set a reminder for 10.30 p.m. to check the laundry.", A, {"hour": 10, "minute": 30, "meridiem": "pm", "task": "check the laundry"}),
    ("remind me at 2.30 pm to do laundry", A, {"hour": 2, "minute": 30, "meridiem": "pm", "task": "do laundry"}),
    ("remind me at 7.15am to take the bins out", A, {"hour": 7, "minute": 15, "meridiem": "am", "task": "take the bins out"}),

    # --- multi-day relative offsets
    ("remind me in 3 days to water the plants", R, {"count": 3, "unit": "days", "task": "water the plants"}),
    ("remind me in 2 weeks to book the flights", R, {"count": 2, "unit": "weeks", "task": "book the flights"}),
    ("remind me to water the plants in 3 days", R, {"count": 3, "unit": "days", "task": "water the plants"}),

    # --- named weekdays
    ("remind me friday evening to call mum", W, {"weekday": "4", "hour": "19", "task": "call mum"}),
    ("remind me on friday morning to call mum", W, {"weekday": "4", "hour": "8", "task": "call mum"}),
    ("remind me on friday at 9 pm to call mum", W, {"weekday": "4", "hour": 9, "meridiem": "pm", "task": "call mum"}),
    ("remind me on friday at 9:30 pm to call mum", W, {"weekday": "4", "hour": 9, "minute": 30, "meridiem": "pm", "task": "call mum"}),
    ("remind me on friday to call mum", W, {"weekday": "4", "task": "call mum"}),
    ("remind me next tuesday to renew the domain", W, {"weekday": "1", "task": "renew the domain"}),
    ("remind me to call mum on friday evening", W, {"weekday": "4", "hour": "19", "task": "call mum"}),
    ("set a reminder on sunday morning to do laundry", W, {"weekday": "6", "hour": "8", "task": "do laundry"}),

    # --- snooze
    ("snooze", S, {"count": "10", "unit": "minutes"}),
    ("snooze that", S, {"count": "10", "unit": "minutes"}),
    ("snooze for 5 minutes", S, {"count": 5, "unit": "minutes"}),
    ("snooze that for 20 minutes", S, {"count": 20, "unit": "minutes"}),
    ("remind me again in 15 minutes", S, {"count": 15, "unit": "minutes"}),
    ("remind me again in 2 hours", S, {"count": 2, "unit": "hours"}),
    ("remind me again later", S, {"count": "10", "unit": "minutes"}),

    # --- relative date with an absolute time. This exact utterance went to
    # the LLM, which scheduled it correctly and then said the wrong time aloud.
    ("Remind me in three days at 10am to check the thing.", "SetReminderRelativeAt",
     {"count": 3, "unit": "days", "hour": 10, "meridiem": "am", "task": "check the thing"}),
    ("remind me in 2 weeks at 9 pm to renew it", "SetReminderRelativeAt",
     {"count": 2, "unit": "weeks", "hour": 9, "meridiem": "pm", "task": "renew it"}),
    ("remind me in three days in the morning to check the thing", "SetReminderRelativeAt",
     {"count": 3, "unit": "days", "hour": "8", "task": "check the thing"}),
    ("remind me to check the thing in 3 days at 10 am", "SetReminderRelativeAt",
     {"count": 3, "unit": "days", "hour": 10, "meridiem": "am", "task": "check the thing"}),
    # Nonsense combination must NOT parse rather than parse wrongly.
    ("remind me in 20 minutes at 10am to break this", None, {}),

    ("tell me about my reminders", "ListReminders", {}),
    ("tell me about reminders", "ListReminders", {}),

    # --- the task connector. "to" is not the only one: people say "that", and
    # sometimes a comma stands in for it entirely. All three of these came
    # straight from the Assist transcript after they failed out loud.
    ("Remind me in 3 days at 11 a.m., time to move in.", "SetReminderRelativeAt",
     {"count": 3, "unit": "days", "hour": 11, "meridiem": "am", "task": "time to move in"}),
    ("Remind me in 3 days at 11am that it's move-in time.", "SetReminderRelativeAt",
     {"count": 3, "unit": "days", "hour": 11, "meridiem": "am", "task": "it's move-in time"}),
    ("remind me at 2 pm that the oven is on", A, {"hour": 2, "meridiem": "pm", "task": "the oven is on"}),
    ("remind me that the bins go out tomorrow morning", D,
     {"dayref": "1", "hour": "8", "task": "the bins go out"}),
    # The optional connector must not swallow "to" into the task.
    ("remind me at 2 pm to do laundry", A, {"hour": 2, "meridiem": "pm", "task": "do laundry"}),
    ("remind me in 3 days at 11 am to move in", "SetReminderRelativeAt",
     {"count": 3, "unit": "days", "hour": 11, "meridiem": "am", "task": "move in"}),

    # "all" is not mandatory -- people just say "cancel reminders".
    ("Cancel reminders.", "CancelAllReminders", {}),
    ("cancel my reminders", "CancelAllReminders", {}),

    # --- found by tools/reminders_sweep.py rather than by a person hitting
    # them out loud. Pinned here so the sweep's findings cannot erode.
    ("remind me tomorrow at 9 to call the bank", D, {"dayref": "1", "hour": 9, "task": "call the bank"}),
    ("remind me today at 6:30 pm to eat", D, {"dayref": "0", "hour": 6, "minute": 30, "meridiem": "pm", "task": "eat"}),
    ("remind me at half past two to do laundry", A, {"hour": 2, "minute": "30", "task": "do laundry"}),
    ("remind me at quarter past two to do laundry", A, {"hour": 2, "minute": "15", "task": "do laundry"}),
    ("remind me at quarter to three to do laundry", A, {"hour": 3, "minute": "45", "hourshift": "-1", "task": "do laundry"}),
    ("remind me on the weekend to call the bank", W, {"weekday": "5", "task": "call the bank"}),
    ("remind me this weekend to call the bank", W, {"weekday": "5", "task": "call the bank"}),
    ("remind me in an hour to check the oven", R, {"count": "1", "unit": "hours", "task": "check the oven"}),
    ("remind me in half an hour to check the oven", R, {"count": "30", "unit": "minutes", "task": "check the oven"}),
    ("remind me in a few minutes to check the oven", R, {"count": "5", "unit": "minutes", "task": "check the oven"}),
    ("remind me in a week to check the oven", R, {"count": "1", "unit": "weeks", "task": "check the oven"}),
    ("new reminder at 2 pm to do laundry", A, {"hour": 2, "meridiem": "pm", "task": "do laundry"}),
    # Task-first with no connector at all.
    ("remind me call the bank tomorrow morning", D, {"dayref": "1", "hour": "8", "task": "call the bank"}),
    ("remind me check the oven in 20 minutes", R, {"count": 20, "unit": "minutes", "task": "check the oven"}),
    ("clear the next reminder", "CancelNextReminder", {}),
    ("clear the reminder to do laundry", "CancelReminder", {"task": "do laundry"}),
    ("show me my reminders", "ListReminders", {}),
    ("read out my reminders", "ListReminders", {}),
    ("postpone that", S, {"count": "10", "unit": "minutes"}),
    ("snooze it", S, {"count": "10", "unit": "minutes"}),
    ("give me 10 more minutes", S, {"count": 10, "unit": "minutes"}),

    # --- recurring
    ("remind me every day at 10:30 to give you the medicine", "SetReminderRecurring",
     {"freq": "daily", "hour": 10, "minute": 30, "task": "give you the medicine"}),
    ("remind me every weekday at 8 am to leave", "SetReminderRecurring",
     {"freq": "weekdays", "hour": 8, "meridiem": "am", "task": "leave"}),
    ("remind me every morning to check the thing", "SetReminderRecurring",
     {"freq": "daily", "hour": "8", "task": "check the thing"}),
    ("remind me every tuesday at 9 am to take the bins out", "SetReminderRecurring",
     {"freq": "weekly", "weekday": "1", "hour": 9, "meridiem": "am", "task": "take the bins out"}),
    ("remind me every tuesday morning to take the bins out", "SetReminderRecurring",
     {"freq": "weekly", "weekday": "1", "hour": "8", "task": "take the bins out"}),
    ("remind me every tuesday to take the bins out", "SetReminderRecurring",
     {"freq": "weekly", "weekday": "1", "task": "take the bins out"}),
    ("remind me every weekday morning to leave", "SetReminderRecurring",
     {"freq": "weekdays", "hour": "8", "task": "leave"}),
    ("remind me to take the bins out every tuesday", "SetReminderRecurring",
     {"freq": "weekly", "weekday": "1", "task": "take the bins out"}),
    # A one-off must NOT be swallowed by the recurring grammar.
    ("remind me at 10:30 to give you the medicine", A,
     {"hour": 10, "minute": 30, "task": "give you the medicine"}),

    # --- reading the list back
    ("what are my reminders", "ListReminders", {}),
    ("what are my upcoming reminders", "ListReminders", {}),
    ("which reminders do i have", "ListReminders", {}),
    ("list my reminders", "ListReminders", {}),
    ("read me my reminders", "ListReminders", {}),
    ("do i have any reminders", "ListReminders", {}),
    ("tell me my reminders", "ListReminders", {}),

    # --- cancelling. "next" must reach CancelNextReminder rather than being
    # swallowed as a task by the {rem_task} wildcard, and "all" likewise.
    ("cancel the reminder to do laundry", "CancelReminder", {"task": "do laundry"}),
    ("delete the reminder about the transfer", "CancelReminder", {"task": "the transfer"}),
    ("forget my reminder to call the bank", "CancelReminder", {"task": "call the bank"}),
    ("cancel the do laundry reminder", "CancelReminder", {"task": "do laundry"}),
    ("cancel my next reminder", "CancelNextReminder", {}),
    ("cancel the next reminder", "CancelNextReminder", {}),
    ("cancel all my reminders", "CancelAllReminders", {}),
    ("cancel all reminders", "CancelAllReminders", {}),
    ("clear all my reminders", "CancelAllReminders", {}),
    ("delete every reminder", "CancelAllReminders", {}),

    # --- opening the list on the phone must not steal the speaking variants
    ("open my reminders", "OpenReminderList", {}),
    ("open my to-do list", "OpenReminderList", {}),
    ("open the todo list", "OpenReminderList", {}),
    ("pull up my reminders", "OpenReminderList", {}),
    ("show me my reminders on my phone", "OpenReminderList", {}),
    ("show me my reminders", "ListReminders", {}),

    # --- must NOT be captured: real timers stay timers
    ("set a timer for 20 minutes", "HassStartTimer", {}),
    ("set a 5 minute timer", "HassStartTimer", {}),
    ("cancel the timer", "HassCancelTimer", {}),
    ("cancel all timers", "HassCancelAllTimers", {}),
]

fails = 0
for text, want_intent, want_slots in TESTS:
    r = recognize_best(
        text, intents, slot_lists=slot_lists,
        best_metadata_key=METADATA_CUSTOM_SENTENCE, best_slot_name="name",
    )
    # want_intent None means "this must NOT parse" -- for combinations that are
    # nonsense, where matching wrongly is worse than not matching at all.
    if want_intent is None:
        ok = r is None
        fails += not ok
        print(f"  {'ok  ' if ok else 'FAIL'}  {'(no match, as wanted)' if ok else r.intent.name:20} {text!r}")
        continue
    if r is None:
        print(f"  FAIL  NO MATCH            {text!r}")
        fails += 1
        continue
    got = {k: v.value for k, v in r.entities.items()}
    # Wildcards keep their trailing whitespace; the intent_script trims too.
    got = {k: (v.strip() if isinstance(v, str) else v) for k, v in got.items()}
    bad = [k for k, v in want_slots.items() if got.get(k) != v]
    ok = r.intent.name == want_intent and not bad
    fails += not ok
    shown = {k: v for k, v in got.items() if not k.startswith("_")}
    print(f"  {'ok  ' if ok else 'FAIL'}  {r.intent.name:20} {text!r}")
    if not ok:
        print(f"          wanted {want_intent} {want_slots}")
        print(f"          got    {shown}")

print("\nFAILURES:", fails)
