import asyncio
import json
import re
import time
from pathlib import Path
from typing import Optional

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register
from astrbot.core.provider.entities import ProviderRequest
from astrbot.core.utils.astrbot_path import get_astrbot_data_path

from .core.buffer import MessageBuffer
from .core.analyzer import RelationAnalyzer
from .core.trigger import AnalysisTrigger
from .core.tracker import DimensionTracker
from .core.injector import RelationInjector
from .storage.db import RelationDatabase
from .commands.admin import RelationAdminCommands
from .statics.defaults import COOLING_DEPENDENCE_DECAY, COOLING_DEPTH_DECAY, COOLING_INACTIVITY_HOURS
from .statics.prompts import LIVE_PERCEPTION_PROMPT, LIVE_PERCEPTION_UPDATE_PROMPT

RS_INJECTION_HEADER = "<!-- RS_Injection -->"
RS_INJECTION_FOOTER = "<!-- /RS_Injection -->"

_RS_INJECTION_PATTERN = re.compile(
    re.escape(RS_INJECTION_HEADER) + r".*?" + re.escape(RS_INJECTION_FOOTER),
    flags=re.DOTALL,
)


def _remove_rs_injection(system_prompt):
    if not system_prompt:
        return "", False
    cleaned = _RS_INJECTION_PATTERN.sub("", system_prompt)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    return cleaned, cleaned != system_prompt


