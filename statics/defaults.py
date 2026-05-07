from dataclasses import dataclass, field


@dataclass
class PluginConfig:
    enable_injection: bool = True

    analysis_interval_minutes: int = 30
    min_new_messages: int = 15

    analysis_provider_id: str = ""
    analysis_secondary_provider_id: str = ""
    analysis_llm_name: str = ""
    analysis_timeout_seconds: float = 60.0

    buffer_max_size: int = 100

    debug_mode: bool = False
    enable_live_perception: bool = False
    enable_live_perception_update: bool = False

    enable_group_mode: bool = False
    unify_cross_session: bool = False
    group_active_days: int = 3
    group_analysis_interval_minutes: int = 120
    group_max_active_users: int = 20

    def to_dict(self) -> dict:
        return {
            "enable_injection": self.enable_injection,
            "analysis_interval_minutes": self.analysis_interval_minutes,
            "min_new_messages": self.min_new_messages,
            "analysis_provider_id": self.analysis_provider_id,
            "analysis_secondary_provider_id": self.analysis_secondary_provider_id,
            "analysis_llm_name": self.analysis_llm_name,
            "analysis_timeout_seconds": self.analysis_timeout_seconds,
            "buffer_max_size": self.buffer_max_size,
            "debug_mode": self.debug_mode,
            "enable_live_perception": self.enable_live_perception,
            "enable_live_perception_update": self.enable_live_perception_update,
            "enable_group_mode": self.enable_group_mode,
            "unify_cross_session": self.unify_cross_session,
            "group_active_days": self.group_active_days,
            "group_analysis_interval_minutes": self.group_analysis_interval_minutes,
            "group_max_active_users": self.group_max_active_users,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "PluginConfig":
        return cls(
            enable_injection=data.get("enable_injection", True),
            analysis_interval_minutes=data.get("analysis_interval_minutes", 30),
            min_new_messages=data.get("min_new_messages", 15),
            analysis_provider_id=data.get("analysis_provider_id", ""),
            analysis_secondary_provider_id=data.get("analysis_secondary_provider_id", ""),
            analysis_llm_name=data.get("analysis_llm_name", ""),
            analysis_timeout_seconds=data.get("analysis_timeout_seconds", 60.0),
            buffer_max_size=data.get("buffer_max_size", 100),
            debug_mode=data.get("debug_mode", False),
            enable_live_perception=data.get("enable_live_perception", False),
            enable_live_perception_update=data.get("enable_live_perception_update", False),
            enable_group_mode=data.get("enable_group_mode", False),
            unify_cross_session=data.get("unify_cross_session", False),
            group_active_days=data.get("group_active_days", 3),
            group_analysis_interval_minutes=data.get("group_analysis_interval_minutes", 120),
            group_max_active_users=data.get("group_max_active_users", 20),
        )


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
