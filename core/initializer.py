import asyncio
import json

from astrbot.api import logger

from ..storage.db import RelationDatabase


class RelationInitializer:
    """插件首次加载时的回溯初始化：从 AstrBot 历史中取消息，推断初始关系状态。"""

    def __init__(self, context, db: RelationDatabase, plugin=None):
        self.context = context
        self.db = db
        self.plugin = plugin

    async def initialize_session(
        self,
        session_id: str,
        platform_id: str,
        user_id: str,
        bot_name: str = "Bot",
        user_name: str = "用户",
        persona_prompt: str = "",
    ) -> bool:
        """对指定会话执行回溯初始化。

        从 AstrBot message_history_manager 取最近 50-80 条历史消息，
        调用分析 LLM 推断初始关系状态。

        Args:
            session_id: unified_msg_origin
            platform_id: 平台 ID
            user_id: 用户 ID
            bot_name: Bot 名称
            user_name: 用户名称
            persona_prompt: 人格提示词

        Returns:
            True 表示回溯成功，False 表示冷启动
        """
        from ..core.tracker import DimensionTracker
        tracker = DimensionTracker(self.plugin)
        cold_affection, cold_trust, cold_depth = 60.0, 50.0, 35.0
        cold_dependence, cold_return_rate = 10.0, 5.0
        cold_level = tracker.compute_level(cold_affection, cold_trust, cold_depth)

        dialogue_text = await self._fetch_history(platform_id, user_id)
        if not dialogue_text:
            logger.info(
                "[RelationSense] 未获取到历史消息 session=%s，使用冷启动默认值（等级=%s）",
                session_id, cold_level,
            )
            await self.db.upsert_relation_state(
                session_id=session_id,
                affection=cold_affection,
                trust=cold_trust,
                depth=cold_depth,
                dependence=cold_dependence,
                return_rate=cold_return_rate,
                relation_level=cold_level,
                summary="",
            )
            await self.db.add_analysis_log(
                session_id=session_id,
                raw_json=json.dumps({}),
                old_values="{}",
                new_values=json.dumps({
                    "affection": cold_affection,
                    "trust": cold_trust,
                    "depth": cold_depth,
                    "dependence": cold_dependence,
                    "return_rate": cold_return_rate,
                }),
                summary="冷启动初始化",
                trigger="backfill",
                source="cold_start",
            )
            return False

        # 调用分析 LLM（延迟导入避免循环）
        from .analyzer import RelationAnalyzer
        analyzer = RelationAnalyzer(self.context, self.plugin)
        result = await analyzer.backfill_analyze(
            session_id=session_id,
            dialogue_text=dialogue_text,
            persona_prompt=persona_prompt,
            bot_name=bot_name,
            user_name=user_name,
        )

        if not result:
            logger.warning("[RelationSense] 回溯分析失败 session=%s，使用默认值（等级=%s）", session_id, cold_level)
            await self.db.upsert_relation_state(
                session_id=session_id,
                affection=cold_affection,
                trust=cold_trust,
                depth=cold_depth,
                dependence=cold_dependence,
                return_rate=cold_return_rate,
                relation_level=cold_level,
                summary="",
            )
            await self.db.add_analysis_log(
                session_id=session_id,
                raw_json=json.dumps({}),
                old_values="{}",
                new_values=json.dumps({
                    "affection": cold_affection,
                    "trust": cold_trust,
                    "depth": cold_depth,
                    "dependence": cold_dependence,
                    "return_rate": cold_return_rate,
                }),
                summary="回溯分析失败，回退冷启动",
                trigger="backfill",
                source="cold_start",
            )
            return False

        # 从结果中提取五维数值
        from ..core.tracker import DimensionTracker
        tracker = DimensionTracker(self.plugin)

        new_affection = float(_safe_get_score(result, "affection", 50))
        new_trust = float(_safe_get_score(result, "trust", 30))
        new_depth = float(_safe_get_score(result, "depth", 20))
        new_dependence = float(_safe_get_score(result, "dependence", 10))
        new_return_rate = float(_safe_get_score(result, "return_rate", 0))
        new_summary = result.get("summary", "")

        levels = tracker.compute_level(new_affection, new_trust, new_depth)

        await self.db.upsert_relation_state(
            session_id=session_id,
            affection=new_affection,
            trust=new_trust,
            depth=new_depth,
            dependence=new_dependence,
            return_rate=new_return_rate,
            relation_level=levels,
            summary=new_summary,
        )

        old_vals = json.dumps({"affection": 50, "trust": 30, "depth": 20, "dependence": 10, "return_rate": 0})
        new_vals = json.dumps({
            "affection": new_affection,
            "trust": new_trust,
            "depth": new_depth,
            "dependence": new_dependence,
            "return_rate": new_return_rate,
        })

        await self.db.add_analysis_log(
            session_id=session_id,
            raw_json=json.dumps(result, ensure_ascii=False),
            old_values=old_vals,
            new_values=new_vals,
            summary=new_summary,
            confidence=float(result.get("confidence", 0.0)),
            trigger="backfill",
            source="history_backfill",
        )

        logger.info(
            "[RelationSense] 回溯初始化成功 会话=%s 好感度=%.1f 信任度=%.1f 等级=%s",
            session_id, new_affection, new_trust, levels,
        )
        return True

    async def _fetch_history(self, platform_id: str, user_id: str) -> str:
        """从 AstrBot message_history_manager 获取最近 50-80 条双方消息。

        Returns:
            格式化的对话文本，如 "用户: xxx\nBot: xxx\n..."，获取失败返回空字符串
        """
        try:
            if not hasattr(self.context, "message_history_manager"):
                logger.debug("[RelationSense] message_history_manager 不可用")
                return ""

            manager = self.context.message_history_manager
            if not manager:
                return ""

            page_size = 80
            history = await manager.get(
                platform_id=platform_id,
                user_id=user_id,
                page=1,
                page_size=page_size,
            )

            if not history:
                return ""

            lines = []
            for item in history:
                sender_name = _extract_sender(item)
                content = _extract_content(item)
                if content and content.strip():
                    lines.append(f"{sender_name}: {content.strip()}")

            return "\n".join(lines)
        except Exception as e:
            logger.warning("[RelationSense] 获取历史消息失败: %s", e)
            return ""


def _safe_get_score(result: dict, dim: str, default: float = 50.0) -> float:
    """安全地从分析结果中提取维度分数。"""
    dim_data = result.get(dim, {})
    if isinstance(dim_data, dict):
        return dim_data.get("score", default)
    if isinstance(dim_data, (int, float)):
        return float(dim_data)
    return default


def _extract_sender(item) -> str:
    """从历史消息 item 中提取发送者名称。"""
    if isinstance(item, dict):
        sender = item.get("sender_name") or item.get("sender", {}).get("nickname", "")
        if not sender:
            sender_id = str(item.get("sender_id") or item.get("sender", {}).get("user_id", ""))
            return sender_id or "未知"
        return sender
    for attr in ("sender_name", "sender_id"):
        val = getattr(item, attr, None)
        if val:
            return str(val)
    sender = getattr(item, "sender", None)
    if sender:
        for attr in ("nickname", "user_id"):
            val = getattr(sender, attr, None)
            if val:
                return str(val)
    return "未知"


def _extract_content(item) -> str:
    """从历史消息 item 中提取消息内容。"""
    if isinstance(item, dict):
        return str(item.get("content") or item.get("message_str") or item.get("text", ""))
    for attr in ("content", "message_str", "text", "completion_text"):
        val = getattr(item, attr, None)
        if val:
            return str(val)
    return ""
