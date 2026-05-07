import json

from astrbot.api import logger

from ..core.tracker import DimensionTracker


class RelationAdminCommands:
    """关系感知管理命令。"""

    def __init__(self, plugin):
        self.plugin = plugin

    async def get_status(self, session_id: str) -> str:
        """获取当前会话的关系状态统计信息。"""
        db = self.plugin.db
        state = await db.get_relation_state_safe(session_id)
        if state is None:
            return "该会话暂无关系数据。"

        lines = [
            "=== 关系感知状态 ===",
            f"会话: {session_id}",
            f"好感度: {state.get('affection', 50):.1f}/100",
            f"信任度: {state.get('trust', 30):.1f}/100",
            f"对话深度: {state.get('depth', 20):.1f}/100",
            f"依赖度: {state.get('dependence', 10):.1f}/100",
            f"回归率: {state.get('return_rate', 0.0):.1f}/100",
            f"关系等级: {state.get('relation_level', '-')}",
            f"一句话: {state.get('summary', '-')}",
            f"最后更新: {state.get('updated_at', '-')}",
        ]
        return "\n".join(lines)

    async def get_history(self, session_id: str, limit: int = 5) -> str:
        """查看最近 N 条分析历史记录。"""
        db = self.plugin.db
        logs = await db.get_recent_analysis(session_id, limit)
        if not logs:
            return "暂无分析历史记录。"

        lines = [f"=== 最近 {len(logs)} 条分析历史 ==="]
        for i, log in enumerate(logs, 1):
            summary = log.get("summary", "-")
            level = ""
            try:
                new_vals = json.loads(log.get("new_values", "{}"))
                tracker = DimensionTracker(self.plugin)
                level = tracker.compute_level(
                    new_vals.get("affection", 50),
                    new_vals.get("trust", 30),
                    new_vals.get("depth", 20),
                )
            except Exception:
                pass
            lines.append(
                f"{i}. [{log.get('created_at', '?')}] "
                f"{level} | {summary} | "
                f"触发={log.get('trigger', '?')}"
            )
        return "\n".join(lines)

    async def reset(self, session_id: str) -> str:
        """重置关系数据。"""
        db = self.plugin.db
        await db.reset_relation_state(session_id)
        logger.info("[RelationSense] 已重置关系数据 session=%s", session_id)
        return f"已重置 {session_id} 的关系数据。"
