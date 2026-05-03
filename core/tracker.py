import json
from typing import Optional

from astrbot.api import logger

from ..statics.defaults import DIMENSION_KEYS, LEVEL_WEIGHTS, MAX_DELTA_PER_ROUND, RELATION_LEVELS


class DimensionTracker:
    """五维数值计算、冻结管理、等级判定。"""

    def __init__(self, plugin=None):
        self.plugin = plugin

    def _cfg(self, key: str, default=None):
        if self.plugin:
            return self.plugin.config.get(key, default)
        return default

    def compute_level(self, affection: float, trust: float, depth: float) -> str:
        """根据三核加权计算关系等级。

        Args:
            affection: 好感度
            trust: 信任度
            depth: 对话深度

        Returns:
            等级标签，如 "Lv3"
        """
        weighted = (
            affection * LEVEL_WEIGHTS["affection"]
            + trust * LEVEL_WEIGHTS["trust"]
            + depth * LEVEL_WEIGHTS["depth"]
        )

        for low, high, level, _ in RELATION_LEVELS:
            if low <= weighted < high:
                return level

        return "Lv1"

    def compute_label(self, level: str) -> str:
        """获取等级对应的中文标签。"""
        for _, _, l, label in RELATION_LEVELS:
            if l == level:
                return label
        return "未知"

    def is_dimension_frozen(self, dim_name: str, score: float) -> bool:
        """判断某维度是否达到冻结阈值。

        Args:
            dim_name: 'affection' 或 'trust'
            score: 当前数值
        """
        if dim_name == "affection":
            threshold = float(self._cfg("affection_freeze_threshold", 90.0))
            return score >= threshold
        if dim_name == "trust":
            threshold = float(self._cfg("trust_freeze_threshold", 88.0))
            return score >= threshold
        return False

    def apply_analysis_result(
        self,
        current_values: dict,
        analysis_result: dict,
        is_initial: bool = False,
    ) -> tuple[dict, bool]:
        """将 LLM 分析结果应用到五维数值上。

        Args:
            current_values: 当前五维 dict，如 {"affection": 72, "trust": 55, ...}
            analysis_result: LLM 解析的 JSON dict

        Returns:
            (new_values dict, has_changes bool)
        """
        new_values = dict(current_values)
        has_changes = False

        for dim in DIMENSION_KEYS:
            dim_data = analysis_result.get(dim, {})
            if not isinstance(dim_data, dict):
                continue

            frozen = dim_data.get("frozen", False)
            if frozen:
                logger.debug("[RelationSense] 维度 %s 已冻结，跳过更新", dim)
                continue

            if dim in ("affection", "trust") and self.is_dimension_frozen(
                dim, current_values.get(dim, 0)
            ):
                logger.debug("[RelationSense] 维度 %s 已达本地冻结阈值，跳过更新", dim)
                continue

            new_score = dim_data.get("score")
            if new_score is not None and isinstance(new_score, (int, float)):
                clamped = max(0.0, min(100.0, float(new_score)))
                old_val = new_values.get(dim, 0)
                if not is_initial:
                    delta = clamped - old_val
                    max_delta = MAX_DELTA_PER_ROUND.get(dim, 20)
                    if abs(delta) > max_delta:
                        direction = 1 if delta > 0 else -1
                        clamped = old_val + direction * max_delta
                        logger.debug(
                            "[RelationSense] 维度 %s 变化 %.1f 超过上限 %.1f，截断为 %.1f",
                            dim, delta, max_delta, clamped,
                        )
                if abs(clamped - old_val) > 0.01:
                    new_values[dim] = round(clamped, 1)
                    has_changes = True

        return new_values, has_changes

    def clamp_all(self, values: dict) -> dict:
        """确保所有维度值在 0-100 范围内。"""
        result = {}
        for k, v in values.items():
            result[k] = max(0.0, min(100.0, float(v)))
        return result
