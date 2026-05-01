import asyncio
import json
import re
import time
from typing import Any, Optional

from astrbot.api import logger

from ..statics.prompts import ANALYZER_SYSTEM_PROMPT, ANALYZER_USER_PROMPT, BACKFILL_USER_PROMPT


def _parse_json_response(raw_text: str) -> Optional[dict]:
    """从 LLM 原始文本中提取 JSON 对象。"""
    if not raw_text:
        return None
    text = raw_text.strip()

    if text.startswith("```"):
        lines = text.split("\n")
        if len(lines) > 1:
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    match = re.search(r"\{[\s\S]*\}", text)
    if match:
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            pass

    return None


class RelationAnalyzer:
    def __init__(self, context, plugin=None):
        self.context = context
        self.plugin = plugin

    def _cfg(self, key: str, default=None):
        if self.plugin:
            return self.plugin.config.get(key, default)
        return default

    async def analyze(
        self,
        session_id: str,
        dialogue_text: str,
        current_values: dict,
        bot_name: str = "Bot",
        user_name: str = "用户",
        persona_prompt: str = "",
    ) -> Optional[dict]:
        """执行关系分析。

        Args:
            session_id: 会话标识
            dialogue_text: 对话文本
            current_values: 当前五维数值
            bot_name: Bot 名称
            user_name: 用户名
            persona_prompt: Bot 人设提示词

        Returns:
            解析后的分析结果 dict，失败时返回 None
        """
        primary = str(self._cfg("analysis_provider_id", "") or "").strip()
        secondary = str(self._cfg("analysis_secondary_provider_id", "") or "").strip()
        timeout = float(self._cfg("analysis_timeout_seconds", 60.0) or 60.0)

        if not primary and not secondary:
            logger.warning("[RelationSense] 未配置分析模型，跳过分析")
            return None

        affection_threshold = float(self._cfg("affection_freeze_threshold", 90.0))
        trust_threshold = float(self._cfg("trust_freeze_threshold", 88.0))

        system_prompt = ANALYZER_SYSTEM_PROMPT.format(
            affection_threshold=affection_threshold,
            trust_threshold=trust_threshold,
        )

        user_prompt = ANALYZER_USER_PROMPT.format(
            bot_name=bot_name,
            user_name=user_name,
            persona_prompt=persona_prompt if persona_prompt else "（无人设提示）",
            dialogue_text=dialogue_text,
            affection=current_values.get("affection", 50),
            trust=current_values.get("trust", 30),
            depth=current_values.get("depth", 20),
            dependence=current_values.get("dependence", 10),
            return_rate=current_values.get("return_rate", 0),
        )

        providers = [p for p in [primary, secondary] if p]
        for provider_id in providers:
            try:
                t0 = time.perf_counter()
                result = await asyncio.wait_for(
                    self._call_llm(provider_id, system_prompt, user_prompt),
                    timeout=timeout,
                )
                elapsed = time.perf_counter() - t0
                logger.info(
                    "[RelationSense] LLM 调用完成 提供商=%s 耗时=%.2f秒 会话=%s",
                    provider_id, elapsed, session_id,
                )
                if result:
                    return result
                logger.warning(
                    "[RelationSense] 模型返回结果解析失败 提供商=%s，尝试备用",
                    provider_id,
                )
            except asyncio.TimeoutError:
                logger.warning("[RelationSense] 分析调用超时 提供商=%s", provider_id)
            except Exception as e:
                logger.warning(
                    "[RelationSense] 分析调用异常 提供商=%s 错误=%s",
                    provider_id, e,
                )

        logger.error("[RelationSense] 所有分析模型均不可用")
        return None

    async def backfill_analyze(
        self,
        session_id: str,
        dialogue_text: str,
        persona_prompt: str = "",
        bot_name: str = "Bot",
        user_name: str = "用户",
    ) -> Optional[dict]:
        """回溯初始化分析。"""
        primary = str(self._cfg("analysis_provider_id", "") or "").strip()
        secondary = str(self._cfg("analysis_secondary_provider_id", "") or "").strip()
        timeout = float(self._cfg("analysis_timeout_seconds", 60.0) or 60.0)

        if not primary and not secondary:
            logger.warning("[RelationSense] 未配置分析模型，跳过回溯分析")
            return None

        affection_threshold = float(self._cfg("affection_freeze_threshold", 90.0))
        trust_threshold = float(self._cfg("trust_freeze_threshold", 88.0))

        system_prompt = ANALYZER_SYSTEM_PROMPT.format(
            affection_threshold=affection_threshold,
            trust_threshold=trust_threshold,
        )

        user_prompt = BACKFILL_USER_PROMPT.format(
            bot_name=bot_name,
            user_name=user_name,
            persona_prompt=persona_prompt if persona_prompt else "（无人设提示）",
            dialogue_text=dialogue_text,
        )

        providers = [p for p in [primary, secondary] if p]
        for provider_id in providers:
            try:
                t0 = time.perf_counter()
                result = await asyncio.wait_for(
                    self._call_llm(provider_id, system_prompt, user_prompt),
                    timeout=timeout,
                )
                elapsed = time.perf_counter() - t0
                logger.info(
                    "[RelationSense] 回溯分析完成 提供商=%s 耗时=%.2f秒 会话=%s",
                    provider_id, elapsed, session_id,
                )
                if result:
                    return result
            except asyncio.TimeoutError:
                logger.warning("[RelationSense] 回溯分析超时 提供商=%s", provider_id)
            except Exception as e:
                logger.warning(
                    "[RelationSense] 回溯分析调用异常 提供商=%s 错误=%s",
                    provider_id, e,
                )

        logger.error("[RelationSense] 回溯分析：所有模型均不可用")
        return None

    async def _call_llm(
        self,
        provider_id: str,
        system_prompt: str,
        user_prompt: str,
    ) -> Optional[dict]:
        """调用 LLM 并解析返回的 JSON。"""
        try:
            llm_resp = await self.context.llm_generate(
                chat_provider_id=provider_id,
                prompt=user_prompt,
                system_prompt=system_prompt,
            )
        except Exception as e:
            logger.warning("[RelationSense] LLM 调用异常: %s", e)
            raise

        raw_text = (getattr(llm_resp, "completion_text", "") or "").strip()
        if not raw_text:
            return None

        return _parse_json_response(raw_text)
