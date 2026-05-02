import random

from perlin_noise import PerlinNoise

DEFAULT_WORLD_WIDTH = 50
DEFAULT_WORLD_HEIGHT = 50

# Terrain proportions for Perlin map generation, ordered low to high
# Values are weights, not percentages
TERRAIN_LEVELS = [
    ("water", 20),
    ("grass", 45),
    ("forest", 35),
]


def generate_perlin_terrain(width=DEFAULT_WORLD_WIDTH, height=DEFAULT_WORLD_HEIGHT, seed=None):
    # Generate a width x height terrain grid using layered Perlin noise
    # Each output cell maps to exactly one noise sample (1 pixel = 1 tile)
    # Returns a list of rows of tile dicts

    if seed is None:
        seed = random.randint(0, 2 ** 31 - 1)

    base = PerlinNoise(octaves=3, seed=seed)
    detail = PerlinNoise(octaves=6, seed=seed)

    noise_map = []
    for y in range(height):
        row = []
        for x in range(width):
            # Normalize coords to [0, 1] so the pattern fits the whole map
            nx, ny = x / width, y / height
            v = base([nx, ny]) + 0.5 * detail([nx, ny])
            row.append(v)
        noise_map.append(row)

    flat = [v for row in noise_map for v in row]
    lo, hi = min(flat), max(flat)
    total_weight = sum(w for _, w in TERRAIN_LEVELS)

    # Cumulative cutoff for each terrain band
    cutoffs = []
    running = lo
    for terrain, weight in TERRAIN_LEVELS:
        running += (hi - lo) * (weight / total_weight)
        cutoffs.append((terrain, running))
    # Make sure the last band always catches the maximum value
    cutoffs[-1] = (cutoffs[-1][0], hi)

    grid = []
    for y, row in enumerate(noise_map):
        out_row = []
        for x, value in enumerate(row):
            tile_type = cutoffs[-1][0]
            for terrain, cutoff in cutoffs:
                if value <= cutoff:
                    tile_type = terrain
                    break
            out_row.append({
                "id": f"tile-{x}-{y}",
                "type": tile_type,
                "door": None,
            })
        grid.append(out_row)

    return grid


def generate_complex_map(width=DEFAULT_WORLD_WIDTH, height=DEFAULT_WORLD_HEIGHT, seed=None):
    # Build Perlin terrain first, then add in buildings
    grid = generate_perlin_terrain(width, height, seed)

    def get_neighbors(coords, radius=1):
        # Returns all coordinates within a certain radius of the given tiles
        neighbors = set()
        for cx, cy in coords:
            for dy in range(-radius, radius + 1):
                for dx in range(-radius, radius + 1):
                    nx, ny = cx + dx, cy + dy
                    if 0 <= nx < width and 0 <= ny < height:
                        neighbors.add((nx, ny))
        return neighbors

    def place_buildings(count):
        placed = 0
        attempts = 0
        max_attempts = count * 50
        while placed < count and attempts < max_attempts:
            attempts += 1
            bx, by = random.randint(0, width - 1), random.randint(0, height - 1)
            direction = random.choice([(1, 0), (0, 1), (-1, 0), (0, -1)])
            nx, ny = bx + direction[0], by + direction[1]

            if not (0 <= nx < width and 0 <= ny < height):
                continue

            area = get_neighbors([(bx, by), (nx, ny)], radius=1)
            if any(grid[ay][ax]["type"] == "building" for ax, ay in area):
                continue

            grid[by][bx]["type"] = "building"
            grid[ny][nx]["type"] = "building"

            # Buffer around the building becomes grass (also drains any water
            # the building landed on so the door can be reached)
            for ax, ay in area:
                if grid[ay][ax]["type"] != "building":
                    grid[ay][ax]["type"] = "grass"

            # Door faces toward the world's center so it can be interacted with later
            center = width / 2
            dist_b = abs(bx - center) + abs(by - center)
            dist_n = abs(nx - center) + abs(ny - center)
            tx, ty = (bx, by) if dist_b < dist_n else (nx, ny)
            door_tile = grid[ty][tx]
            if abs(tx - center) > abs(ty - center):
                door_tile["door"] = "right" if tx < center else "left"
            else:
                door_tile["door"] = "bottom" if ty < center else "top"

            placed += 1

    area_scale = max(1, (width * height) // 100)
    place_buildings(3 * area_scale)

    return grid
