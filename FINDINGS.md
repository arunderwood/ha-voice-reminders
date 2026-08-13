# Findings

Non-obvious behaviour of the Home Assistant voice stack, each one measured
rather than assumed, gathered while building
[ha-voice-reminders](../). Line references are to Home Assistant 2026.8.1.

Several of these are not documented anywhere else, and a few contradict what the
names in the source suggest. If you are building anything on Assist,
§10 and §11 in particular will save you a day.

---

## 1. The acknowledgement beep silently discards spoken responses

`assist_pipeline/pipeline.py` — if **every** targeted entity is in the same area
as the satellite that heard the command, HA plays an acknowledgement beep and the
response text is never spoken, however it was written.

```python
if all_targets_in_satellite_area or tts_input.strip():
    current_stage = PipelineStage.TTS     # ...and if all_targets_in_satellite_area:
else:                                     #     play the beep, not the speech
    current_stage = PipelineStage.END
```

It only applies to `ACTION_DONE` responses with matched states, and only when the
agent is `conversation.home_assistant` — queries, timers, errors and LLM replies
always speak.

"With matched states" is load-bearing: `_get_all_targets_in_satellite_area`
returns `False` immediately when `intent_response.matched_states` is empty, so it
never reaches the `all()` that would vacuously be true. An `intent_script` that
targets no `name`/`area`/`floor` sets no matched states and therefore always
speaks — which is what makes the reminder confirmations audible.

The beep is `acknowledge.mp3`, shipped inside the component and injected as
`override_media_path` on the TTS stream (`pipeline.py:1813`). It is reachable
**only** through `all_targets_in_satellite_area` — there is no setting, service
or response value that requests it. So "beep instead of speaking" cannot be
opted into for a command whose targets are area-less: buying the beep means
giving the light groups an area, which silences every spoken response for good.
A short spoken acknowledgement like "Done." is the affordable substitute.

**Consequence:** any custom spoken response depends on commands resolving to
entities that have **no area**. Light *groups* are the usual case: give the
group an area and every spoken response for it silently becomes a beep.

## 2. Sentence-match precedence: custom beats built-in, then `{name}` beats `{area}`

`conversation/default_agent.py` calls:

```python
recognize_best(..., best_metadata_key=METADATA_CUSTOM_SENTENCE, best_slot_name="name")
```

Priority: **1.** custom sentence · **2.** result carrying a `name` slot · **3.**
most literal text matched.

Rule 1 means *any* custom `{area}` sentence outranks the built-in `{name}` ones.
That is why `config/custom_sentences/en/area_light_set.yaml` contains `{name}`
**mirror** sentences that duplicate the built-ins — they exist only so a name
match is also "custom" and can win on rule 2. Delete them and every aliased group
silently reverts to area-wide targeting (and therefore to beeping).

`merge_dict` (hassil) **extends** lists, so a custom `data:` block appends to the
built-in sentences rather than replacing them.

## 3. The `COMPUTED_NAME` alias trap

`entity_registry.py:621` — when an entity has aliases, Assist uses **only the
aliases**. The entity's own name is included solely via a `COMPUTED_NAME`
sentinel *inside the alias list*, which the websocket API serialises as `null`.

Writing `aliases: ["Kitchen lights"]` therefore makes the entity unmatchable by
its real name. Always send `[null, "Your Alias"]` —
`config/entity_registry/update` accepts `[vol.Any(str, None)]` and maps `null`
back to `COMPUTED_NAME`.

Verify with the `conversation/agent/homeassistant/debug` websocket command: it
reports which slots matched (`name` vs `area`) and the resolved targets, without
executing anything.

## 4. Prompt-cache poisoning is the biggest LLM latency factor

HA embeds live entity state in the LLM system prompt, sorted **by friendly name**
(`components/homeassistant/llm.py:96`), and `interesting_attributes` includes
`brightness` and `temperature`. llama.cpp reuses the longest common prefix, so
any entity whose line changes invalidates every token after it. An entity that
churns *and* sorts early is catastrophic.

Measured: a full ~3.1k-token prefill costs **~17s** at this hardware's ~190 tok/s.

