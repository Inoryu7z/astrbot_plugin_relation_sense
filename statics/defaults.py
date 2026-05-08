DEFAULT_FIVE_DIMS = {
    "affection": 50.0,
    "trust": 30.0,
    "depth": 20.0,
    "dependence": 10.0,
    "return_rate": 0.0,
}

DEFAULT_LEVEL = "Lv0"

RELATION_LEVELS = [
    (0, 12, "Lv-2", "敌意"),
    (12, 24, "Lv-1", "厌恶"),
    (24, 36, "Lv0", "冷淡"),
    (36, 50, "Lv1", "陌路"),
    (50, 63, "Lv2", "初识"),
    (63, 75, "Lv3", "认识"),
    (75, 87, "Lv4", "朋友"),
    (87, 97, "Lv5", "好友"),
    (97, 101, "Lv6", "亲密"),
]

LEVEL_WEIGHTS = {
    "affection": 0.40,
    "trust": 0.35,
    "depth": 0.25,
}

DIMENSION_KEYS = ["affection", "trust", "depth", "dependence", "return_rate"]

MAX_DELTA_PER_ROUND = {
    "affection": 12,
    "trust": 8,
    "depth": 20,
    "dependence": 10,
    "return_rate": 5,
}

COOLING_INACTIVITY_HOURS = 12
COOLING_DEPTH_DECAY = 2.0
COOLING_DEPENDENCE_DECAY = 1.5
