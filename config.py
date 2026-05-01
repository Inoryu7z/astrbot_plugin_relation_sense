from .statics.defaults import PluginConfig


def load_config(raw_config: dict) -> PluginConfig:
    if not raw_config:
        return PluginConfig()
    return PluginConfig.from_dict(raw_config)
