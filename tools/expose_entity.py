"""Expose or unexpose an entity to a voice assistant.

Exposure lives in .storage, not in this repo, so this is the tracked record of
deployment steps that push.sh cannot perform. `expose_new_entities` is false for
conversation (findings §4), so anything that needs to reach the LLM has to be
turned on deliberately -- script.remind_me being the one that matters here.

    HA_TOKEN=... python tools/expose_entity.py script.remind_me
    HA_TOKEN=... python tools/expose_entity.py script.remind_me false
    HA_TOKEN=... python tools/expose_entity.py            # list what is exposed

Before exposing anything that carries state, re-read findings §4: an entity whose
state churns and whose friendly name sorts early invalidates the LLM's prompt
cache and costs whole seconds a turn. Scripts and calendars are safe -- they are
skipped by async_get_exposed_entities and contribute a tool, not a state line.
"""

import asyncio, json, os, sys, aiohttp

TOKEN = os.environ["HA_TOKEN"]
BASE = os.environ.get("HA_URL", "http://localhost:8123")
ASSISTANT = "conversation"

ENTITY = sys.argv[1] if len(sys.argv) > 1 else None
EXPOSE = (sys.argv[2].lower() != "false") if len(sys.argv) > 2 else True


async def main():
    async with aiohttp.ClientSession(headers={"Authorization": f"Bearer {TOKEN}"}) as s, \
            s.ws_connect(BASE.replace("http", "ws", 1) + "/api/websocket") as ws:
        n = 0

        async def call(payload):
            nonlocal n
            n += 1
            payload["id"] = n
            await ws.send_json(payload)
            while True:
                m = json.loads(await ws.receive_str())
                if m.get("id") == n and m["type"] == "result":
                    if not m["success"]:
                        raise RuntimeError(m["error"])
                    return m["result"]

        await ws.receive_str()  # auth_required
        await ws.send_json({"type": "auth", "access_token": TOKEN})
        while json.loads(await ws.receive_str())["type"] != "auth_ok":
            pass

        if ENTITY:
            await call({
                # Note: "homeassistant/expose_entity", NOT ".../expose" --
                # the list command is "homeassistant/expose_entity/list", and
                # the symmetry is misleading.
                "type": "homeassistant/expose_entity",
                "entity_ids": [ENTITY],
                "assistants": [ASSISTANT],
                "should_expose": EXPOSE,
            })
            print(f"{'exposed' if EXPOSE else 'unexposed'} {ENTITY} -> {ASSISTANT}")

        res = await call({"type": "homeassistant/expose_entity/list"})
        exposed = sorted(e for e, v in res["exposed_entities"].items()
                         if v.get(ASSISTANT))
        print(f"\n{len(exposed)} entities exposed to {ASSISTANT}:")
        for e in exposed:
            print(f"   {e}")
        if ENTITY:
            state = ENTITY in exposed
            print(f"\n{ENTITY} exposed: {state}"
                  + ("  (as requested)" if state == EXPOSE else "  <-- MISMATCH"))


asyncio.run(main())
