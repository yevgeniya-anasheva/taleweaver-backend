import asyncio
import json
import math
import uuid
import websockets
from dotenv import load_dotenv
import os
import google.generativeai as genai
from google.generativeai.types import GenerationConfig
import random
from combat import Attack, start_combat
from map import generate_complex_map

# === Load API key ===
load_dotenv()
api_key = os.getenv("API_KEY")

genai.configure(api_key=api_key)
model = genai.GenerativeModel("gemini-2.5-flash")

WORLD_WIDTH = 50
WORLD_HEIGHT = 50
VIEW_SIZE = 10
LEASH_DISTANCE = 7.0  # Max Euclidean distance allowed between any two players
MAX_MOVE_STEP = 6     
WALKABLE_TILE_TYPES = {"grass", "forest"}
rooms = {}


def visualize_map(grid):
    COLORS = {
        "water": "\033[94m", "grass": "\033[92m", "forest": "\033[32m",
        "mountain": "\033[90m", "building": "\033[93m", "reset": "\033[0m"
    }
    # Using dots for grass and letters for others
    CHARS = {"water": "W", "grass": ".", "forest": "F", "mountain": "M", "building": "█"}

    print("\n--- Map ---")
    for row in grid:
        for tile in row:
            color = COLORS.get(tile['type'], "")
            char = CHARS.get(tile['type'], "?")

            if tile['door'] == "left":
                display = f"|{char}"
            elif tile['door'] == "right":
                display = f"{char}|"
            elif tile['door'] == "top":
                display = f"¯{char}"
            elif tile['door'] == "bottom":
                display = f"_{char}"
            else:
                display = f" {char} "

            print(f"{color}{display}{COLORS['reset']}", end="")
        print()


# === Viewport helpers ===

def is_walkable(tile):
    """Tiles a player can step onto."""
    if not tile:
        return False
    return tile.get("type") in WALKABLE_TILE_TYPES


_DIRECTION_DELTAS = {
    "up": (0, -1),
    "down": (0, 1),
    "left": (-1, 0),
    "right": (1, 0),
    "north": (0, -1),
    "south": (0, 1),
    "west": (-1, 0),
    "east": (1, 0),
}


def direction_to_delta(direction):
    if direction is None:
        return None
    return _DIRECTION_DELTAS.get(str(direction).lower())


def get_viewport(player_positions, world_map, world_width, world_height, view_size=VIEW_SIZE):
    """Return a view_size x view_size slice of the world centered on the players' average position.

    Returns:
        (tiles, (start_x, start_y)) where `tiles` is a flat list of tile dicts
        annotated with rel_x/rel_y (viewport coords) and world_x/world_y (global coords).
    """
    positions = list(player_positions.values()) if isinstance(player_positions, dict) else list(player_positions)

    if positions:
        avg_x = sum(p["x"] for p in positions) / len(positions)
        avg_y = sum(p["y"] for p in positions) / len(positions)
    else:
        avg_x = world_width / 2
        avg_y = world_height / 2

    start_x = int(avg_x - view_size / 2)
    start_y = int(avg_y - view_size / 2)

    # Clamp so the viewport never reads outside the world bounds
    start_x = max(0, min(start_x, world_width - view_size))
    start_y = max(0, min(start_y, world_height - view_size))

    tiles = []
    for y in range(start_y, start_y + view_size):
        for x in range(start_x, start_x + view_size):
            tile = dict(world_map[y][x])
            tile["rel_x"] = x - start_x
            tile["rel_y"] = y - start_y
            tile["world_x"] = x
            tile["world_y"] = y
            tiles.append(tile)

    return tiles, (start_x, start_y)


def is_move_allowed(moving_player_id, new_coords, all_players, max_distance=LEASH_DISTANCE):
    """Reject moves that would stretch the group past the leash distance."""
    nx, ny = new_coords
    for pid, pos in all_players.items():
        if pid == moving_player_id:
            continue
        dist = math.sqrt((nx - pos["x"]) ** 2 + (ny - pos["y"]) ** 2)
        if dist > max_distance:
            return False
    return True


