import asyncio
import json
import uuid
import websockets
from dotenv import load_dotenv
import os
import google.generativeai as genai
from google.generativeai.types import GenerationConfig
import random
from combat import Attack, start_combat

# === Load API key ===
load_dotenv()
api_key = os.getenv("API_KEY")

genai.configure(api_key=api_key)
model = genai.GenerativeModel("gemini-2.5-flash")

# Structure: { room_id: {"clients": set([websocket]), "token": str or None} }
rooms = {}

def generate_complex_map(width=10, height=10):
    # Initialize grid with a default (e.g., forest or mountains)
    grid = [[{"id": f"tile-{x}-{y}", "type": "forest", "door": None}
             for x in range(width)] for y in range(height)]

    def get_neighbors(coords, radius=1):
        """Returns all coordinates within a certain radius of the given tiles."""
        neighbors = set()
        for cx, cy in coords:
            for dy in range(-radius, radius + 1):
                for dx in range(-radius, radius + 1):
                    nx, ny = cx + dx, cy + dy
                    if 0 <= nx < width and 0 <= ny < height:
                        neighbors.add((nx, ny))
        return neighbors

    def place_cluster(cluster_type, count):
        placed = 0
        while placed < count:
            bx, by = random.randint(0, width - 1), random.randint(0, height - 1)
            direction = random.choice([(1, 0), (0, 1), (-1, 0), (0, -1)])
            nx, ny = bx + direction[0], by + direction[1]

            if 0 <= nx < width and 0 <= ny < height:
                # For buildings, check if area is clear of other buildings
                if cluster_type == "building":
                    area = get_neighbors([(bx, by), (nx, ny)], radius=1)
                    if any(grid[ay][ax]["type"] == "building" for ax, ay in area):
                        continue

                    # Place building
                    grid[by][bx]["type"] = "building"
                    grid[ny][nx]["type"] = "building"

                    # Force the 1-block buffer to be grass
                    for ax, ay in area:
                        if grid[ay][ax]["type"] != "building":
                            grid[ay][ax]["type"] = "grass"

                    # Door Logic (Inward facing)
                    center = width / 2
                    dist_b = abs(bx - center) + abs(by - center)
                    dist_n = abs(nx - center) + abs(ny - center)
                    tx, ty = (bx, by) if dist_b < dist_n else (nx, ny)
                    door_tile = grid[ty][tx]
                    if abs(tx - center) > abs(ty - center):
                        door_tile["door"] = "right" if tx < center else "left"
                    else:
                        door_tile["door"] = "bottom" if ty < center else "top"

                else:
                    # Generic cluster (Water)
                    grid[by][bx]["type"] = cluster_type
                    grid[ny][nx]["type"] = cluster_type

                placed += 1

    # 1. Place Water in 2-block groups first
    place_cluster("water", 3)

    # 2. Place Buildings in 2-block groups (will overwrite/clear area to grass)
    place_cluster("building", 3)

    return [tile for row in grid for tile in row]


def visualize_map(map_data, width=10):
    COLORS = {
        "water": "\033[94m", "grass": "\033[92m", "forest": "\033[32m",
        "mountain": "\033[90m", "building": "\033[93m", "reset": "\033[0m"
    }
    # Using dots for grass and letters for others
    CHARS = {"water": "W", "grass": ".", "forest": "F", "mountain": "M", "building": "█"}

    print("\n--- Map ---")
    for i, tile in enumerate(map_data):
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
        if (i + 1) % width == 0: print()

# Execute
my_map = generate_complex_map(10, 10)
visualize_map(my_map, 10)
print(my_map)

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

# Spawn 3; each gets its own 3 random abilities
active_enemies = [dict(e.copy(), abilities=_enemy_abilities()) for e in [random.choice(enemy_pool) for _ in range(3)]]

# Mock player data for local combat testing
my_player_json = {
    "race": "Test Hero",
    "hp": 20,
    "ac": 12,
    "stats": {"str": 14, "dex": 12, "con": 12},
    "abilities": {
        "melee": [
            {
                "attackName": "Sword Slash",
                "baseDamage": 5,
            }
        ]
    },
}

# Start combat if enemies exist
if active_enemies:
    start_combat(my_player_json, active_enemies)

# Example of using Attack directly with the same mock data
if active_enemies:
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
  "portrait": "some generated ASCII art",
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



# Handle each websocket connection
async def handle_client(websocket):
    current_room = None

    try:
        raw = await websocket.recv()
        data = json.loads(raw)
        action = data.get("action")

        # Create room
        if action == "create":
            token = data.get("token") or None
            room_id = str(uuid.uuid4())[:8]
            rooms[room_id] = {"clients": set([websocket]), "token": token}
            current_room = room_id
            await websocket.send(json.dumps({"type": "room_created", "room": room_id}))
            print(f"Room {room_id} created with token={token}")

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

            rooms[room_id]["clients"].add(websocket)
            current_room = room_id
            await websocket.send(json.dumps({"type": "joined", "room": room_id}))
            await broadcast(room_id, json.dumps({"info": f"New client joined {room_id}"}))
            print(f"Client joined room {room_id}")

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

                # === NEW: CREATE CHARACTER ===
                elif action == "create_character":
                    race = data.get("race")
                    class_type = data.get("class")

                    await websocket.send(json.dumps({"type": "status", "message": "Generating character..."}))

                    new_char = await create_character(race, class_type)

                    if current_room:
                        await broadcast(current_room, json.dumps({
                            "type": "character_created",
                            "character": new_char
                        }))

                else:
                    # Default behavior — broadcast messages
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
        if current_room and websocket in rooms.get(current_room, {}).get("clients", set()):
            rooms[current_room]["clients"].remove(websocket)
            print(f"Client left room {current_room}")
            if not rooms[current_room]["clients"]:
                print(f"Room {current_room} empty — deleting")
                del rooms[current_room]


async def start_server():
    print("Server running on ws://localhost:8765")
    async with websockets.serve(handle_client, "localhost", 8765):
        await asyncio.Future()


if __name__ == "__main__":
    asyncio.run(start_server())
