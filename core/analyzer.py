import asyncio
import json
import re
import time
from typing import Any, Optional

from astrbot.api import logger

from ..statics.prompts import ANALYZER_SYSTEM_PROMPT, ANALYZER_USER_PROMPT, ANALYZER_USER_PROMPT_INITIAL, BACKFILL_USER_PROMPT, GROUP_ANALYZER_SYSTEM_PROMPT, GROUP_ANALYZER_USER_PROMPT, GROUP_BATCH_ANALYZER_SYSTEM_PROMPT, GROUP_BATCH_ANALYZER_USER_PROMPT


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

    match = re.search(r"\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}", text)
    if match:
        try:
            return json.loads(match.group(0))
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
        is_initial: bool = False,
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

        system_prompt = ANALYZER_SYSTEM_PROMPT

        if is_initial:
            user_prompt = ANALYZER_USER_PROMPT_INITIAL.format(
                user_name=user_name,
                persona_prompt=persona_prompt if persona_prompt else "（无人设提示）",
                dialogue_text=dialogue_text,
            )
        else:
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

        system_prompt = ANALYZER_SYSTEM_PROMPT

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

    async def analyze_group(
        self,
        session_id: str,
        dialogue_text: str,
        current_values: dict,
        target_name: str = "用户",
        target_id: str = "",
        persona_prompt: str = "",
    ) -> Optional[dict]:
        primary = str(self._cfg("analysis_provider_id", "") or "").strip()
        secondary = str(self._cfg("analysis_secondary_provider_id", "") or "").strip()
        timeout = float(self._cfg("analysis_timeout_seconds", 60.0) or 60.0)

        if not primary and not secondary:
            logger.warning("[RelationSense] 未配置分析模型，跳过群聊分析")
            return None

        system_prompt = GROUP_ANALYZER_SYSTEM_PROMPT

        user_prompt = GROUP_ANALYZER_USER_PROMPT.format(
            target_name=target_name,
            target_id=target_id,
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
                    "[RelationSense] 群聊分析完成 提供商=%s 耗时=%.2f秒 目标=%s(%s)",
                    provider_id, elapsed, target_name, target_id,
                )
                if result:
                    return result
                logger.warning(
                    "[RelationSense] 群聊分析结果解析失败 提供商=%s，尝试备用",
                    provider_id,
                )
            except asyncio.TimeoutError:
                logger.warning("[RelationSense] 群聊分析超时 提供商=%s", provider_id)
            except Exception as e:
                logger.warning(
                    "[RelationSense] 群聊分析异常 提供商=%s 错误=%s",
                    provider_id, e,
                )

        logger.error("[RelationSense] 群聊分析：所有模型均不可用")
        return None

    async def analyze_group_batch(
        self,
        dialogue_text: str,
        target_users: list[dict],
        persona_prompt: str = "",
    ) -> Optional[list[dict]]:
        primary = str(self._cfg("analysis_provider_id", "") or "").strip()
        secondary = str(self._cfg("analysis_secondary_provider_id", "") or "").strip()
        timeout = float(self._cfg("analysis_timeout_seconds", 60.0) or 60.0)

        if not primary and not secondary:
            logger.warning("[RelationSense] 未配置分析模型，跳过群聊批量分析")
            return None

        system_prompt = GROUP_BATCH_ANALYZER_SYSTEM_PROMPT

        users_list_parts = []
        for u in target_users:
            name = u.get("user_name", u.get("user_id", "未知"))
            uid = u.get("user_id", "")
            users_list_parts.append(f"- {name}(ID:{uid})")
        target_users_list = "\n".join(users_list_parts)

        user_prompt = GROUP_BATCH_ANALYZER_USER_PROMPT.format(
            persona_prompt=persona_prompt if persona_prompt else "（无人设提示）",
            target_users_list=target_users_list,
            dialogue_text=dialogue_text,
        )

        providers = [p for p in [primary, secondary] if p]
        for provider_id in providers:
            try:
                t0 = time.perf_counter()
                raw_result = await asyncio.wait_for(
                    self._call_llm_raw(provider_id, system_prompt, user_prompt),
                    timeout=timeout,
                )
                elapsed = time.perf_counter() - t0
                logger.info(
                    "[RelationSense] 群聊批量分析完成 提供商=%s 耗时=%.2f秒 用户数=%d",
                    provider_id, elapsed, len(target_users),
                )
                if raw_result:
                    return raw_result
                logger.warning(
                    "[RelationSense] 群聊批量分析结果解析失败 提供商=%s",
                    provider_id,
                )
            except asyncio.TimeoutError:
                logger.warning("[RelationSense] 群聊批量分析超时 提供商=%s", provider_id)
            except Exception as e:
                logger.warning(
                    "[RelationSense] 群聊批量分析异常 提供商=%s 错误=%s",
                    provider_id, e,
                )

        logger.error("[RelationSense] 群聊批量分析：所有模型均不可用")
        return None

    async def _call_llm_raw(
        self,
        provider_id: str,
        system_prompt: str,
        user_prompt: str,
    ) -> Optional[list[dict]]:
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

        return _parse_json_array_response(raw_text)


def _parse_json_array_response(raw_text: str) -> Optional[list[dict]]:
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
        result = json.loads(text)
        if isinstance(result, list):
            return result
    except json.JSONDecodeError:
        pass

    match = re.search(r"\[[^[\]]*(?:\[[^[\]]*\][^[\]]*)*\]", text)
    if match:
        try:
            result = json.loads(match.group(0))
            if isinstance(result, list):
                return result
        except json.JSONDecodeError:
            pass

    match = re.search(r"\[[\s\S]*\]", text)
    if match:
        try:
            result = json.loads(match.group(0))
            if isinstance(result, list):
                return result
        except json.JSONDecodeError:
            pass

    return None
