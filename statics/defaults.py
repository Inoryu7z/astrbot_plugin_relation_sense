from dataclasses import dataclass, field


@dataclass
class PluginConfig:
    enable_plugin: bool = True
    enable_injection: bool = True

    analysis_interval_minutes: int = 30
    min_new_messages: int = 15
    enable_event_trigger: bool = True

    affection_freeze_threshold: float = 90.0
    trust_freeze_threshold: float = 88.0

    analysis_provider_id: str = ""
    analysis_secondary_provider_id: str = ""
    analysis_llm_name: str = ""
    analysis_timeout_seconds: float = 60.0

    history_retention_days: int = 60
    buffer_max_size: int = 100

    debug_mode: bool = False
    enable_live_perception: bool = False
    enable_live_perception_update: bool = False

    def to_dict(self) -> dict:
        return {
            "enable_plugin": self.enable_plugin,
            "enable_injection": self.enable_injection,
            "analysis_interval_minutes": self.analysis_interval_minutes,
            "min_new_messages": self.min_new_messages,
            "enable_event_trigger": self.enable_event_trigger,
            "affection_freeze_threshold": self.affection_freeze_threshold,
            "trust_freeze_threshold": self.trust_freeze_threshold,
            "analysis_provider_id": self.analysis_provider_id,
            "analysis_secondary_provider_id": self.analysis_secondary_provider_id,
            "analysis_llm_name": self.analysis_llm_name,
            "analysis_timeout_seconds": self.analysis_timeout_seconds,
            "history_retention_days": self.history_retention_days,
            "buffer_max_size": self.buffer_max_size,
            "debug_mode": self.debug_mode,
            "enable_live_perception": self.enable_live_perception,
            "enable_live_perception_update": self.enable_live_perception_update,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "PluginConfig":
        return cls(
            enable_plugin=data.get("enable_plugin", True),
            enable_injection=data.get("enable_injection", True),
            analysis_interval_minutes=data.get("analysis_interval_minutes", 30),
            min_new_messages=data.get("min_new_messages", 15),
            enable_event_trigger=data.get("enable_event_trigger", True),
            affection_freeze_threshold=data.get("affection_freeze_threshold", 90.0),
            trust_freeze_threshold=data.get("trust_freeze_threshold", 88.0),
            analysis_provider_id=data.get("analysis_provider_id", ""),
            analysis_secondary_provider_id=data.get("analysis_secondary_provider_id", ""),
            analysis_llm_name=data.get("analysis_llm_name", ""),
            analysis_timeout_seconds=data.get("analysis_timeout_seconds", 60.0),
            history_retention_days=data.get("history_retention_days", 60),
            buffer_max_size=data.get("buffer_max_size", 100),
            debug_mode=data.get("debug_mode", False),
            enable_live_perception=data.get("enable_live_perception", False),
            enable_live_perception_update=data.get("enable_live_perception_update", False),
        )


DEFAULT_FIVE_DIMS = {
    "affection": 50.0,
    "trust": 30.0,
    "depth": 20.0,
    "dependence": 10.0,
    "return_rate": 0.0,
}

DEFAULT_LEVEL = "Lv1"

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
