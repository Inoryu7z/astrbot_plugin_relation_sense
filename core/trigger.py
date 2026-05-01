import time
from typing import Optional, Callable, Any

from astrbot.api import logger

from ..statics.prompts import EVENT_TRIGGER_KEYWORDS
from ..storage.db import RelationDatabase


class AnalysisTrigger:
    def __init__(
        self,
        db: RelationDatabase,
        plugin=None,
    ):
        self.db = db
        self.plugin = plugin

    def _cfg(self, key: str, default=None):
        if self.plugin:
            return self.plugin.config.get(key, default)
        return default

    async def should_analyze(self, session_id: str) -> bool:
        """判断是否满足常规触发条件：距上次分析 ≥ interval 分钟 + 新增消息 ≥ min_new 条。"""
        interval_minutes = int(self._cfg("analysis_interval_minutes", 30))
        min_new = int(self._cfg("min_new_messages", 15))

        last_ts = await self.db.get_last_analysis_at(session_id)
        elapsed = time.time() - last_ts
        if elapsed < interval_minutes * 60:
            return False

        new_count = await self.db.get_msg_count_since_last(session_id)
        return new_count >= min_new

    def detect_event_trigger(self, text: str) -> Optional[tuple[str, str]]:
        """
        检测是否有关键事件触发词。
        返回 (事件类型, 匹配关键词) 或 None。

        事件类型: 'conflict', 'deep_secret', 'return'
        """
        if not text:
            return None

        if not self._cfg("enable_event_trigger", True):
            return None

        for event_type, keywords in EVENT_TRIGGER_KEYWORDS.items():
            for kw in keywords:
                if kw in text:
                    logger.debug(
                        "[RelationSense] 检测到关键事件触发 类型=%s 关键词=%s",
                        event_type, kw,
                    )
                    return (event_type, kw)
        return None
