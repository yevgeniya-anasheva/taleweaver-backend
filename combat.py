import random
import math


def _distance(pos1, pos2):
    """Euclidean distance between two (x, y) positions."""
    return math.sqrt((pos1[0] - pos2[0]) ** 2 + (pos1[1] - pos2[1]) ** 2)


def _ensure_positions(players, enemies):
    """Ensure every participant has a position (for AOE/range). Assign defaults if missing."""
    for i, p in enumerate(players):
        if "position" not in p or p["position"] is None:
            p["position"] = (0, i)  # Row of players at x=0
    for i, e in enumerate(enemies):
        if "position" not in e or e["position"] is None:
            e["position"] = (2, i)  # Enemies at x=2


def _get_players_in_radius(attacker_pos, players, radius):
    """Return list of living players within given radius of attacker. radius 0 = single-target (return up to 1)."""
    alive = [p for p in players if p.get("hp", 0) > 0]
    if radius == 0:
        return alive[:1]  # Single target: first alive (could be refined to "closest")
    in_range = [p for p in alive if _distance(attacker_pos, p.get("position", (0, 0))) <= radius]
    return in_range


def _get_all_abilities(actor):
    """Return flat list of all abilities (melee + ranged) with attackType, baseDamage, radius."""
    abilities = []
    for atype in ("melee", "ranged"):
        for a in actor.get("abilities", {}).get(atype, []):
            ab = dict(a)
            ab.setdefault("attackType", atype)
            ab.setdefault("radius", 0)
            abilities.append(ab)
    return abilities


class Attack:
    def __init__(self, name, damage, accuracy_mod, attack_type="melee", radius=0):
        self.name = name
        self.damage = damage
        self.accuracy_mod = accuracy_mod  # The stat modifier (e.g., Str or Dex)
        self.type = attack_type
        self.radius = radius  # 0 = single target, >0 = AOE (max distance in tiles)

    def perform_attack(self, attacker, target, saved_bonus=0):
        # 1. Roll d20
        roll = random.randint(1, 20)
        total_hit_roll = roll + self.accuracy_mod + saved_bonus

        print(f"--- {attacker['race']} attacks with {self.name}! ---")
        print(
            f"Roll: {roll} + Mod: {self.accuracy_mod} + Bonus: {saved_bonus} = {total_hit_roll} vs AC: {target['ac']}")

        # 2. Compare to Target AC
        if total_hit_roll >= target['ac']:
            damage_dealt = self.damage + random.randint(0, 3)
            target['hp'] -= damage_dealt
            print(f"Hit! Dealt {damage_dealt} damage. {target['race']} HP: {max(0, target['hp'])}")
            return True
        else:
            print("Miss!")
            return False

    def perform_attack_aoe(self, attacker, targets, saved_bonus=0):
        """Perform this attack against multiple targets (each rolls separately). Returns total damage dealt."""
        total_damage = 0
        for target in targets:
            if target['hp'] <= 0:
                continue
            roll = random.randint(1, 20)
            total_hit_roll = roll + self.accuracy_mod + saved_bonus
            print(f"--- {attacker['race']} uses {self.name} on {target['race']}! ---")
            print(f"Roll: {roll} + Mod: {self.accuracy_mod} = {total_hit_roll} vs AC: {target['ac']}")
            if total_hit_roll >= target['ac']:
                damage_dealt = self.damage + random.randint(0, 3)
                target['hp'] -= damage_dealt
                total_damage += damage_dealt
                print(f"Hit! Dealt {damage_dealt} damage. {target['race']} HP: {max(0, target['hp'])}")
            else:
                print("Miss!")
        return total_damage


def get_modifier(stat_value):
    return (stat_value - 10) // 2


def calculate_ac(char):
    return 10 + get_modifier(char['stats']['con'])


