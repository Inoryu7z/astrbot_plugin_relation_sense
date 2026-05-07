import time
from typing import Optional, Callable, Any

from astrbot.api import logger

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

