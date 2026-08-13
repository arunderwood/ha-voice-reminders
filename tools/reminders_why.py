"""Ask why a particular sentence did or did not match, offline, in ~1 second.

The standing debugging loop for this project: something misbehaves in the Assist
transcript, you copy the exact text out of it, and you run it through here. A
miss is never silent -- it gets picked up by some built-in {name} sentence and
comes back as an error -- so the failing string is always visible and always
testable.

    python -m venv .venv && . .venv/bin/activate
    pip install -r requirements-dev.txt
    python tools/reminders_why.py "remind me at 2 p.m. to do laundry"

Reports the winning intent and every slot it filled, or NO MATCH. NO MATCH means
the sentence reached the conversation agent's fallback instead: an LLM, which
answers plausibly and schedules nothing, or a plain error.

Quote each sentence. Six of this project's grammar bugs were found by pasting a
transcript line in here, and every one turned out to be a single word the
grammar treated as mandatory or had never been taught.
"""

import json, os, sys, yaml
from hassil import Intents, TextSlotList
from hassil.recognize import recognize_best

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

from hassil.util import merge_dict
merge_dict(base, custom)
intents = Intents.from_dict(base)

slot_lists = {
    "area": TextSlotList.from_strings(["kitchen", "living room", "bedroom"]),
    "floor": TextSlotList.from_strings(["upstairs", "downstairs"]),
    "name": TextSlotList.from_strings(["Kitchen Lights", "Desk Lamp", "Fan"]),
}

for text in sys.argv[1:]:
    r = recognize_best(
        text, intents, slot_lists=slot_lists,
        best_metadata_key=METADATA_CUSTOM_SENTENCE, best_slot_name="name",
    )
    print(f"\n{text!r}")
    if r is None:
        print("    NO MATCH  -> falls through to the LLM")
        continue
    slots = {k: v.value for k, v in r.entities.items() if not k.startswith("_")}
    slots = {k: (v.strip() if isinstance(v, str) else v) for k, v in slots.items()}
    print(f"    {r.intent.name}")
    for k, v in sorted(slots.items()):
        print(f"        {k} = {v!r}")
