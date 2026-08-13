# ha-voice-reminders

Spoken reminders for Home Assistant Assist, resolved **deterministically** — no
LLM in the path that decides what time you meant.

```
"remind me at 2:30 pm to do laundry"
"remind me in twenty minutes to check the oven"
"remind me tomorrow morning to call the bank"
"remind me every Thursday at 6 pm to put out the trash"
"what are my reminders"
"cancel the reminder to do laundry"
"snooze for 20 minutes"
```

When one comes due it is **spoken on every Assist satellite** and pushed to your
phone with Snooze buttons.

Home Assistant has no reminder intent. The adjacent pieces don't fit: timers are
relative-only, satellite-bound and carry no message; list intents carry no time.
This fills that gap with custom sentences, an `intent_script`, and a `local_todo`
list as the store.

## Why deterministic

The obvious build is to hand the sentence to an LLM and ask for a timestamp. It
does not work well:

- Asked for ISO 8601, an 8B model returned `2023-08-12T18:19:00-05:00` — the
  time of day arithmetically perfect, the **year three years wrong** (FINDINGS §14).
- In 2 of 6 attempts it skipped the tool entirely and **invented a confirmation**
  for a reminder that was never created (FINDINGS §15).

So every time is computed in Jinja from integers the grammar already validated.
An LLM fallback exists for phrasings the grammar misses, and it supplies
`hour` / `minute` / `days_from_today` — never a date.

## What it understands

| Shape | Examples |
|---|---|
| Absolute | `at 2:30 pm`, `at 2pm`, `at 2 p.m.`, `at 10.30`, `at 14:30`, `at half past two`, `at quarter to three` |
| Bare hour | `at 6` — resolves to the next *daytime* occurrence, never 6 AM tomorrow |
| Relative | `in 20 minutes`, `in twenty minutes`, `in an hour`, `in half an hour`, `in 3 days`, `in 2 weeks` |
| Relative + time | `in three days at 11 am` |
| Dayparts | `tomorrow morning`, `this evening`, `tonight`, `in the morning`, `at noon`, `tomorrow at 9` |
| Weekdays | `on Friday at 9 pm`, `Friday evening`, `next Tuesday`, `on the weekend` |
| Recurring | `every day at 10:30`, `every weekday at 8 am`, `every Thursday at 6 pm`, `every morning` |
| Managing | `what are my reminders`, `cancel the reminder to X`, `cancel my next reminder`, `cancel all reminders`, `snooze`, `snooze for 20 minutes`, `open my reminders` |

Both word orders work everywhere — `remind me to do laundry at 2 pm` as well as
`remind me at 2 pm to do laundry`. That is not decoration: the built-in
`HassStartTimer` has wildcard sentences that otherwise **steal** the task-first
phrasing and turn your reminder into a timer.

## Install

1. **Create a to-do list** for reminders — Settings → Devices & Services → Add
   integration → **Local To-do**, named `Reminders`. Or run
   `python tools/add_local_todo.py` (needs `HA_URL` and `HA_TOKEN`).

2. **Copy two files** into your Home Assistant config, keeping the paths:

   ```
   custom_sentences/en/reminders.yaml  ->  <config>/custom_sentences/en/reminders.yaml
   packages/reminders.yaml             ->  <config>/packages/reminders.yaml
   ```

   If you don't already load packages, add this to `configuration.yaml`:

   ```yaml
   homeassistant:
     packages: !include_dir_named packages
   ```

3. **Edit the two anchors** at the top of `SetReminderAbsolute` in
   `packages/reminders.yaml`:

   ```yaml
   reminders_list:   &reminder_list todo.reminders
   reminders_notify: &notify_target notify.mobile_app_your_phone
   ```

   Every other reference in the file is `*reminder_list` or `*notify_target`.
   Point `notify_target` at a [notify group] to reach several phones.

4. **Restart** Home Assistant. Custom sentences and packages only load at
   startup (though see FINDINGS §12 for the reload services that avoid this
   while you iterate).

5. **Say something.** `remind me in two minutes to test this`.

Announcing needs at least one `assist_satellite` — a Voice PE, an ESPHome
satellite, anything that reports the ANNOUNCE feature. Targets are resolved at
run time, so adopting another satellite later needs no edit here. If you have
none, the phone push still works.

[notify group]: https://www.home-assistant.io/integrations/group/#notify-groups

## Testing

Most of this repo is tests, because a voice grammar fails in ways you cannot see
from the config.

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt

./tools/check_all.sh --offline          # grammar only, ~5s, no Home Assistant

export HA_URL=http://homeassistant.local:8123
export HA_TOKEN=...                     # Profile -> Long-lived access tokens
./tools/check_all.sh                    # everything, ~10 min
```

| Suite | What it protects |
|---|---|
| `reminders_check.py` | every known phrasing reaches the right intent with the right slots |
| `reminders_sweep.py` | ~1000 generated phrasings; **fails only when a time lands inside a task** |
| `reminders_e2e.py` | due times, computed independently in Python, through a real pipeline |
| `reminders_manage_test.py` | list and cancel |
| `reminders_recurring_test.py` | a series schedules exactly one successor, without drift, and catches up after downtime |
| `reminders_dispatch_test.py` | one announce, one push, one completion, no repeat |

The sweep's second check is the one that matters. A phrasing that *matches* but
swallows the time into the task — `task = "at 10am to break this"` — schedules
the **wrong time while sounding completely successful**. That is the worst thing
this system can do, so it is the only thing the sweep treats as a failure.

Everything that touches your reminder list snapshots it first and restores it
afterwards. That is not decoration: an earlier ad-hoc version of the cancel
testing destroyed a real pending reminder.

### When a phrasing misbehaves

```bash
python tools/reminders_why.py "the exact text from the Assist transcript"
```

It reports which intent matched and every slot, or NO MATCH. Six of this
project's grammar bugs were found this way, and every one turned out to be a
single word the grammar treated as mandatory or had never been taught —
`set reminder` without "a", `10.30` with a dot, `p.m.` with periods, `that`
instead of `to`.

## Notes

**Responses are plain by default.** `examples/personality.md` shows how to give
them a character, with the mechanism the author uses.

**Recurrence is stored in the to-do item's own description** (`repeat:weekly:3`),
because `local_todo` has no recurrence field and `todo.add_item` has no `rrule`.
The dispatcher reads it back when an item fires and schedules the next
occurrence — computed from the **due time**, not from `now`, so a series that
fires late does not drift later every day.

**The reminder list should not be exposed to your LLM agent.** Its state is an
item count that changes on every add and complete, which churns the prompt cache
and costs seconds per turn (FINDINGS §4). The intents are hidden from the LLM
too, for a different reason (FINDINGS §11).

**Not implemented, deliberately:** re-sending until acknowledged. The Companion
app fires no event when a notification is dismissed (FINDINGS §16), so a nag
loop would keep firing through the exact gesture people use to mean "seen it".

**Monthly recurrence** is unsupported: month arithmetic in Jinja means deciding
what the 31st means in February, and a wrong date is worse than an unsupported
phrase.

## FINDINGS.md

19 pieces of Home Assistant voice behaviour that are not documented elsewhere,
each measured rather than assumed. Among them: `prefer_local_intents` rejects
rather than allows, every `intent_script` is auto-published as an LLM tool with
parameters that cannot express it, hassil does not strip punctuation, and the
acknowledgement beep silently discards spoken responses.

## Licence

MIT — see [LICENSE](LICENSE).