@register(
    "astrbot_plugin_relation_sense",
    "Inoryu7z",
    "关系感知插件，感知与用户的关系亲密度、对方画像与对话氛围",
    "1.6.0",
)
class RelationSensePlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig = None):
        super().__init__(context)
        self.config = config or {}

        data_dir = Path(get_astrbot_data_path()) / "plugin_data" / "astrbot_plugin_relation_sense"
        data_dir.mkdir(parents=True, exist_ok=True)

        self.db = RelationDatabase(data_dir)
        self.buffer = MessageBuffer(
            max_size=int(self.config.get("buffer_max_size", 100))
        )
        self.analyzer = RelationAnalyzer(context, plugin=self)
        self.trigger = AnalysisTrigger(self.db, plugin=self)
        self.tracker = DimensionTracker(plugin=self)
        self.injector = RelationInjector(plugin=self)
        self.admin = RelationAdminCommands(plugin=self)
        self.data_dir = data_dir

        self._bg_tasks: set[asyncio.Task] = set()
        self._analysis_locks: dict[str, asyncio.Lock] = {}
        self._lock_last_used: dict[str, float] = {}
        self._last_persona: dict[str, str] = {}
        self._last_affection_change: dict[str, float] = {}
        self._last_activity: dict[str, float] = {}
        self._scenario_flags: dict[str, str] = {}
        self._live_user_state: dict[str, str] = {}
        self._group_user_last_analyzed: dict[str, float] = {}
        self._last_request_session: dict[str, tuple[str, bool]] = {}
        self._last_cooled: dict[str, float] = {}

        logger.info("[RelationSense] 插件初始化完成")

    async def initialize(self):
        logger.info("[RelationSense] 插件启动，数据库就绪")
        if self._cfg("enable_dialogue_static_update", False) and not self._cfg("enable_live_perception", False):
            logger.warning("[RelationSense] enable_dialogue_static_update 需要先开启 enable_live_perception，当前未生效")
        self._spawn_bg(self._cleanup_loop())
        self._spawn_bg(self._cooling_loop())
        if self._cfg("enable_group_mode", False):
            self._spawn_bg(self._group_batch_analysis_loop())
            logger.info("[RelationSense] 群聊模式已启用，批量分析循环已启动")

    async def terminate(self):
        logger.info("[RelationSense] 插件卸载中，等待后台任务结束…")
        for task in self._bg_tasks:
            task.cancel()
        if self._bg_tasks:
            await asyncio.gather(*self._bg_tasks, return_exceptions=True)
        logger.info("[RelationSense] 插件已卸载")

    def _cfg(self, key: str, default=None):
        return self.config.get(key, default)

    def _spawn_bg(self, coro):
        task = asyncio.create_task(coro)
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)
        return task

    def _get_lock(self, session_id: str) -> asyncio.Lock:
        if session_id not in self._analysis_locks:
            self._analysis_locks[session_id] = asyncio.Lock()
        self._lock_last_used[session_id] = time.time()
        return self._analysis_locks[session_id]

    def _resolve_session_key(self, event: AstrMessageEvent) -> tuple[str, bool]:
        is_private = event.is_private_chat()
        if is_private:
            if self._cfg("unify_cross_session", False):
                platform = event.get_platform_name()
                user_id = event.get_sender_id()
                return f"user::{platform}::{user_id}", False
            return event.unified_msg_origin, False

        if not self._cfg("enable_group_mode", False):
            return event.unified_msg_origin, True

        platform = event.get_platform_name()
        group_id = event.get_group_id()
        user_id = event.get_sender_id()

        if self._cfg("unify_cross_session", False):
            return f"user::{platform}::{user_id}", True

        return f"{platform}::{group_id}::{user_id}", True

    # ========== 消息监听 & 缓存 ==========

    @filter.on_llm_request()
    async def on_llm_request_cache(self, event: AstrMessageEvent, req: ProviderRequest):
        try:
            session_id, is_group = self._resolve_session_key(event)
            message_str = getattr(event, "message_str", "") or ""
            if getattr(req, "system_prompt", ""):
                self._last_persona[session_id] = req.system_prompt
            self._last_activity[session_id] = time.time()

            if message_str and message_str.strip():
                sender_id = ""
                sender_name = ""
                is_at_bot = False

                if is_group and self._cfg("enable_group_mode", False):
                    sender_id = event.get_sender_id() or ""
                    sender_name = getattr(event, "get_sender_name", lambda: "")() or ""
                    is_at_bot = getattr(event, "is_at_bot", lambda: False)()

                    platform = event.get_platform_name()
                    group_id = event.get_group_id()
                    await self.db.touch_user_activity(platform, group_id, sender_id, sender_name)

                self.buffer.add_message(
                    session_id, "user", message_str,
                    sender_id=sender_id, sender_name=sender_name, is_at_bot=is_at_bot,
                )
                if is_group and self._cfg("enable_group_mode", False):
                    group_key = self._extract_group_key(session_id)
                    if group_key:
                        self.buffer.add_message(
                            group_key, "user", message_str,
                            sender_id=sender_id, sender_name=sender_name, is_at_bot=is_at_bot,
                        )
                await self.db.increment_msg_count(session_id)

                self._last_request_session[event.unified_msg_origin] = (session_id, is_group)
        except Exception as e:
            logger.debug("[RelationSense] 用户消息缓存失败: %s", e)

    @filter.on_llm_response()
    async def on_llm_response_cache(self, event: AstrMessageEvent, resp):
        try:
            stored = self._last_request_session.get(event.unified_msg_origin)
            if stored:
                session_id, is_group = stored
            else:
                session_id, is_group = self._resolve_session_key(event)
            completion = getattr(resp, "completion_text", "") or ""
            if completion.strip():
                self.buffer.add_message(session_id, "assistant", completion.strip())
                if is_group and self._cfg("enable_group_mode", False):
                    group_key = self._extract_group_key(session_id)
                    if group_key:
                        self.buffer.add_message(group_key, "assistant", completion.strip())
                await self.db.increment_msg_count(session_id)
        except Exception as e:
            logger.debug("[RelationSense] Bot 回复缓存失败: %s", e)

    # ========== 常规触发分析 ==========

    @filter.on_llm_response()
    async def on_llm_response_trigger(self, event: AstrMessageEvent, resp):
        try:
            stored = self._last_request_session.get(event.unified_msg_origin)
            if stored:
                session_id, is_group = stored
            else:
                session_id, is_group = self._resolve_session_key(event)
            should = await self.trigger.should_analyze(session_id)
            if should:
                logger.info(
                    "[RelationSense] 常规触发分析 session=%s",
                    session_id,
                )
                if is_group and self._cfg("enable_group_mode", False):
                    self._spawn_bg(self._do_group_analyze(session_id, trigger="scheduled"))
                else:
                    self._spawn_bg(self._do_analyze(session_id, trigger="scheduled"))
        except Exception as e:
            logger.debug("[RelationSense] 触发条件检查失败: %s", e)

    # ========== 核心分析流程 ==========

    async def _apply_analysis_and_save(
        self,
        session_id: str,
        state: dict,
        current_values: dict,
        result: dict,
        is_initial: bool,
        trigger: str,
        source: str,
    ) -> dict:
        new_values, has_changes = self.tracker.apply_analysis_result(
            current_values, result, is_initial=is_initial,
        )

        if not has_changes:
            logger.debug("[RelationSense] 分析结果无变化 session=%s", session_id)

        affection_delta = new_values["affection"] - current_values["affection"]
        self._last_affection_change[session_id] = affection_delta

        level = self.tracker.compute_level(
            new_values["affection"], new_values["trust"], new_values["depth"],
        )

        summary = result.get("summary", state.get("summary", ""))
        user_state = result.get("user_state", "")
        tone_hint = result.get("tone_hint", "")
        confidence = result.get("confidence", 0.0)

        await self.db.set_meta_value(f"user_state_{session_id}", user_state)
        await self.db.set_meta_value(f"tone_hint_{session_id}", tone_hint)

        await self.db.upsert_relation_state(
            session_id=session_id,
            persona_name=state.get("persona_name", ""),
            affection=new_values["affection"],
            trust=new_values["trust"],
            depth=new_values["depth"],
            dependence=new_values["dependence"],
            return_rate=new_values["return_rate"],
            relation_level=level,
            summary=summary,
        )

        old_vals_json = json.dumps(current_values, ensure_ascii=False)
        new_vals_json = json.dumps(new_values, ensure_ascii=False)

        await self.db.add_analysis_log(
            session_id=session_id,
            persona_name=state.get("persona_name", ""),
            raw_json=json.dumps(result, ensure_ascii=False),
            old_values=old_vals_json,
            new_values=new_vals_json,
            summary=summary,
            confidence=confidence,
            trigger=trigger,
            source=source,
        )

        await self.db.reset_msg_count(session_id)

        debug_mode = self._cfg("debug_mode", False)
        if debug_mode or has_changes:
            logger.info(
                "[RelationSense] 分析完成 会话=%s 触发=%s 来源=%s "
                "好感度=%.1f→%.1f 信任度=%.1f→%.1f 对话深度=%.1f→%.1f 等级=%s",
                session_id, trigger, source,
                current_values["affection"], new_values["affection"],
                current_values["trust"], new_values["trust"],
                current_values["depth"], new_values["depth"],
                level,
            )

        return {
            "ok": True,
            "level": level,
            "level_label": self.tracker.compute_label(level),
            "summary": summary,
            "user_state": user_state,
            "tone_hint": tone_hint,
            "has_changes": has_changes,
            "is_initial": is_initial,
            "changes": {
                dim: (current_values[dim], new_values[dim])
                for dim in ("affection", "trust", "depth", "dependence", "return_rate")
            },
        }

    async def _do_analyze(self, session_id: str, trigger: str = "scheduled") -> dict:
        lock = self._get_lock(session_id)
        if lock.locked():
            logger.debug("[RelationSense] 分析已在执行 session=%s，跳过", session_id)
            return {"ok": False, "error": "分析已在执行中，请稍后再试"}

        async with lock:
            try:
                messages = self.buffer.get_recent(session_id, 80)
                if not messages:
                    logger.debug("[RelationSense] 无缓存消息 session=%s，跳过分析", session_id)
                    return {"ok": False, "error": "暂无缓存消息，请先进行几轮对话后再分析"}

                dialogue_lines = []
                for msg in messages:
                    role_label = "用户" if msg["role"] == "user" else "你"
                    dialogue_lines.append(f"{role_label}: {msg['content']}")
                dialogue_text = "\n".join(dialogue_lines)

                state = await self.db.get_relation_state_safe(session_id)
                is_initial = state is None

                use_live = self._cfg("enable_live_perception", False)
                rs_content = await self._get_rs_content(session_id) if use_live else None

                if is_initial and use_live and not rs_content:
                    logger.info(
                        "[RelationSense] 初始化等待对话模型 <rs> 标签 session=%s，跳过本次分析",
                        session_id,
                    )
                    return {"ok": False, "error": "等待对话模型首次关系感知"}

                if is_initial:
                    state = {
                        "affection": 50.0, "trust": 30.0, "depth": 20.0,
                        "dependence": 10.0, "return_rate": 0.0,
                        "relation_level": "Lv0", "summary": "",
                    }
                    await self.db.upsert_relation_state(
                        session_id=session_id, affection=50.0, trust=30.0,
                        depth=20.0, dependence=10.0, return_rate=0.0,
                        relation_level="Lv0", summary="",
                    )

                current_values = {
                    "affection": state.get("affection", 50),
                    "trust": state.get("trust", 30),
                    "depth": state.get("depth", 20),
                    "dependence": state.get("dependence", 10),
                    "return_rate": state.get("return_rate", 0),
                }

                result = await self.analyzer.analyze(
                    session_id=session_id,
                    dialogue_text=dialogue_text,
                    current_values=current_values,
                    bot_name="Bot",
                    user_name="对方",
                    persona_prompt=self._last_persona.get(session_id, ""),
                    is_initial=is_initial,
                    rs_content=rs_content,
                )

                if not result:
                    logger.warning("[RelationSense] 分析失败 session=%s", session_id)
                    return {"ok": False, "error": "LLM 分析调用失败，请检查分析模型配置"}

                return await self._apply_analysis_and_save(
                    session_id=session_id,
                    state=state,
                    current_values=current_values,
                    result=result,
                    is_initial=is_initial,
                    trigger=trigger,
                    source="live_analysis",
                )

            except Exception as e:
                logger.error(
                    "[RelationSense] 分析异常 session=%s: %s",
                    session_id, e, exc_info=True,
                )
                return {"ok": False, "error": f"分析异常: {e}"}

    # ========== 群聊分析流程 ==========

    async def _do_group_analyze(self, session_key: str, trigger: str = "scheduled") -> dict:
        platform, group_id, user_id = self._parse_group_user_key(session_key)
        if platform and group_id and user_id and trigger != "manual":
            last_ts = self._group_user_last_analyzed.get(session_key, 0)
            if time.time() - last_ts < 1800:
                logger.debug("[RelationSense] 群聊用户 %s 30分钟内已分析(内存)，跳过", session_key)
                return {"ok": False, "error": "该用户近期已分析"}
            last_analyzed_at = await self.db.get_user_last_analyzed_at(platform, group_id, user_id)
            if time.time() - last_analyzed_at < 1800:
                self._group_user_last_analyzed[session_key] = time.time()
                logger.debug("[RelationSense] 群聊用户 %s 30分钟内已分析(DB)，跳过", session_key)
                return {"ok": False, "error": "该用户近期已分析"}

        lock = self._get_lock(session_key)
        if lock.locked():
            logger.debug("[RelationSense] 群聊分析已在执行 session=%s，跳过", session_key)
            return {"ok": False, "error": "分析已在执行中"}

        async with lock:
            try:
                group_key = self._extract_group_key(session_key)
                if not group_key:
                    return await self._do_analyze(session_key, trigger=trigger)

                dialogue_text = await self._get_group_dialogue(group_key)
                if not dialogue_text:
                    logger.debug("[RelationSense] 群聊无缓存消息 session=%s，跳过分析", session_key)
                    return {"ok": False, "error": "暂无缓存消息"}

                state = await self.db.get_relation_state_safe(session_key)
                is_initial = state is None

                use_live = self._cfg("enable_live_perception", False)
                rs_content = await self._get_rs_content(session_key) if use_live else None

                if is_initial and use_live and not rs_content:
                    logger.info(
                        "[RelationSense] 群聊初始化等待对话模型 <rs> 标签 session=%s，跳过本次分析",
                        session_key,
                    )
                    return {"ok": False, "error": "等待对话模型首次关系感知"}

                if is_initial:
                    state = {
                        "affection": 50.0, "trust": 30.0, "depth": 20.0,
                        "dependence": 10.0, "return_rate": 0.0,
                        "relation_level": "Lv0", "summary": "",
                    }
                    await self.db.upsert_relation_state(
                        session_id=session_key, affection=50.0, trust=30.0,
                        depth=20.0, dependence=10.0, return_rate=0.0,
                        relation_level="Lv0", summary="",
                    )

                current_values = {
                    "affection": state.get("affection", 50),
                    "trust": state.get("trust", 30),
                    "depth": state.get("depth", 20),
                    "dependence": state.get("dependence", 10),
                    "return_rate": state.get("return_rate", 0),
                }

                target_name = await self._extract_user_name(session_key)
                target_id = self._extract_user_id(session_key)

                result = await self.analyzer.analyze_group(
                    session_id=session_key,
                    dialogue_text=dialogue_text,
                    current_values=current_values,
                    target_name=target_name,
                    target_id=target_id,
                    persona_prompt=self._last_persona.get(session_key, ""),
                    rs_content=rs_content,
                )

                if not result:
                    logger.warning("[RelationSense] 群聊分析失败 session=%s", session_key)
                    return {"ok": False, "error": "LLM 分析调用失败"}

                outcome = await self._apply_analysis_and_save(
                    session_id=session_key,
                    state=state,
                    current_values=current_values,
                    result=result,
                    is_initial=is_initial,
                    trigger=trigger,
                    source="group_analysis",
                )

                platform, group_id, user_id = self._parse_group_user_key(session_key)
                if platform and group_id and user_id:
                    await self.db.mark_user_analyzed(platform, group_id, user_id)
                    self._group_user_last_analyzed[session_key] = time.time()

                return outcome

            except Exception as e:
                logger.error(
                    "[RelationSense] 群聊分析异常 session=%s: %s",
                    session_key, e, exc_info=True,
                )
                return {"ok": False, "error": f"分析异常: {e}"}

    async def _group_batch_analysis_loop(self):
        while True:
            try:
                interval = int(self._cfg("group_analysis_interval_minutes", 120))
                await asyncio.sleep(interval * 60)

                if not self._cfg("enable_group_mode", False):
                    continue

                active_days = int(self._cfg("group_active_days", 3))
                max_users = int(self._cfg("group_max_active_users", 20))

                group_keys = set()
                for session_key in list(self._last_activity.keys()):
                    gk = self._extract_group_key(session_key)
                    if gk:
                        group_keys.add(gk)

                for group_key in group_keys:
                    try:
                        if not group_key.startswith("grp::"):
                            continue
                        parts = group_key.split("::")
                        if len(parts) != 3:
                            continue
                        platform, group_id = parts[1], parts[2]

                        await self.db.clean_inactive_users(platform, group_id, active_days)

                        stale_users = await self.db.get_stale_active_users(
                            platform, group_id,
                            stale_hours=2.0, min_msgs=10, limit=5,
                        )

                        if not stale_users:
                            continue

                        dialogue_text = await self._get_group_dialogue(group_key)
                        if not dialogue_text:
                            continue

                        batch_result = await self.analyzer.analyze_group_batch(
                            dialogue_text=dialogue_text,
                            target_users=stale_users,
                            persona_prompt=self._last_persona.get(group_key, ""),
                        )

                        if not batch_result:
                            logger.warning(
                                "[RelationSense] 群聊批量分析失败 group=%s", group_key
                            )
                            continue

                        for user_result in batch_result:
                            if not isinstance(user_result, dict):
                                continue
                            user_id = str(user_result.get("user_id", ""))
                            if not user_id:
                                continue

                            if self._cfg("unify_cross_session", False):
                                session_key = f"user::{platform}::{user_id}"
                            else:
                                session_key = f"{platform}::{group_id}::{user_id}"

                            lock = self._get_lock(session_key)
                            if lock.locked():
                                logger.debug(
                                    "[RelationSense] 批量分析跳过已锁定用户 %s",
                                    session_key,
                                )
                                continue

                            try:
                                state = await self.db.get_relation_state_safe(session_key)
                                is_initial = state is None
                                if is_initial:
                                    state = {
                                        "affection": 50.0, "trust": 30.0, "depth": 20.0,
                                        "dependence": 10.0, "return_rate": 0.0,
                                        "relation_level": "Lv0", "summary": "",
                                    }

                                current_values = {
                                    "affection": state.get("affection", 50),
                                    "trust": state.get("trust", 30),
                                    "depth": state.get("depth", 20),
                                    "dependence": state.get("dependence", 10),
                                    "return_rate": state.get("return_rate", 0),
                                }

                                outcome = await self._apply_analysis_and_save(
                                    session_id=session_key,
                                    state=state,
                                    current_values=current_values,
                                    result=user_result,
                                    is_initial=is_initial,
                                    trigger="batch",
                                    source="group_batch_analysis",
                                )
                                await self.db.mark_user_analyzed(platform, group_id, user_id)
                                logger.info(
                                    "[RelationSense] 批量分析更新 用户=%s 群=%s 等级=%s",
                                    user_id, group_key, outcome.get("level", "?"),
                                )
                            except Exception as e:
                                logger.warning(
                                    "[RelationSense] 群聊批量分析异常 group=%s user=%s: %s",
                                    group_key, user_id, e,
                                )

                    except Exception as e:
                        logger.warning(
                            "[RelationSense] 群聊批量分析异常 group=%s: %s",
                            group_key, e,
                        )

            except asyncio.CancelledError:
                return
            except Exception as e:
                logger.warning("[RelationSense] 群聊批量分析循环异常: %s", e)

    # ========== 群聊辅助方法 ==========

    def _extract_group_key(self, session_key: str) -> str:
        if session_key.startswith("user::"):
            return ""
        if session_key.startswith("grp::"):
            return session_key
        parts = session_key.split("::")
        if len(parts) >= 3:
            return f"grp::{parts[0]}::{parts[1]}"
        return ""

    def _extract_user_id(self, session_key: str) -> str:
        if session_key.startswith("user::"):
            parts = session_key.split("::", 2)
            return parts[2] if len(parts) >= 3 else ""
        parts = session_key.split("::")
        return parts[2] if len(parts) >= 3 else ""

    async def _extract_user_name(self, session_key: str) -> str:
        platform, group_id, user_id = self._parse_group_user_key(session_key)
        if platform and group_id and user_id:
            name = await self.db.get_user_name(platform, group_id, user_id)
            if name:
                return name
        return user_id or session_key

    def _parse_group_user_key(self, session_key: str) -> tuple[str, str, str]:
        if session_key.startswith("user::"):
            parts = session_key.split("::", 2)
            if len(parts) >= 3:
                return parts[1], "", parts[2]
            return "", "", ""
        if session_key.startswith("grp::"):
            return "", "", ""
        parts = session_key.split("::")
        if len(parts) >= 3:
            return parts[0], parts[1], parts[2]
        return "", "", ""

    async def _get_group_dialogue(self, group_key: str, count: int = 80) -> str:
        return self.buffer.format_group_dialogue(group_key, count)

    async def _build_active_users_summary(self, group_key: str, exclude_user_id: str) -> str:
        if not group_key.startswith("grp::"):
            return ""
        parts = group_key.split("::")
        if len(parts) != 3:
            return ""
        platform, group_id = parts[1], parts[2]
        active_days = int(self._cfg("group_active_days", 3))
        active_users = await self.db.get_group_active_users(platform, group_id, active_days)

        summaries = []
        for user in active_users:
            uid = user.get("user_id", "")
            if uid == exclude_user_id:
                continue
            if len(summaries) >= 3:
                break
            name = user.get("user_name") or uid or "某人"
            user_session = f"{platform}::{group_id}::{uid}"
            if self._cfg("unify_cross_session", False):
                user_session = f"user::{platform}::{uid}"
            state = await self.db.get_relation_state_safe(user_session)
            if state and state.get("summary"):
                summaries.append(f"{name}：{state['summary']}")
            else:
                level_label = "初识"
                if state:
                    level = state.get("relation_level", "")
                    if level:
                        level_label = self.tracker.compute_label(level)
                summaries.append(f"{name}（{level_label}）")

        return "；".join(summaries)

    # ========== system_prompt 注入 ==========

    @filter.on_llm_request()
    async def inject_relation_context(self, event: AstrMessageEvent, req: ProviderRequest):
        if not self._cfg("enable_injection", True):
            return

        try:
            session_id, is_group = self._resolve_session_key(event)

            if is_group and self._cfg("enable_group_mode", False):
                await self._inject_group_context(event, req, session_id)
                return

            state = await self.db.get_relation_state_safe(session_id)
            use_live = self._cfg("enable_live_perception", False)

            if state is None:
                if use_live:
                    await self._inject_live_perception_only(req, session_id)
                return

            rs_content = await self._get_rs_content(session_id)
            use_dialogue_static = self._cfg("enable_dialogue_static_update", False)

            if use_live and (use_dialogue_static or rs_content):
                if rs_content:
                    state["user_state"] = rs_content.get("user_state", "") or state.get("summary", "")
                    state["tone_hint"] = rs_content.get("tone", "") or "保持自然语气回应"
                    state["_rs_atmosphere"] = rs_content.get("atmosphere", "")
                else:
                    state["user_state"] = ""
                    state["tone_hint"] = "保持自然语气回应"
            else:
                try:
                    user_state_meta = await self.db.get_meta_value(f"user_state_{session_id}", "")
                    tone_hint_meta = await self.db.get_meta_value(f"tone_hint_{session_id}", "")
                    state["user_state"] = user_state_meta or state.get("summary", "")
                    state["tone_hint"] = tone_hint_meta or "保持自然语气回应"
                except Exception:
                    state["user_state"] = state.get("summary", "")
                    state["tone_hint"] = "保持自然语气回应"

            if not state.get("user_state"):
                state["user_state"] = "对方正在和你聊天。"

            rs_driven = use_live and rs_content is not None and (use_dialogue_static or rs_content.get("atmosphere"))
            injection = self.injector.build_injection(state, scenario=self._determine_scenario(session_id, state), rs_driven=rs_driven)
            if not injection:
                return

            if getattr(req, "system_prompt", None) is None:
                req.system_prompt = ""

            req.system_prompt, removed = _remove_rs_injection(req.system_prompt)
            if removed:
                logger.debug("[RelationSense] 已清理上次注入: session=%s", session_id)

            req.system_prompt += "\n" + RS_INJECTION_HEADER + "\n" + injection + "\n" + RS_INJECTION_FOOTER

            if use_live:
                await self._inject_live_perception_into_contexts(req, session_id)

            debug_mode = self._cfg("debug_mode", False)
            if debug_mode:
                logger.info(
                    "[RelationSense] 已注入关系上下文 会话=%s 好感度=%.1f 信任度=%.1f 实时感知=%s\n===== 注入内容 =====\n%s\n==================",
                    session_id, state.get("affection", 0), state.get("trust", 0), use_live, injection.strip(),
                )

        except Exception as e:
            logger.debug("[RelationSense] 注入失败: %s", e)

    async def _inject_live_perception_only(self, req: ProviderRequest, session_id: str):
        use_live = self._cfg("enable_live_perception", False)
        if not use_live:
            return
        await self._inject_live_perception_into_contexts(req, session_id)
        logger.debug("[RelationSense] 无 state，仅注入实时感知提示 session=%s", session_id)

    async def _inject_live_perception_into_contexts(self, req: ProviderRequest, session_id: str):
        if self._cfg("enable_live_perception_update", False):
            perception_text = LIVE_PERCEPTION_UPDATE_PROMPT
        else:
            perception_text = LIVE_PERCEPTION_PROMPT

        live_state = self._live_user_state.get(session_id, "")
        if live_state:
            perception_text += f"\n\n你最近一次感知到对方的状态是：{live_state}\n如果这与当前对话不符，请更新你的感知。"

        rs_content = await self._get_rs_content(session_id)
        if rs_content:
            perception_text += f"\n\n你上一次的关系感知：氛围={rs_content.get('atmosphere', '')} 语气={rs_content.get('tone', '')} 对方状态={rs_content.get('user_state', '')}\n如果关系状态有变化，请在本次 <rs> 标签中更新。"

        contexts = getattr(req, "contexts", None)
        if isinstance(contexts, list):
            contexts.append({
                "role": "user",
                "content": perception_text,
                "_no_save": True,
            })
        else:
            logger.debug("[RelationSense] req.contexts 不可用，跳过实时感知注入（需 AstrBot v4.24.2+）")

    async def _get_rs_content(self, session_id: str) -> Optional[dict]:
        try:
            rs_raw = await self.db.get_meta_value(f"rs_content_{session_id}", "")
            if rs_raw:
                data = json.loads(rs_raw)
                if isinstance(data, dict) and (data.get("atmosphere") or data.get("tone") or data.get("user_state")):
                    return data
        except (json.JSONDecodeError, ValueError, Exception):
            pass
        return None

    async def _inject_group_context(self, event: AstrMessageEvent, req: ProviderRequest, session_key: str):
        platform, group_id, user_id = self._parse_group_user_key(session_key)
        use_live = self._cfg("enable_live_perception", False)

        if platform and group_id and user_id:
            last_ts = self._group_user_last_analyzed.get(session_key, 0)
            if time.time() - last_ts > 1800:
                last_analyzed_at = await self.db.get_user_last_analyzed_at(platform, group_id, user_id)
                if time.time() - last_analyzed_at > 1800:
                    self._spawn_bg(self._do_group_analyze(session_key, trigger="reply"))

        state = await self.db.get_relation_state_safe(session_key)
        if state is None:
            if use_live:
                await self._inject_live_perception_only(req, session_key)
            return

        rs_content = await self._get_rs_content(session_key)
        use_dialogue_static = self._cfg("enable_dialogue_static_update", False)

        if use_live and (use_dialogue_static or rs_content):
            if rs_content:
                state["user_state"] = rs_content.get("user_state", "") or state.get("summary", "")
                state["tone_hint"] = rs_content.get("tone", "") or "保持自然语气回应"
                state["_rs_atmosphere"] = rs_content.get("atmosphere", "")
            else:
                state["user_state"] = ""
                state["tone_hint"] = "保持自然语气回应"
        else:
            try:
                user_state_meta = await self.db.get_meta_value(f"user_state_{session_key}", "")
                tone_hint_meta = await self.db.get_meta_value(f"tone_hint_{session_key}", "")
                state["user_state"] = user_state_meta or state.get("summary", "")
                state["tone_hint"] = tone_hint_meta or "保持自然语气回应"
            except Exception:
                state["user_state"] = state.get("summary", "")
                state["tone_hint"] = "保持自然语气回应"

        if not state.get("user_state"):
            state["user_state"] = ""

        sender_name = getattr(event, "get_sender_name", lambda: "")() or "用户"
        sender_id = event.get_sender_id() or ""

        group_key = self._extract_group_key(session_key)
        active_users_summary = ""
        if group_key:
            active_users_summary = await self._build_active_users_summary(group_key, sender_id)

        scenario = self._determine_scenario(session_key, state)
        rs_driven_group = use_live and rs_content is not None and (use_dialogue_static or rs_content.get("atmosphere"))

        injection = self.injector.build_group_injection(
            state,
            sender_name=sender_name,
            active_users_summary=active_users_summary,
            scenario=scenario,
            rs_driven=rs_driven_group,
        )

        if not injection:
            return

        if getattr(req, "system_prompt", None) is None:
            req.system_prompt = ""

        req.system_prompt, removed = _remove_rs_injection(req.system_prompt)
        if removed:
            logger.debug("[RelationSense] 已清理上次群聊注入: session=%s", session_key)

        req.system_prompt += "\n" + RS_INJECTION_HEADER + "\n" + injection + "\n" + RS_INJECTION_FOOTER

        if use_live:
            await self._inject_live_perception_into_contexts(req, session_key)

        debug_mode = self._cfg("debug_mode", False)
        if debug_mode:
            logger.info(
                "[RelationSense] 已注入群聊关系上下文 会话=%s 说话者=%s 好感度=%.1f\n===== 注入内容 =====\n%s\n==================",
                session_key, sender_name, state.get("affection", 0), injection.strip(),
            )

    @filter.on_llm_response()
    async def on_llm_response_parse_update(self, event: AstrMessageEvent, resp):
        use_live = self._cfg("enable_live_perception", False)
        if not use_live:
            return

        try:
            stored = self._last_request_session.get(event.unified_msg_origin)
            if stored:
                session_id = stored[0]
            else:
                session_id, _ = self._resolve_session_key(event)
            completion = getattr(resp, "completion_text", "") or ""
            if not completion:
                return

            rs_match = re.search(r"<rs>(.*?)</rs>", completion, re.DOTALL)
            if rs_match:
                rs_raw = rs_match.group(1).strip()
                rs_data = self._parse_rs_tag(rs_raw)
                if rs_data:
                    await self.db.set_meta_value(f"rs_content_{session_id}", rs_data)
                    self._live_user_state[session_id] = rs_data.get("user_state", "")
                    logger.info(
                        "[RelationSense] 对话模型输出 <rs> 标签 session=%s atmosphere=%s tone=%s user_state=%s",
                        session_id, rs_data.get("atmosphere", ""), rs_data.get("tone", ""), rs_data.get("user_state", ""),
                    )

                cleaned = re.sub(r"<rs>.*?</rs>", "", completion, flags=re.DOTALL).strip()
                try:
                    resp.completion_text = cleaned
                except (AttributeError, TypeError):
                    logger.debug("[RelationSense] 无法修改 resp.completion_text，<rs> 标签可能泄露到回复中")

            if self._cfg("enable_live_perception_update", False) and not rs_match:
                update_match = re.search(r"<update>(.*?)</update>", completion, re.DOTALL)
                if update_match:
                    new_state = update_match.group(1).strip()
                    if new_state:
                        self._live_user_state[session_id] = new_state
                        logger.info("[RelationSense] LLM 自主修正 user_state: session=%s state=%s", session_id, new_state)

                    cleaned = re.sub(r"<update>.*?</update>", "", completion, flags=re.DOTALL).strip()
                    try:
                        resp.completion_text = cleaned
                    except (AttributeError, TypeError):
                        logger.debug("[RelationSense] 无法修改 resp.completion_text，<update> 标签可能泄露到回复中")
        except Exception as e:
            logger.debug("[RelationSense] 标签解析失败: %s", e)

    def _parse_rs_tag(self, raw: str) -> Optional[dict]:
        try:
            data = json.loads(raw)
            if isinstance(data, dict):
                return {
                    "atmosphere": str(data.get("atmosphere", "")).strip(),
                    "tone": str(data.get("tone", "")).strip(),
                    "user_state": str(data.get("user_state", "")).strip(),
                }
        except (json.JSONDecodeError, ValueError):
            pass

        atmosphere = ""
        tone = ""
        user_state = ""

        atm_match = re.search(r'"atmosphere"\s*:\s*"([^"]*)"', raw)
        if atm_match:
            atmosphere = atm_match.group(1).strip()
        tone_match = re.search(r'"tone"\s*:\s*"([^"]*)"', raw)
        if tone_match:
            tone = tone_match.group(1).strip()
        us_match = re.search(r'"user_state"\s*:\s*"([^"]*)"', raw)
        if us_match:
            user_state = us_match.group(1).strip()

        if atmosphere or tone or user_state:
            return {"atmosphere": atmosphere, "tone": tone, "user_state": user_state}
        return None

    # ========== 管理命令 ==========

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("关系状态", alias={"relation_status", "查看关系"})
    async def cmd_relation_status(self, event: AstrMessageEvent):
        session_id, _ = self._resolve_session_key(event)
        result = await self.admin.get_status(session_id)
        yield event.plain_result(result)

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("关系历史", alias={"relation_history", "关系记录"})
    async def cmd_relation_history(self, event: AstrMessageEvent, limit: str = "5"):
        try:
            n = int(limit)
        except (ValueError, TypeError):
            n = 5
        session_id, _ = self._resolve_session_key(event)
        result = await self.admin.get_history(session_id, n)
        yield event.plain_result(result)

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("关系重置", alias={"relation_reset", "重置关系"})
    async def cmd_relation_reset(self, event: AstrMessageEvent):
        session_id, _ = self._resolve_session_key(event)
        result = await self.admin.reset(session_id)
        yield event.plain_result(result)

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("关系分析", alias={"relation_analyze", "分析关系"})
    async def cmd_relation_analyze(self, event: AstrMessageEvent):
        session_id, is_group = self._resolve_session_key(event)
        yield event.plain_result("正在分析当前会话的关系状态，请稍候…")
        if is_group and self._cfg("enable_group_mode", False):
            outcome = await self._do_group_analyze(session_id, trigger="manual")
        else:
            outcome = await self._do_analyze(session_id, trigger="manual")
        if not outcome["ok"]:
            yield event.plain_result(f"关系分析失败：{outcome['error']}")
            return
        lines = [
            f"📊 关系分析完成",
            f"等级：{outcome['level']}「{outcome['level_label']}」",
        ]
        if outcome["is_initial"]:
            lines.append("（首次印象评估）")
        if outcome["summary"]:
            lines.append(f"总结：{outcome['summary']}")
        if outcome["user_state"]:
            lines.append(f"对方状态：{outcome['user_state']}")
        changes = outcome["changes"]
        dim_labels = {"affection": "好感度", "trust": "信任度", "depth": "对话深度", "dependence": "依赖度", "return_rate": "回归率"}
        for dim, label in dim_labels.items():
            before, after = changes[dim]
            arrow = "↑" if after > before else ("↓" if after < before else "→")
            lines.append(f"{label}：{before:.0f} {arrow} {after:.0f}")
        yield event.plain_result("\n".join(lines))

    # ========== 场景判定 ==========

    def _determine_scenario(self, session_id: str, state: dict) -> str:
        """根据当前状态判定注入场景策略。"""
        affection = state.get("affection", 0)
        trust = state.get("trust", 0)

        if affection >= 90 and trust >= 88:
            return "minimal"

        last_change = self._last_affection_change.get(session_id, 0)
        if last_change <= -5:
            self._last_affection_change[session_id] = 0
            return "conflict"

        if 60 <= affection <= 75 and trust < 70:
            return "ambiguous"

        now = time.time()
        last_active = self._last_activity.get(session_id, now)
        if now - last_active > COOLING_INACTIVITY_HOURS * 3600 * 0.5:
            depth = state.get("depth", 20)
            dependence = state.get("dependence", 10)
            if depth < 40 and dependence < 40:
                return "silence"

        if session_id in self._scenario_flags:
            flag = self._scenario_flags.pop(session_id)
            return flag

        return "normal"

    # ========== 冷却循环 ==========

    async def _cooling_loop(self):
        """定期检查长时间不活跃的会话，施加自然衰减。"""
        while True:
            try:
                await asyncio.sleep(1800)
                now = time.time()
                threshold = COOLING_INACTIVITY_HOURS * 3600
                for sid, last_active in list(self._last_activity.items()):
                    if now - last_active < threshold:
                        continue
                    last_cooled = self._last_cooled.get(sid, 0)
                    if now - last_cooled < threshold:
                        continue
                    state = await self.db.get_relation_state_safe(sid)
                    if state is None:
                        continue
                    depth = float(state.get("depth", 20))
                    dependence = float(state.get("dependence", 10))
                    if depth <= 0 or dependence <= 0:
                        continue
                    new_depth = max(0.0, depth - COOLING_DEPTH_DECAY)
                    new_dependence = max(0.0, dependence - COOLING_DEPENDENCE_DECAY)
                    level = self.tracker.compute_level(
                        state.get("affection", 50), state.get("trust", 30), new_depth,
                    )
                    await self.db.upsert_relation_state(
                        session_id=sid,
                        persona_name=state.get("persona_name", ""),
                        affection=state.get("affection", 50),
                        trust=state.get("trust", 30),
                        depth=new_depth,
                        dependence=new_dependence,
                        return_rate=state.get("return_rate", 0),
                        relation_level=level,
                        summary=state.get("summary", ""),
                    )
                    self._last_cooled[sid] = now
                    logger.info(
                        "[RelationSense] 冷却衰减 会话=%s 对话深度=%.1f→%.1f 依赖度=%.1f→%.1f",
                        sid, depth, new_depth, dependence, new_dependence,
                    )
            except asyncio.CancelledError:
                return
            except Exception as e:
                logger.warning("[RelationSense] 冷却检查异常: %s", e)

    # ========== 定期清理 ==========

    _MEMORY_CLEANUP_THRESHOLD = 3600 * 24 * 7
    _LOCK_CLEANUP_THRESHOLD = 3600 * 24 * 2

    async def _cleanup_loop(self):
        while True:
            try:
                now = time.time()
                stale_cutoff = now - self._MEMORY_CLEANUP_THRESHOLD
                stale_sessions = [
                    sid for sid, ts in self._last_activity.items()
                    if ts < stale_cutoff
                ]
                for sid in stale_sessions:
                    self._analysis_locks.pop(sid, None)
                    self._lock_last_used.pop(sid, None)
                    self._last_persona.pop(sid, None)
                    self._last_affection_change.pop(sid, None)
                    self._last_activity.pop(sid, None)
                    self._scenario_flags.pop(sid, None)
                    self._live_user_state.pop(sid, None)
                    self._group_user_last_analyzed.pop(sid, None)
                    self._last_cooled.pop(sid, None)
                    self.buffer.clear(sid)
                if stale_sessions:
                    for sid in stale_sessions:
                        for prefix in ("rs_content_", "user_state_", "tone_hint_", "msg_count_"):
                            try:
                                await self.db.delete_meta_value(f"{prefix}{sid}")
                            except Exception:
                                pass
                    logger.info(
                        "[RelationSense] 清理了 %d 个过期会话的内存缓存",
                        len(stale_sessions),
                    )

                stale_origins = [
                    origin for origin, (sid, _) in self._last_request_session.items()
                    if sid in stale_sessions or sid not in self._last_activity
                ]
                for origin in stale_origins:
                    self._last_request_session.pop(origin, None)

                lock_cutoff = now - self._LOCK_CLEANUP_THRESHOLD
                stale_locks = [
                    sid for sid, ts in self._lock_last_used.items()
                    if ts < lock_cutoff
                ]
                for sid in stale_locks:
                    self._analysis_locks.pop(sid, None)
                    self._lock_last_used.pop(sid, None)
                if stale_locks:
                    logger.info(
                        "[RelationSense] 清理了 %d 个过期分析锁",
                        len(stale_locks),
                    )

                try:
                    deleted = await self.db.clean_old_analysis_logs(90)
                    if deleted:
                        logger.info(
                            "[RelationSense] 清理了 %d 条过期分析日志",
                            deleted,
                        )
                except Exception as e:
                    logger.debug("[RelationSense] 分析日志清理失败: %s", e)
            except asyncio.CancelledError:
                return
            except Exception as e:
                logger.warning("[RelationSense] 清理异常: %s", e)

            await asyncio.sleep(86400)
