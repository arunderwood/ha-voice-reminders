"""Create a Local To-do list through the config flow, then verify it.

Config entries live in .storage, not in this repo, so this is the tracked record
of how todo.reminders was made. Re-running it is safe: the flow aborts as
already_configured if a list with the same slugified name exists.

Run it inside the container, like the other tools here (aiohttp is HA's own):

    export HA_URL=http://homeassistant.local:8123
    export HA_TOKEN=...        # Profile -> Long-lived access tokens
    python tools/add_local_todo.py

Verifies, per PLAN.md phase 2:
  - the entity reports SET_DUE_DATETIME_ON_ITEM (32) in supported_features
  - it is NOT exposed to conversation (findings §4 — its state is an item count
    that churns on every add/complete, and would poison the LLM prompt cache)
"""

import asyncio, json, os, sys, aiohttp

TOKEN = os.environ["HA_TOKEN"]
BASE = os.environ.get("HA_URL", "http://localhost:8123")
WS = BASE.replace("http", "ws", 1) + "/api/websocket"
NAME = sys.argv[1] if len(sys.argv) > 1 else "Reminders"

SET_DUE_DATETIME_ON_ITEM = 32


async def main():
    hdr = {"Authorization": f"Bearer {TOKEN}"}
    async with aiohttp.ClientSession(headers=hdr) as s:
        # --- create, via the config flow (REST; there is no websocket equivalent)
        async with s.post(
            f"{BASE}/api/config/config_entries/flow",
            json={"handler": "local_todo", "show_advanced_options": False},
        ) as r:
            flow = await r.json()
        if flow.get("type") == "abort":
            print(f"flow aborted at the first step: {flow.get('reason')}")
            return
        print(f"flow {flow['flow_id']} step={flow['step_id']}")

        async with s.post(
            f"{BASE}/api/config/config_entries/flow/{flow['flow_id']}",
            json={"todo_list_name": NAME},
        ) as r:
            res = await r.json()
        if res.get("type") == "abort":
            print(f"abort: {res['reason']}  (already present — continuing to verify)")
        else:
            print(f"created entry {res['result']['entry_id']} title={res['title']!r}")

        # --- verify the entity
        entity_id = f"todo.{NAME.lower().replace(' ', '_')}"
        async with s.get(f"{BASE}/api/states/{entity_id}") as r:
            if r.status != 200:
                print(f"FAIL: {entity_id} does not exist ({r.status})")
                return
            state = await r.json()
        feat = state["attributes"].get("supported_features", 0)
        ok = bool(feat & SET_DUE_DATETIME_ON_ITEM)
        print(f"{entity_id}: state={state['state']} supported_features={feat} "
              f"due_datetime={'yes' if ok else 'NO'}")

        # --- verify it stayed out of the LLM prompt
        async with s.ws_connect(WS) as ws:
            await ws.receive_str()  # auth_required
            await ws.send_json({"type": "auth", "access_token": TOKEN})
            while json.loads(await ws.receive_str())["type"] != "auth_ok":
                pass
            await ws.send_json({"id": 1, "type": "homeassistant/expose_entity/list"})
            while True:
                msg = json.loads(await ws.receive_str())
                if msg.get("id") == 1 and msg["type"] == "result":
                    break
            exposed = msg["result"]["exposed_entities"].get(entity_id, {})
            conv = exposed.get("conversation", False)
            print(f"exposed to conversation: {conv}"
                  + ("  <-- unexpected, see findings §4" if conv else "  (correct)"))


asyncio.run(main())
