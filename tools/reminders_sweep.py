"""Sweep hundreds of plausible reminder phrasings to find gaps before a person does.

Six grammar gaps have been found the same way: someone says a normal sentence,
it fails out loud, the transcript gets pasted in, one word turns out to have
been mandatory or missing. Every one was trivial to fix and free to find --
afterwards. This finds them beforehand.

Two kinds of problem are reported, and the second is the dangerous one:

  MISS     nothing matched. On the app's pipeline (built-in agent, no LLM) that
           is a hard error; on the satellite's it silently reaches the model.

  SUSPECT  something matched, but a time-looking phrase ended up inside the
           {task} wildcard -- e.g. task="at 10am to break this". That is a
           reminder scheduled at the WRONG TIME while sounding perfectly
           successful, which is the worst failure this system can produce.

Not every MISS is a bug. Some generated sentences are genuinely ambiguous or
nonsense, and refusing them is correct. Read the report, decide, and pin the
verdict in tools/reminders_check.py so it cannot regress.

    python -m venv .venv && . .venv/bin/activate
    pip install -r requirements-dev.txt
    python tools/reminders_sweep.py
"""

import json, os, re, yaml
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
METADATA_CUSTOM_SENTENCE = "hass_custom_sentence"

base = json.load(open(STOCK))
custom = yaml.safe_load(open(NEW))
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

# --- the axes people actually vary -------------------------------------------
VERBS = [
    "remind me", "set a reminder", "set reminder", "create a reminder",
    "create reminder", "add a reminder", "add reminder", "make a reminder",
    "schedule a reminder", "put a reminder", "set me a reminder",
    "new reminder", "can you remind me", "please remind me",
]
CONNECTORS = ["to", "that", ",", "", "about", "for"]
ABS_TIMES = [
    "at 2 pm", "at 2pm", "at 2 p.m.", "at 2:30 pm", "at 2:30pm", "at 2.30 pm",
    "at 2.30", "at 14:30", "at 2", "for 2 pm", "at half past two",
    "at quarter past two", "at two pm", "at 2 o'clock", "at noon", "at midnight",
]
REL_TIMES = [
    "in 20 minutes", "in twenty minutes", "in 20 mins", "in 20min",
    "in an hour", "in half an hour", "in a few minutes", "in 2 hours",
    "in 3 days", "in three days", "in a week", "in 2 weeks",
]
DAY_TIMES = [
    "tomorrow morning", "this evening", "tonight", "in the morning",
    "tomorrow at 9", "on friday", "friday evening", "on friday at 9 pm",
    "next tuesday", "on the weekend", "this weekend", "on saturday morning",
]
TASKS = ["do laundry", "call the bank", "it's move-in time"]

# Time-looking things that must never end up inside {task}.
TIMEISH = re.compile(
    r"\b(\d{1,2}[:.]?\d{0,2}\s*(am|pm|a\.m\.|p\.m\.)?|am|pm|a\.m\.|p\.m\.|noon|midnight"
    r"|minutes?|mins?|hours?|days?|weeks?|tomorrow|tonight|morning|afternoon|evening"
    r"|monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b",
    re.I,
)

cases = []  # (category, text)


def add(cat, text):
    cases.append((cat, " ".join(text.split()).replace(" ,", ",")))


for v in VERBS:
    for t in ABS_TIMES[:6]:
        for c in CONNECTORS:
            add("absolute, time first", f"{v} {t} {c} do laundry")
for v in VERBS[:8]:
    for t in REL_TIMES:
        add("relative, time first", f"{v} {t} to check the oven")
        add("relative, task first", f"{v} check the oven {t}")
for v in VERBS[:8]:
    for t in DAY_TIMES:
        add("daypart / weekday", f"{v} {t} to call the bank")
        add("daypart / weekday, task first", f"{v} call the bank {t}")
for t in ABS_TIMES:
    add("absolute spellings", f"remind me {t} to do laundry")
    add("absolute spellings, that", f"remind me {t} that the oven is on")
for t in REL_TIMES:
    for at in ["at 11 am", "at 11am", "in the morning"]:
        add("relative date + absolute time", f"remind me {t} {at} to move in")
for task in TASKS:
    add("task with apostrophes", f"remind me at 9 pm that {task}")
for x in ["cancel", "delete", "remove", "forget", "clear", "drop", "scrap"]:
    for y in ["reminders", "all reminders", "all my reminders", "my reminders",
              "every reminder", "the next reminder", "my next reminder",
              "the reminder to do laundry", "the reminder about laundry",
              "the do laundry reminder"]:
        add("cancel", f"{x} {y}")
for x in ["what are my reminders", "what reminders do i have", "list my reminders",
          "show me my reminders", "show my reminders", "read my reminders",
          "tell me my reminders", "tell me about my reminders",
          "do i have any reminders", "what's on my reminder list",
          "any reminders", "what have i got coming up", "read out my reminders"]:
    add("list", x)
for x in ["snooze", "snooze that", "snooze for 5 minutes", "snooze 5 minutes",
          "remind me again in 10 minutes", "remind me again later",
          "remind me in 10 minutes again", "postpone that", "snooze it",
          "give me 10 more minutes"]:
    add("snooze", x)

seen, uniq = set(), []
for cat, text in cases:
    if text not in seen:
        seen.add(text)
        uniq.append((cat, text))

misses, suspects, ok = {}, [], 0
for cat, text in uniq:
    r = recognize_best(
        text, intents, slot_lists=slot_lists,
        best_metadata_key=METADATA_CUSTOM_SENTENCE, best_slot_name="name",
    )
    if r is None:
        misses.setdefault(cat, []).append(text)
        continue
    ok += 1
    slots = {k: v.value for k, v in r.entities.items()}
    task = slots.get("task")
    if isinstance(task, str) and TIMEISH.search(task):
        suspects.append((text, r.intent.name, task.strip()))

print(f"swept {len(uniq)} phrasings: {ok} matched, {len(uniq) - ok} missed\n")

print("=" * 72)
print("SUSPECT -- matched, but a time landed inside the task (wrong-time risk)")
print("=" * 72)
if not suspects:
    print("  none")
for text, intent, task in suspects:
    print(f"  {text!r}\n      {intent}  task={task!r}")

print()
print("=" * 72)
print("MISS -- nothing matched")
print("=" * 72)
for cat in sorted(misses):
    print(f"\n  [{cat}]  {len(misses[cat])}")
    for text in misses[cat]:
        print(f"      {text!r}")

# Only SUSPECTs count as failures. A MISS needs a human verdict -- plenty of
# generated strings are nonsense and refusing them is correct -- but a time
# inside a task is never acceptable, so that is what a runner should gate on.
print(f"\nswept {len(uniq)}, matched {ok}, missed {len(uniq) - ok}, suspects {len(suspects)}")
print(f"FAILURES: {len(suspects)}")
