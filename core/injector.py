from typing import Optional

from ..statics.prompts import (
    INJECTION_AMBIGUOUS,
    INJECTION_CONFLICT,
    INJECTION_MINIMAL,
    INJECTION_NORMAL,
    INJECTION_RS_DIRECT,
    INJECTION_SILENCE,
    GROUP_INJECTION_AMBIGUOUS,
    GROUP_INJECTION_CONFLICT,
    GROUP_INJECTION_MINIMAL,
    GROUP_INJECTION_NORMAL,
    GROUP_INJECTION_RS_DIRECT,
    GROUP_INJECTION_SILENCE,
)


class RelationInjector:
    """构建注入到 system_prompt 的关系上下文文本，支持多场景策略。"""

    def __init__(self, plugin=None):
        self.plugin = plugin

    def _cfg(self, key: str, default=None):
        if self.plugin:
            return self.plugin.config.get(key, default)
        return default

    def build_injection(self, state: dict, scenario: str = "normal", rs_driven: bool = False) -> Optional[str]:
        """根据关系状态和当前场景构建注入文本。

        Args:
            state: relation_state 的 dict
            scenario: 场景标签
                - "minimal": 好感信任双满
                - "conflict": 刚发生冲突
                - "ambiguous": 好感在 60-75 暧昧区间
                - "silence": 用户话很少/敷衍
                - "normal": 默认
            rs_driven: 是否由对话模型的 <rs> 标签驱动（跳过场景模板）

        Returns:
            注入文本，不需要注入时返回 None
        """
        if not self._cfg("enable_injection", True):
            return None

        affection = state.get("affection", 0)
        trust = state.get("trust", 0)
        summary = state.get("summary", "")
        user_state = state.get("user_state", "")

        rs_atmosphere = state.get("_rs_atmosphere", "")

        if rs_driven and rs_atmosphere:
            tone_hint = state.get("tone_hint", "保持自然语气回应")
            user_state_text = user_state or summary or ""
            return INJECTION_RS_DIRECT.format(
                user_state=user_state_text,
                atmosphere=rs_atmosphere,
                tone_hint=tone_hint,
            )

        if scenario == "minimal" or (affection >= 90 and trust >= 88):
            user_state_text = summary or user_state
            return INJECTION_MINIMAL.format(
                user_state=user_state_text if user_state_text else "对方今天主动找你聊天。"
            )

        if scenario == "conflict":
            return INJECTION_CONFLICT.format(
                user_state=user_state or summary or ""
            )

        if scenario == "ambiguous":
            return INJECTION_AMBIGUOUS.format(
                user_state=user_state or summary or ""
            )

        if scenario == "silence":
            return INJECTION_SILENCE.format(
                user_state=user_state or summary or ""
            )

        atmosphere = rs_atmosphere or self._derive_atmosphere(state)
        tone_hint = state.get("tone_hint", "保持自然语气回应")
        user_state_text = user_state or summary or ""
        return INJECTION_NORMAL.format(
            user_state=user_state_text,
            atmosphere=atmosphere,
            tone_hint=tone_hint,
        )

    @staticmethod
    def _derive_atmosphere(state: dict) -> str:
        depth = state.get("depth", 20)
        dependence = state.get("dependence", 10)

        if depth > 70 and dependence > 60:
            return "亲密、深聊、相互依赖"
        if depth > 70:
            return "深聊、坦诚"
        if dependence > 60:
            return "黏人、依赖、撒娇"
        if depth > 40:
            return "轻松、友好"
        if depth > 20:
            return "日常、闲聊"
        return "平淡、初识"

    def build_group_injection(
        self,
        state: dict,
        sender_name: str,
        active_users_summary: str = "",
        scenario: str = "normal",
        rs_driven: bool = False,
    ) -> Optional[str]:
        if not self._cfg("enable_injection", True):
            return None

        affection = state.get("affection", 0)
        trust = state.get("trust", 0)
        user_state = state.get("user_state", "")
        tone_hint = state.get("tone_hint", "")

        fmt_kwargs = {
            "sender_name": sender_name,
            "sender_user_state": user_state or "",
            "sender_tone_hint": tone_hint or "保持自然语气回应",
            "active_users_summary": active_users_summary or "无",
            "atmosphere": state.get("_rs_atmosphere", ""),
        }

        if rs_driven and fmt_kwargs["atmosphere"]:
            return GROUP_INJECTION_RS_DIRECT.format(**fmt_kwargs)

        if scenario == "minimal" or (affection >= 90 and trust >= 88):
            return GROUP_INJECTION_MINIMAL.format(**fmt_kwargs)

        if scenario == "conflict":
            return GROUP_INJECTION_CONFLICT.format(**fmt_kwargs)

        if scenario == "ambiguous":
            return GROUP_INJECTION_AMBIGUOUS.format(**fmt_kwargs)

        if scenario == "silence":
            return GROUP_INJECTION_SILENCE.format(**fmt_kwargs)

        return GROUP_INJECTION_NORMAL.format(**fmt_kwargs)
