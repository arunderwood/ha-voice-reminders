# Giving the responses a character

The shipped responses are plain:

> Reminder set for 2 PM tomorrow: do laundry.

They are plain on purpose — a confirmation's job is to state the time and the
task so you can catch a mistake before it matters. But there is no reason it has
to be dry, and the author's own install answers in the voice of a passive
aggressive AI.

## The mechanism

Every response is an ordinary Jinja template, so a list plus `random` is all it
takes. The pattern used in the original, which speaks in character roughly one
command in three and plainly otherwise:

```jinja
{% set lines = [
  "Noted. " ~ task ~ ", at " ~ when ~ ". I will remind you. You will still forget.",
  "Fine. " ~ when ~ ". " ~ task ~ ". Written down, because you would not.",
  "Reminder set for " ~ when ~ ". I have added it to the list of things you intend to do.",
  "At " ~ when ~ " I will say " ~ task ~ " back to you. Prepare to be surprised by it.",
  "Scheduled. " ~ when ~ ". Outsourcing your memory to a speaker. A bold arrangement.",
] %}
{{ (lines | random) if (range(3) | random) == 0
   else ("Reminder set for " ~ when ~ ": " ~ task ~ ".") }}
```

Drop that in place of the plain line in `packages/reminders.yaml`, in the
`&reminder_speech` anchor.

**Keep the time and the task in every branch.** A confirmation that omits them
confirms nothing, and the whole point of speaking one is that you notice when
the system heard "2 AM" and you said "2 PM".

## The spoken reminder itself

In the dispatch automation, `message:`:

```jinja
{% set task = repeat.item.summary %}
{% set lines = [
  "Reminder. " ~ task ~ ". You asked for this. I have the recording.",
  "It is time to " ~ task ~ ". I have been counting down. Quietly. For both of us.",
  "You wanted reminding: " ~ task ~ ". Consider yourself reminded.",
  "This is the part where I say " ~ task ~ ", and you say you were just about to.",
] %}
{{ (lines | random) if (range(3) | random) == 0 else ("Reminder. " ~ task ~ ".") }}
```

## Two things worth knowing before you go further

**Never make a response an empty string.** An empty response makes the pipeline
skip text-to-speech entirely, so the satellite says nothing at all — which is
indistinguishable from it not having heard you, and you will repeat yourself
into a device that already did the thing.

**A spoken response can be silently replaced by a beep.** If every entity a
command targets is in the same area as the satellite that heard it, Home
Assistant plays an acknowledgement sound instead of speaking, no matter what the
response says. This does not affect reminders — an `intent_script` that targets
nothing sets no matched states — but it will bite you the moment you write
custom responses for lights. See FINDINGS §1.