def compute_reachable_tiles(world_map, world_width, world_height, start,
                              max_steps=MAX_MOVE_STEP, blocked_positions=()):
    """8-direction BFS from `start` (a (x, y) tuple).

    Returns a dict mapping each reachable (x, y) -> step count. Tiles that
    aren't walkable, are blocked by another player, or out of bounds are
    treated as obstacles. The starting tile itself is omitted from the result.
    """
    sx, sy = start
    blocked = set(blocked_positions)
    visited = {(sx, sy): 0}
    frontier = [(sx, sy)]
    while frontier:
        next_frontier = []
        for (cx, cy) in frontier:
            d = visited[(cx, cy)]
            if d >= max_steps:
                continue
            for dy in (-1, 0, 1):
                for dx in (-1, 0, 1):
                    if dx == 0 and dy == 0:
                        continue
                    nx, ny = cx + dx, cy + dy
                    if not (0 <= nx < world_width and 0 <= ny < world_height):
                        continue
                    if (nx, ny) in visited:
                        continue
                    if (nx, ny) in blocked:
                        continue
                    if not is_walkable(world_map[ny][nx]):
                        continue
                    visited[(nx, ny)] = d + 1
                    next_frontier.append((nx, ny))
        frontier = next_frontier
    visited.pop((sx, sy), None)
    return visited