def _choose_ai_ability(enemy, players):
    """
    Choose which ability the AI uses this turn.
    First priority: AOE that can hit multiple players and yields NET more damage than best single-target.
    Second priority: highest total damage (single or AOE).
    """
    alive_players = [p for p in players if p.get("hp", 0) > 0]
    if not alive_players:
        return None, []

    pos = enemy.get("position", (0, 0))
    abilities = _get_all_abilities(enemy)
    if not abilities:
        return None, []

    # Use str for melee, dex for ranged
    def acc_mod(ab):
        if ab.get("attackType") == "ranged":
            return get_modifier(enemy["stats"]["dex"])
        return get_modifier(enemy["stats"]["str"])

    # Build options: (expected_total_damage, ability, targets_list, is_aoe)
    options = []
    for ab in abilities:
        radius = ab.get("radius", 0)
        damage = ab.get("baseDamage", 0)
        targets = _get_players_in_radius(pos, alive_players, radius)
        if not targets:
            continue
        # Expected damage: assume 50% hit chance for simplicity, or use (21 - avg_ac)/20
        avg_ac = sum(p["ac"] for p in targets) / len(targets)
        hit_chance = max(0.05, min(0.95, (21 - avg_ac + acc_mod(ab)) / 20))
        expected_total = damage * len(targets) * hit_chance
        options.append((expected_total, damage * len(targets), ab, targets, len(targets) > 1))

    if not options:
        return None, []

    # First priority: AOE that hits multiple and has NET more expected damage than best single-target
    single_target_options = [(e, d, ab, t, aoe) for (e, d, ab, t, aoe) in options if not aoe]
    best_single_expected = max((e for e, _, _, _, _ in single_target_options), default=0)
    best_single_total = max((d for e, d, _, _, aoe in options if not aoe), default=0)

    aoe_options = [(e, d, ab, t) for (e, d, ab, t, aoe) in options if aoe and len(t) > 1]
    for expected, total_damage, ab, targets in aoe_options:
        if expected > best_single_expected and total_damage > best_single_total:
            return ab, targets

    # Second priority: choose whichever does the most (expected) damage
    best = max(options, key=lambda x: (x[0], x[1]))
    return best[2], best[3]


def start_combat(player, enemies):
    # Normalize to list of players (support single player or multiple)
    players = player if isinstance(player, list) else [player]
    _ensure_positions(players, enemies)

    print(enemies)

    saved_attack_bonus = 0
    round_num = 0

    while any(e['hp'] > 0 for e in enemies):
        alive_players = [p for p in players if p['hp'] > 0]
        if not alive_players:
            print("All players have fallen!")
            break

        round_num += 1
        print(f"=== ROUND {round_num} ===\n")

        # --- PLAYER TURNS (each player gets one turn) ---
        for actor in alive_players:
            if actor['hp'] <= 0:
                continue
            print(f">> {actor['race']}'s Turn (HP: {actor['hp']})")
            print("1. Attack | 2. Bonus Action (Save Attack for +2 next turn) | 3. Use Item")
            choice = input("Choose action: ").strip() or "1"

            if choice == "1":
                abilities = actor.get("abilities", {})
                melee = abilities.get("melee", [])
                ranged = abilities.get("ranged", [])
                atk_list = melee if melee else ranged
                if not atk_list:
                    print("No attack available!")
                    continue
                atk_data = atk_list[0]
                mod = get_modifier(actor['stats']['str']) if melee else get_modifier(actor['stats']['dex'])
                atk_obj = Attack(
                    atk_data.get('attackName', 'Strike'),
                    atk_data.get('baseDamage', 3),
                    mod,
                    attack_type="melee" if melee else "ranged",
                    radius=atk_data.get('radius', 0),
                )
                target_enemy = next((e for e in enemies if e['hp'] > 0), None)
                if target_enemy:
                    atk_obj.perform_attack(actor, target_enemy, saved_attack_bonus)
                saved_attack_bonus = 0

            elif choice == "2":
                saved_attack_bonus = 2
                print("Focusing... Next attack has +2 to hit!")

        # Remove dead enemies after player phase
        enemies = [e for e in enemies if e['hp'] > 0]
        if not enemies:
            print("\nAll enemies defeated!")
            break

        # --- ENEMY TURNS (each enemy gets one turn) ---
        for enemy in list(enemies):
            if enemy['hp'] <= 0:
                continue
            ab, targets = _choose_ai_ability(enemy, players)
            if not ab or not targets:
                continue
            mod = get_modifier(enemy['stats']['dex']) if ab.get('attackType') == 'ranged' else get_modifier(enemy['stats']['str'])
            atk_obj = Attack(
                ab.get('attackName', 'Strike'),
                ab.get('baseDamage', 3),
                mod,
                attack_type=ab.get('attackType', 'melee'),
                radius=ab.get('radius', 0),
            )
            if len(targets) > 1:
                atk_obj.perform_attack_aoe(enemy, targets, saved_bonus=0)
            else:
                atk_obj.perform_attack(enemy, targets[0], saved_bonus=0)

        enemies = [e for e in enemies if e['hp'] > 0]

    print("\n--- COMBAT ENDS ---")