| Culprit | Why | Action |
|---|---|---|
| a thermostat's `current_temperature` sensor | sorts near the top of the dump, and ticks every few minutes by a fraction of a degree | unexposed |
| satellite `media_player` + LED-ring `light` | toggle on **every spoken response** — the speaker invalidated its own cache | unexposed |

Result: LLM turns went from ~22s to **3.5–3.9s** end-to-end including TTS.

Diagnosis: `grep 'prompt eval time' ~/ollama-daemon/service.log` on the Mac.
~3k tokens = cache miss, <300 = hit.

Healthy steady state, measured 2026-08-12 with the reminder stack in place:

```
prompt eval time = 1603.03 ms /  276 tokens (172.17 tokens per second)
prompt eval time =   58.15 ms /    1 tokens
```

276 tokens is the whole re-evaluated tail of a ~3.1k-token prompt, so the
changing part — the user's turn and the date/time line — sits at the END of the
prompt, not the start. That is the only reason any of this caches at all: a
clock at the top would invalidate everything after it on every single turn.
Prefill is then 1.6s of a ~4s voice turn. **A test that stops at `end_stage: intent`
never triggers TTS and so cannot see the media_player effect** — use
`end_stage: tts`.

`expose_new_entities` for `conversation` is now **false**, so new entities
(notably each future satellite's media_player and LED ring) stay out of the
prompt unless exposed deliberately.

**One slow turn after a long idle is not poisoning.** *Measured* 2026-08-12: a
first turn of **17.4s** after several hours without an LLM request, then 4.5,
4.1, 4.6, 4.3s with no configuration change at all. Seen again 2026-08-13:
first run 9.45s, then 4.00 / 3.92 / 4.24 / 3.57s. Judge poisoning on the
**steady state**, never on the first turn.

*Not measured — why.* The plausible cause is Ollama unloading the model after its
`keep_alive` window (5 minutes by default), so the next request pays weight
loading and a full prefill together. That was never verified, and llama.cpp slot
eviction or an exposed entity changing state during the idle would look
identical. To settle it: `ollama ps` on the Mac immediately before a slow turn
shows whether the model was resident, and `prompt eval time` shows whether the
prefill was full.

**How to measure a turn, and what the numbers mean** (`tools/pipeline_probe.py`,
timing from the pipeline's own event timestamps):

| Path | Median | Notes |
|---|---|---|
| LLM-routed (`HassGetState`) | **4.00s** | all of it in the intent stage |
| local deterministic intent | **0.01s** | reminders, lights — effectively free |

Two traps, both of which caught earlier measurements in this repo:

- **Do not time `ssh ... docker exec ... python`.** That puts SSH handshake,
  container exec, interpreter start and websocket auth inside the "turn". It
  cost about 0.2–0.3s here — small, but it makes numbers incomparable with the
  event-timestamp ones.
- **`end_stage: tts` does not measure speech synthesis.** The `tts-start` and
  `tts-end` events bracket *stream setup* and report ~0.00s; Piper only
  synthesises when something fetches the stream URL. Running to `tts` matters
  for triggering the satellite's media_player churn on a **real spoken turn**,
  not for costing TTS from a websocket run. For real turns, read them back
  afterwards with `tools/pipeline_timing.py`.

## 5. Whisper's initial prompt matters more than the model size

Benchmarked 2026-08-13 on **12 recorded utterances of real speech** — captured
with `assist_pipeline: debug_recording_dir`, through the actual Voice PE
microphone, in the actual room. Synthesised audio is useless for this: Whisper
handles clean TTS almost perfectly and every config scores full marks.

Scored on **whether the transcript still reaches the right intent**, not on word
error rate. `"2.30 p.m."`, `"2.30 pm"` and `"2.30pm"` are three different
strings and all three are correct; `"at 6"` heard as `"it's 6"` is a total
failure from one short word.

| Config | Right intent | Mean |
|---|---|---|
| `small-int8` + lights prompt *(was deployed)* | 10/12 | 1.09s |
| `small-int8` + reminder prompt | **11/12** | 1.09s |
| `base-int8` + lights prompt | 9/12 | 0.35s |
| **`base-int8` + reminder prompt** *(now deployed)* | **11/12** | **0.35s** |

The deployed `--initial-prompt` listed only light commands — "let's play D and
D", "reset the lights" — and no reminder vocabulary at all. It was fixing two
failures' worth of accuracy in the wrong direction:

- "Remind me" heard as **"Rime me"** (small) and **"Rhyme at"** (base)
- "every weekday" heard as **"every week that"** (base)

**A straight A/B would have reached the wrong conclusion.** Against the stale
prompt, base looks *worse* than small (9 vs 10) and you keep small-int8. Fix the
prompt and they tie, at a third of the latency. Whenever a model is swapped,
re-check the prompt in the same experiment — it is a second variable, not a
constant.

One failure survives every config: **"remind me at 6 to start dinner"** is heard
as "remind me it's 6…". A fifth prompt naming bare-hour times explicitly ("at 2
pm, at 6, at 9") did not shift it, so it is acoustic rather than promptable. Not
worked around in the grammar, because accepting "it's" for "at" would collide
with real tasks like "remind me that it's move-in time".

`tools/stt_bench.py` runs the comparison; `tools/stt_score.py` scores the
transcripts against the grammar. The 12 wavs are kept at `~/stt_wavs` on the Mac
as a regression corpus.

## 6. `launchctl bootout` is asynchronous, and bootstrapping too soon says EIO

Re-installing the Whisper daemon failed with:

```
Bootstrap failed: 5: Input/output error
```

which reads like a malformed plist and is not one — `plutil -lint` passed and the
file installed correctly. `launchctl bootout` returns before the teardown
finishes, so the `bootstrap` immediately after it races and fails. The service
was left **booted out and not restarted**: nothing listening on 10300, so STT
was down until a process was started by hand.

`mac/whisper/install-daemon.sh` now polls until `launchctl print` reports the
label gone *and* port 10300 is free, enables the label *before* bootstrapping
(a disabled label fails with the same EIO, and enabling afterwards is too late),
retries three times, and exits non-zero on failure instead of printing "Done"
over it.

The original script had the same latent bug. It only appears on a **re**-install,
because the first run had no system daemon to tear down.

## 7. Prompt engineering: examples get copied verbatim

An 8B model reproduces any concrete example line in the system prompt, numbers
included (11/20 replies in testing, including a fabricated count). Describing the
*register* abstractly, with zero example replies, produced 16/16 correct tool
calls and 19/20 distinct replies. Keep `config/custom_sentences/` persona prompts
example-free.

## 8. Wyoming has no reconfigure flow

`wyoming/config_flow.py` has only `async_step_user`. Host/port cannot be changed
through any UI or API, and delete-and-re-add changes the entity's unique_id
(`<entry_id>-stt`), which would rename `stt.faster_whisper` and silently break
`stt_engine` in every pipeline.

Safe procedure: back up `.storage/core.config_entries`, `docker stop`, edit
`data.host` with a throwaway container
(`docker run --rm -v <config>:/config --entrypoint python homeassistant/home-assistant:<tag>`),
`docker start`.

## 9. Assist pipeline selection is per-device

The Voice PE has its own selector entity, `select.<device>_assistant`. Changing
the *preferred* pipeline in Settings does nothing while that select names a
specific pipeline.

## 10. `prefer_local_intents` filters the opposite way to how it reads

When the pipeline's agent has `ConversationEntityFeature.CONTROL` — both agents
here do, `supported_features: 1` — `pipeline.py` passes
`_async_local_fallback_intent_filter` into local matching:

```python
def _async_local_fallback_intent_filter(result: RecognizeResult) -> bool:
    """Filter out intents that are not local fallback."""
    return result.intent.name in (INTENT_GET_STATE, INTENT_MEDIA_SEARCH_AND_PLAY)
```

The name and docstring suggest an allowlist. It is a **rejection** list:
`default_agent.py:1459` reads `if ... intent_filter(result): return None`, so
returning `True` sends the utterance to the LLM. `HassGetState` and
`HassMediaSearchAndPlay` are deliberately handed to the model — everything else,
including custom `intent_script` intents, stays on the fast local path.

Measured on a pipeline whose agent is a local LLM with the Assist API:

| Utterance | Intent | `processed_locally` |
|---|---|---|
| "what time is it" | `HassGetCurrentTime` (not listed) | **True** |
| "what is the state of the kitchen lights" | `HassGetState` (listed) | **False** |

Local matching also runs with `strict_intents_only=True`, which only means it
will not fall back to matching unexposed entities. Grammars with no
`{name}`/`{area}` slot — the reminder ones — are unaffected.

## 11. Every `intent_script` becomes an LLM tool, with the wrong parameters

`intent_script/llm.py` builds an `IntentTool` for each configured intent script.
`IntentTool` derives its parameters from `handler.slot_schema`, and
`ScriptIntentHandler.slot_schema` is a fixed generic set — `name`/`area`/`floor`,
`domain`, `device_class`. It knows nothing about the custom slots the sentences
actually fill.

So a custom intent taking `{task}` and `{hour}` is advertised to the model as a
tool whose parameters cannot express either. The model calls it anyway, and the
templates run with everything undefined.

The lever is `platforms:`, which is dual-purposed:

- `intent_script/llm.py` keeps the tool only if `platforms` names a domain with
  at least one entity exposed to the assistant
- `IntentHandler.async_can_handle` (`helpers/intent.py:831`) admits only the
  listed **calling platforms**

`platforms: [conversation]` satisfies both: voice calls
`intent.async_handle(..., DOMAIN, ...)` with `DOMAIN == "conversation"`
(`default_agent.py:565`) so it still works, while no `conversation.*` entity is
exposed, so the tool disappears from the prompt. Verify exposure before relying
on this — `todo` *is* an exposed domain here (via `todo.shopping_list`), so
`platforms: [todo]` would not have suppressed anything.

Side effect: `/api/intent/handle` runs on platform `http` and can no longer
invoke these intents. Test through the pipeline.

## 12. Custom sentences and packages reload without a restart

`intent_script.reload` re-reads `configuration.yaml` including packages, and
`conversation.reload` re-reads `custom_sentences/`. Both are plain services:

```bash
curl -X POST -H "Authorization: Bearer $TOKEN" \
  http://homeassistant.local:8123/api/services/intent_script/reload
```

This turns a one-minute edit-restart cycle into a two-second one. Reload and
cold start are still different code paths, so confirm the final state with a real
restart — but not on every iteration.

## 13. The LLM fallback fabricates confirmations

Asked something the grammar misses — "remind me tomorrow morning to call the
bank" — the model replied *"I've added 'Call the bank' to your shopping list.
I'll remind you when the time comes."* Both `todo.shopping_list` and
`todo.reminders` were empty afterwards and nothing was logged.

This is worse than the refusal recorded in `PLAN.md`'s baseline, because it is
indistinguishable from success at the speaker. Anything phase 5 builds on the
LLM path has to confirm from the *result of the action*, never from the model's
own account of it.

## 14. An ISO datetime is the wrong thing to ask a small model for

`script.remind_me` originally took `when` as an ISO 8601 string, which is the
obvious design and the one the prompt supports — `helpers/llm.py:45` supplies
both `Current time is HH:MM:SS` and `Today's date is YYYY-MM-DD`.

Asked to schedule "remind me in a couple of hours to water the plants" at
16:19, `ministral-3:8b` called the tool with:

```json
{"what": "Water the plants", "when": "2023-08-12T18:19:00-05:00"}
```

The arithmetic is perfect — 16:19 plus two hours, correct offset. The **year is
three years wrong**, so it failed the in-the-past check and no reminder was set.
An ISO string hands the model a year, a month and a day to hallucinate, when the
only thing it is reliably good at is small integers.

Replacing `when` with three separate integer fields — `hour` (0-23), `minute`,
`days_from_today` — fixed it outright, and every date decision moved into the
script. Same principle as the sentence grammar: the model supplies numbers, the
template does the arithmetic. The worst case becomes a reminder on the wrong day
rather than one in 2023.

## 15. The model skips the tool and invents the confirmation ~1 in 3 times

Measured over six identical-shaped requests with `script.remind_me` exposed:

| Utterance | Tool called? |
|---|---|
| "remind me tomorrow morning to call the bank" × 3 | **no**, **no**, yes |
| "remind me in a couple of hours to water the plants" | yes |
| "remind me this evening to take the bins out" | yes |
| "remind me tomorrow afternoon to book the flights" | yes |

The two misses both produced confident spoken confirmations — *"Reminders are
for people who forget. I'll remind you at 7 AM tomorrow. I've set it."* — with
no `call_service` event behind them. This is findings §13 again, and no amount of
wording in the tool description ("Always call this rather than replying that you
cannot set reminders") removed it.

**The fix is not to argue with the model, it is to not ask it.** Fuzzy times are
a small closed set — morning, afternoon, evening, night, noon, midnight, crossed
with today/tomorrow/tonight — so they moved into the grammar as
`SetReminderDaypart`, resolved deterministically. The LLM path remains only for
genuinely open-ended phrasings, where it is the difference between an
approximate reminder and none.

Do not treat a spoken confirmation as evidence a tool ran. Subscribe to
`call_service` — `tools/reminders_dispatch_test.py` shows the pattern.

## 16. The Companion app cannot tell you a notification was dismissed

`clear_notification` lets Home Assistant remove a notification, and tapping an
action button fires `mobile_app_notification_action`. **Swiping a notification
away fires nothing.**

Consequence for any "nag until acknowledged" design: acknowledgement can only be
an explicit action — an action button or a voice command — so a re-send loop
keeps firing through the gesture most people use to mean "seen it". This is why
`PLAN.md` decision 6 rules that feature out rather than building it.

Related, if reaching for persistence instead: `persistent: true` makes a
notification non-dismissible, but **from Android 14 onward it is dismissable
anyway except on the lock screen**. `sticky: true` only survives a tap; it is
still swipeable. A dedicated `channel` plus `importance: high` is the part that
reliably changes behaviour, because it can be exempted from Do Not Disturb.

## 17. Android blocks background app launches, so `command_webview` needs a permission

`notify.mobile_app_*` with `message: command_webview` looks like the obvious way
to put something on screen from a voice command. On Android 10+ it answers with
a notification saying the app needs permission and does nothing, because the OS
forbids an app from starting an activity from the background. The permission is
**"Display over other apps"** (`SYSTEM_ALERT_WINDOW`), and the same applies to
`command_activity`.

**A normal notification with `clickAction` needs no permission at all**, because
tapping it is a user-initiated launch:

```yaml
- action: notify.mobile_app_<your_phone>
  data:
    title: Reminders
    message: "Tap to open your list"
    data:
      clickAction: "/todo"
      tag: reminders_open
```

One tap instead of none, and it works on an unmodified phone. Worth preferring
unless the instant version is worth asking someone to grant a permission that
also lets the app draw over everything else.

## 18. hassil does not strip punctuation, and the two Assist front ends disagree

Reminders worked when spoken at the Voice PE and silently failed in the mobile
app. Nothing was wrong with the app, the pipeline, or the intent: all three
pipelines and both `conversation.process` routes handle the same sentence
correctly. The two front ends simply **spell the same words differently**.

| Source | Writes |
|---|---|
| faster-whisper, at the satellite | `remind me at 2 PM to do laundry` |
| phone dictation / typing | `remind me at 2 p.m. to do laundry` |

hassil keeps the periods, so `p.m.` never matched a list containing only `pm`
and `p m`. The sentence missed the grammar and fell through to the LLM — which
answers plausibly and schedules nothing about a third of the time (findings
§15), so it looked like a broken feature rather than a missed match.

Two fixes, both non-obvious:

1. **List the dotted spellings literally** — `a.m.`, `p.m.`, and likewise
   `min.` / `hrs.`. There is no normalisation step to lean on.
2. **`[ ]`, not a literal space, before an adjacent slot.** With
   `...{minute}] [{meridiem}]`, `2pm` matches but `2:30pm` does not. Making the
   space optional — `...{minute}][ ][{meridiem}]` — covers `2pm`, `2 pm`,
   `2 p.m.`, `2:30pm`, `2:30 pm`, `2:30 p.m.`, `2 30 pm` and `14:30` in one
   rule. Splitting the rule into colon/no-colon alternatives looks equivalent
   and quietly loses `2 30 pm`.

The same phrasing gap bit a second time, from the other direction: `rem_verb`
listed `set a reminder` but not **`set reminder`**, which is what people type.
Without the article the sentence matched a built-in `{name}` sentence instead
and answered *"There is no reminder for 9 p.m. to check. There has never been a
reminder for 9 p.m. to check."* — a confident denial rather than a miss, which
reads as the feature being broken.

**Test every grammar against typed text as well as transcribed text.** A voice
grammar validated only against STT output has a whole second dialect it has
never seen — different punctuation, different articles, and no filler.

A missed match here is never silent. It gets picked up by some built-in
`{name}` sentence and comes back as an error response, so **the phrasing that
fails is always visible in the Assist transcript**. Read the exact string, run
it through `tools/reminders_check.py`, and the diagnosis takes a second.

The dialect is wider than it first looks. Three separate rounds were needed,
each a different axis of the same sentence:

| Round | Broke on | Because |
|---|---|---|
| 1 | `2 p.m.` | punctuation is not stripped |
| 2 | `set reminder` | the article was mandatory |
| 3 | `10.30` | only `:` was accepted as a minute separator |
| 4 | `cancel reminders` | "all" was mandatory |
| 5 | `...that it's move-in time` | only `to` joined the time to the task |
| 6 | `...at 11 a.m., time to move in` | a comma stood in for the connector, so there was none |

Rounds 4-6 are all the same shape as 2: a word the grammar treated as
mandatory that people simply do not say, or a word it never considered. The
pattern is now clear enough to state as a rule — **for every filler word in a
sentence template, ask whether a person might drop it, and for every connector,
ask what else could stand in its place.**

Making a connector optional has a cost, though, and it is worth paying
attention to: with `[<rem_to>]` on the minutes/hours form, "remind me in 20
minutes at 10am to break this" parses as a 20-minute reminder whose task is
"at 10am to break this" — a silently wrong TIME. That form keeps its connector
required so the sentence fails loudly instead. **Prefer no match over a match
with the wrong time.**

**Stop waiting for round 7.** `tools/reminders_sweep.py` generates ~1000
plausible phrasings by crossing the axes people actually vary — verb, article,
connector, punctuation, time spelling, word order — and reports two things:

- **MISS**: nothing matched. Some are correct refusals; read and judge.
- **SUSPECT**: something matched, but a time-looking phrase ended up inside the
  `{task}` wildcard. That is a reminder scheduled at the **wrong time** while
  sounding completely successful, and it is the worst failure this system has.

The SUSPECT check is what makes loosening the grammar safe. Making the task
connector optional is exactly the kind of change that trades a MISS for a
SUSPECT, and without an automated check the trade is invisible. First sweep:
1082 phrasings, 739 matched, 0 suspects. After acting on the report: 1021/1046
matched, still 0 suspects, and the remaining misses are all deliberate.

Round 3 is the instructive one: `probe 2.30 pm -> NO MATCH` had already appeared
in the round-1 probe output and went unread, because the probe was being scanned
for the `p.m.` line it was written to answer. **When a probe prints a table,
read every failing row, not the one the question was about** — the others are
the next bug report, already paid for.

## 19. A test that snapshots data must restore every field of it

Two bugs in this repo's own test suite, both of which quietly damaged real
reminders, and both the same mistake in different clothes: **identifying or
reproducing an item by less than all of it.**

- `reminders_e2e.py` removed its fixtures **by summary**. Its fixtures include
  "call the bank" and "look at the oven" — things a person actually says — so
  it deleted the *user's* reminder and left its own behind, then reported its
  own survivor as a leftover.
- `reminders_manage_test.py` snapshotted `summary` and `due` but restored
  without `description`. Recurrence lives in the description, so every
  recurring reminder it snapshotted came back as a **one-off**. The series just
  stops. Nothing errors, nothing logs, and the item still looks right in the
  list.

The second is the more dangerous shape: a restore that succeeds, is verified as
successful, and is silently lossy — the verification compared only the fields it
had bothered to save.

Rules that follow, and are now applied throughout `tools/`:

- **Address items by uid, never by summary.** Summaries repeat by design.
- **Snapshot and restore whole records**, and diff every field, not the
  convenient ones.
- **Assume the live list contains real data.** It does.