def find_spawn_position(world_map, world_width, world_height, player_positions,
                         view_size=VIEW_SIZE, max_distance=LEASH_DISTANCE):
    """Pick a walkable, unoccupied spawn that respects the leash."""
    occupied = {(p["x"], p["y"]) for p in player_positions.values()}

    if player_positions:
        anchor_x = sum(p["x"] for p in player_positions.values()) / len(player_positions)
        anchor_y = sum(p["y"] for p in player_positions.values()) / len(player_positions)
    else:
        anchor_x = world_width / 2
        anchor_y = world_height / 2

    # Search outward from the anchor in widening rings until we find a valid tile
    max_radius = max(world_width, world_height)
    for radius in range(0, max_radius):
        candidates = []
        for dy in range(-radius, radius + 1):
            for dx in range(-radius, radius + 1):
                if max(abs(dx), abs(dy)) != radius:
                    continue  # only the ring at exactly this radius
                cx = int(round(anchor_x)) + dx
                cy = int(round(anchor_y)) + dy
                if not (0 <= cx < world_width and 0 <= cy < world_height):
                    continue
                if (cx, cy) in occupied:
                    continue
                if not is_walkable(world_map[cy][cx]):
                    continue
                if not is_move_allowed("__spawn__", (cx, cy), player_positions, max_distance):
                    continue
                candidates.append((cx, cy))
        if candidates:
            return random.choice(candidates)

    # Fallback: anywhere walkable and unoccupied
    walkable = [
        (x, y)
        for y in range(world_height)
        for x in range(world_width)
        if is_walkable(world_map[y][x]) and (x, y) not in occupied
    ]
    if walkable:
        return random.choice(walkable)
    return (world_width // 2, world_height // 2)


def build_world_state_message(room):
    """Build the world_state payload to broadcast to clients in `room`."""
    tiles, (start_x, start_y) = get_viewport(
        room["player_positions"],
        room["world_map"],
        room["world_width"],
        room["world_height"],
        room["view_size"],
    )
    return {
        "type": "world_state",
        "tiles": tiles,
        "viewport": {
            "start_x": start_x,
            "start_y": start_y,
            "size": room["view_size"],
        },
        "world_size": {
            "width": room["world_width"],
            "height": room["world_height"],
        },
        "player_positions": dict(room["player_positions"]),
    }


async def broadcast_world_state(room_id):
    if room_id not in rooms:
        return
    payload = build_world_state_message(rooms[room_id])
    await broadcast(room_id, json.dumps(payload))


# Each enemy has 3 random abilities: melee or ranged, with baseDamage and radius (0 = single target, >0 = AOE)
def _random_ability(attack_type):
    names_melee = ["Stab", "Slash", "Bite", "Claw", "Slam", "Cleave", "Strike"]
    names_ranged = ["Throw", "Shot", "Spit", "Bolt", "Blast", "Barrage"]
    names = names_melee if attack_type == "melee" else names_ranged
    return {
        "attackName": random.choice(names),
        "baseDamage": random.randint(2, 10),
        "radius": random.choice([0, 0, 0, 1, 1, 2]),  # 0 = single target, 1–2 = AOE
        "attackType": attack_type,
    }

def _enemy_abilities():
    abilities = []
    for _ in range(3):
        abilities.append(_random_ability(random.choice(["melee", "ranged"])))
    return {"melee": [a for a in abilities if a["attackType"] == "melee"], "ranged": [a for a in abilities if a["attackType"] == "ranged"]}

enemy_pool = [
    {"race": "Goblin", "hp": 7, "ac": 12, "stats": {"str": 8, "dex": 14, "con": 10}, "abilities": _enemy_abilities()},
    {"race": "Skeleton", "hp": 10, "ac": 13, "stats": {"str": 10, "dex": 14, "con": 15}, "abilities": _enemy_abilities()},
    {"race": "Kobold", "hp": 5, "ac": 11, "stats": {"str": 7, "dex": 15, "con": 9}, "abilities": _enemy_abilities()},
    {"race": "Orc", "hp": 15, "ac": 13, "stats": {"str": 15, "dex": 10, "con": 14}, "abilities": _enemy_abilities()},
    {"race": "Giant Rat", "hp": 6, "ac": 10, "stats": {"str": 6, "dex": 12, "con": 10}, "abilities": _enemy_abilities()},
]


def _run_local_combat_demo():
    """Local interactive combat smoke test. Calls input(); never run on import."""
    active_enemies = [dict(e.copy(), abilities=_enemy_abilities())
                       for e in [random.choice(enemy_pool) for _ in range(3)]]

    my_player_json = {
        "race": "Test Hero",
        "hp": 20,
        "ac": 12,
        "stats": {"str": 14, "dex": 12, "con": 12},
        "abilities": {
            "melee": [{"attackName": "Sword Slash", "baseDamage": 5}]
        },
    }

    if active_enemies:
        start_combat(my_player_json, active_enemies)
        new_attack = Attack("Fireball", 10, 5)
        new_attack.perform_attack(my_player_json, active_enemies[0])

# Helper to broadcast messages to all clients in a room
async def broadcast(room_id, message, sender=None):
    if room_id not in rooms:
        return

    disconnected = []
    for client in rooms[room_id]["clients"]:
        try:
            await client.send(message)
        except websockets.exceptions.ConnectionClosed:
            disconnected.append(client)

    for d in disconnected:
        rooms[room_id]["clients"].discard(d)


# Function to generate a new game
async def create_new_game(setting: str) -> str:
    try:
        response = model.generate_content(
            f"Generate a detailed description for a new fictional Dungeons & Dragons style adventure "
            f"set in the following setting: {setting}. Include interesting lore, first main quest, and main NPCs."
        )
        return response.text.strip()
    except Exception as e:
        print(f"Error generating game: {e}")
        return "Error: Could not generate game description."


async def create_character(requested_race: str = None, requested_class: str = None) -> dict:
    prompt = f"""
You are a character generator for a Dungeons & Dragons style game.
Return ONLY a valid JSON object.
DO NOT include any explanation, comments, or markdown.
for charDescription generate something funny, referencing existing popular fantasy characters. It should be brief, 
no longer than 100 characters.
for inventory include something funny, unexpected and 25% useless like rock, rubber duck, lost keys, bag of Moulding, 
reverse rain cloak, lonely left boot of Elvenkind, unlucky charm etc. Include at least 3 items

You MUST return EXACTLY this structure:

{{
  "maxHp": 20,
  "ac": 10+(stats.con for every 2 points above 10 add 1),
  "hp": 20,
  "race": "<DND race>",
  "portrait": "some generated ASCII art. Portrait ASCII art must NOT contain backslashes. Use only: | / ( ) _ ^ o * - = + [ ]",
  "charDescription": "",
  "class": "<warrior/mage/rogue>",
  "traits": [
     {{
        "traitName": "",
        "traitDescription": ""
     }}
  ],
  "stats": {{
    "str": <1-15>,
    "dex": <1-15>,
    "con": <1-15>,
    "int": <1-15>,
    "wis": <1-15>,
    "chr": <1-15>
  }},
  "abilities": {{
    "melee": [
      {{
        "attackName": "",
        "baseDamage": <1-15>,
        "range": <1-15>
      }}
    ],
    "ranged": [
      {{
        "attackName": "",
        "baseDamage": <1-15>,
        "range": <1-15>
      }}
    ]
  }},
  "inventory": [
     {{
        "itemName": "",
        "itemDescription": ""
     }}
  ],
}}

Rules:
- Exactly 3 total abilities (melee + ranged).
- Stats must all be <= 15.
- Select race from: Dragonborn, Dwarf, Elf, High Elf, Gnome, Halfling, Human, Orc, Tiefling, Changeling, Fairy, Githyanki, Owlin
- Classes must be warrior, mage or rogue.
- If class is mage: ranged attacks must be spells.
- Portrait is ASCII image
"""

    if requested_race:
        prompt += f"\nRace MUST be {requested_race}."
    if requested_class:
        prompt += f"\nClass MUST be {requested_class}."

    try:
        response = model.generate_content(
            prompt,
            generation_config=GenerationConfig(
                response_mime_type="application/json",
                temperature=0.4,
                top_p=0.9,
            )
        )

        return json.loads(response.text)

    except Exception as e:
        print("Error generating character:", e)
        return {"error": "Could not parse character JSON"}



def _new_room(token):
    """Build the per-room state dict with a fresh world map."""
    return {
        "clients": set(),
        "client_ids": {},
        "token": token,
        "world_map": generate_complex_map(WORLD_WIDTH, WORLD_HEIGHT),
        "world_width": WORLD_WIDTH,
        "world_height": WORLD_HEIGHT,
        "view_size": VIEW_SIZE,
        "player_positions": {},
    }


def _spawn_player(room, client_id):
    """Place a new client somewhere walkable that respects the leash."""
    spawn_x, spawn_y = find_spawn_position(
        room["world_map"],
        room["world_width"],
        room["world_height"],
        room["player_positions"],
        room["view_size"],
        LEASH_DISTANCE,
    )
    room["player_positions"][client_id] = {"x": spawn_x, "y": spawn_y}
    return spawn_x, spawn_y


# Handle each websocket connection
async def handle_client(websocket):
    current_room = None
    client_id = None

    try:
        raw = await websocket.recv()
        data = json.loads(raw)
        action = data.get("action")
        client_id = data.get("clientId") or str(uuid.uuid4())

        # Create room
        if action == "create":
            token = data.get("token") or None
            room_id = str(uuid.uuid4())[:8]
            room = _new_room(token)
            room["clients"].add(websocket)
            room["client_ids"][websocket] = client_id
            rooms[room_id] = room
            current_room = room_id

            spawn_x, spawn_y = _spawn_player(room, client_id)
            await websocket.send(json.dumps({
                "type": "room_created",
                "room": room_id,
                "clientId": client_id,
                "spawn": {"x": spawn_x, "y": spawn_y},
            }))
            await broadcast_world_state(room_id)
            print(f"Room {room_id} created (token={token}) "
                  f"client={client_id} spawn=({spawn_x},{spawn_y})")

        # Join room
        elif action == "join":
            room_id = data.get("room")
            token = data.get("token")

            if room_id not in rooms:
                await websocket.send(json.dumps({"error": "Room does not exist"}))
                return

            expected_token = rooms[room_id]["token"]
            if expected_token and expected_token != token:
                await websocket.send(json.dumps({"error": "Invalid token"}))
                return

            room = rooms[room_id]
            room["clients"].add(websocket)
            room["client_ids"][websocket] = client_id
            current_room = room_id

            spawn_x, spawn_y = _spawn_player(room, client_id)
            await websocket.send(json.dumps({
                "type": "joined",
                "room": room_id,
                "clientId": client_id,
                "spawn": {"x": spawn_x, "y": spawn_y},
            }))
            await broadcast(room_id, json.dumps({
                "type": "player_joined",
                "clientId": client_id,
                "info": f"New client joined {room_id}",
            }))
            await broadcast_world_state(room_id)
            print(f"Client {client_id} joined room {room_id} "
                  f"spawn=({spawn_x},{spawn_y})")

        else:
            await websocket.send(json.dumps({"error": "First message must be 'create' or 'join'"}))
            return

        # === MAIN MESSAGE LOOP ===
        async for message in websocket:
            try:
                data = json.loads(message)
                action = data.get("action")

                # === CREATE GAME ===
                if action == "create_game":
                    setting = data.get("setting", "a fantasy world")
                    await websocket.send(json.dumps({"type": "status", "message": "Generating new game..."}))
                    description = await create_new_game(setting)
                    if current_room:
                        await broadcast(current_room, json.dumps({
                            "type": "game_created",
                            "setting": setting,
                            "description": description
                        }))

                # === CREATE CHARACTER ===
                elif action == "create_character":
                    race = data.get("race")
                    class_type = data.get("class")

                    await websocket.send(json.dumps({"type": "status", "message": "Generating character..."}))

                    new_char = await create_character(race, class_type)

                    if current_room:
                        await broadcast(current_room, json.dumps({
                            "type": "character_created",
                            "character": new_char,
                            "clientId": client_id,
                        }))

                # === MOVE (telescopic camera input) ===
                elif action == "move":
                    if not current_room or current_room not in rooms:
                        continue

                    room = rooms[current_room]
                    positions = room["player_positions"]
                    if client_id not in positions:
                        # Player has no spawn record yet (shouldn't happen, but guard)
                        spawn_x, spawn_y = _spawn_player(room, client_id)
                        await broadcast_world_state(current_room)
                        continue

                    cur = positions[client_id]
                    target = data.get("target")
                    direction = data.get("direction")

                    if isinstance(target, dict) and "x" in target and "y" in target:
                        new_x = int(target["x"])
                        new_y = int(target["y"])
                    else:
                        delta = direction_to_delta(direction)
                        if delta is None:
                            await websocket.send(json.dumps({
                                "type": "move_rejected",
                                "reason": "invalid_direction",
                            }))
                            continue
                        new_x = cur["x"] + delta[0]
                        new_y = cur["y"] + delta[1]

                    if (new_x, new_y) == (cur["x"], cur["y"]):
                        # Dropping back on yourself is a no-op, not an error.
                        continue

                    if not (0 <= new_x < room["world_width"] and 0 <= new_y < room["world_height"]):
                        await websocket.send(json.dumps({
                            "type": "move_rejected",
                            "reason": "out_of_bounds",
                        }))
                        continue

                    # Cheap pre-check: anything past Chebyshev MAX_MOVE_STEP can't be reached.
                    if max(abs(new_x - cur["x"]), abs(new_y - cur["y"])) > MAX_MOVE_STEP:
                        await websocket.send(json.dumps({
                            "type": "move_rejected",
                            "reason": "too_far",
                        }))
                        continue

                    if not is_walkable(room["world_map"][new_y][new_x]):
                        await websocket.send(json.dumps({
                            "type": "move_rejected",
                            "reason": "impassable",
                        }))
                        continue

                    if any(pid != client_id and pos["x"] == new_x and pos["y"] == new_y
                           for pid, pos in positions.items()):
                        await websocket.send(json.dumps({
                            "type": "move_rejected",
                            "reason": "occupied",
                        }))
                        continue

                    # BFS path check: must be reachable within MAX_MOVE_STEP steps,
                    # routing around impassable tiles and other players.
                    blocked = {(p["x"], p["y"]) for pid, p in positions.items() if pid != client_id}
                    reachable = compute_reachable_tiles(
                        room["world_map"],
                        room["world_width"],
                        room["world_height"],
                        (cur["x"], cur["y"]),
                        max_steps=MAX_MOVE_STEP,
                        blocked_positions=blocked,
                    )
                    if (new_x, new_y) not in reachable:
                        await websocket.send(json.dumps({
                            "type": "move_rejected",
                            "reason": "too_far",
                        }))
                        continue

                    if not is_move_allowed(client_id, (new_x, new_y), positions, LEASH_DISTANCE):
                        await websocket.send(json.dumps({
                            "type": "move_rejected",
                            "reason": "leash",
                        }))
                        continue

                    positions[client_id] = {"x": new_x, "y": new_y}
                    await broadcast_world_state(current_room)

                else:
                    # Default behavior — broadcast messages (chat, attacks, etc.)
                    if current_room:
                        await broadcast(current_room, message)

            except Exception as e:
                print(f"Error in message loop: {e}")
                await websocket.send(json.dumps({"error": str(e)}))

    except websockets.exceptions.ConnectionClosedOK:
        pass
    except Exception as e:
        print(f"Error: {e}")
    finally:
        if current_room and current_room in rooms:
            room = rooms[current_room]
            if websocket in room.get("clients", set()):
                room["clients"].discard(websocket)
            leaving_id = room.get("client_ids", {}).pop(websocket, client_id)
            if leaving_id and leaving_id in room.get("player_positions", {}):
                del room["player_positions"][leaving_id]
            print(f"Client {leaving_id} left room {current_room}")
            if not room["clients"]:
                print(f"Room {current_room} empty — deleting")
                del rooms[current_room]
            else:
                await broadcast(current_room, json.dumps({
                    "type": "player_left",
                    "clientId": leaving_id,
                }))
                await broadcast_world_state(current_room)


async def start_server():
    print("Server running on ws://localhost:8765")
    async with websockets.serve(handle_client, "localhost", 8765):
        await asyncio.Future()


if __name__ == "__main__":
    import sys
    if "--combat-demo" in sys.argv:
        _run_local_combat_demo()
    elif "--render-map" in sys.argv:
        visualize_map(generate_complex_map())
    else:
        asyncio.run(start_server())
