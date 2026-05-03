import asyncio
import json
import time
from pathlib import Path

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register
from astrbot.core.provider.entities import ProviderRequest
from astrbot.core.utils.astrbot_path import get_astrbot_data_path

from .config import load_config
from .core.buffer import MessageBuffer
from .core.analyzer import RelationAnalyzer
from .core.trigger import AnalysisTrigger
from .core.tracker import DimensionTracker
from .core.injector import RelationInjector
from .core.initializer import RelationInitializer
from .storage.db import RelationDatabase
from .commands.admin import RelationAdminCommands
from .statics.defaults import COOLING_DEPENDENCE_DECAY, COOLING_DEPTH_DECAY, COOLING_INACTIVITY_HOURS


@register(
    "astrbot_plugin_relation_sense",
    "Inoryu7z",
    "关系感知插件，感知与用户的关系亲密度、对方画像与对话氛围",
    "1.2.1",
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
        self.initializer = RelationInitializer(context, self.db, plugin=self)
        self.admin = RelationAdminCommands(plugin=self)
        self.data_dir = data_dir

        self._initialized_sessions: set[str] = set()
        self._bg_tasks: set[asyncio.Task] = set()
        self._analysis_locks: dict[str, asyncio.Lock] = {}
        self._last_persona: str = ""
        self._last_affection_change: dict[str, float] = {}
        self._just_returned: set[str] = set()
        self._last_activity: dict[str, float] = {}
        self._scenario_flags: dict[str, str] = {}

        self.context._relation_sense_plugin = self

        logger.info("[RelationSense] 插件初始化完成")

    async def initialize(self):
        logger.info("[RelationSense] 插件启动，数据库就绪")
        self._spawn_bg(self._cleanup_loop())
        self._spawn_bg(self._cooling_loop())

    async def terminate(self):
        logger.info("[RelationSense] 插件已卸载")
        for task in self._bg_tasks:
            task.cancel()

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
        return self._analysis_locks[session_id]

    # ========== 消息监听 & 缓存 ==========

    @filter.on_llm_request()
    async def on_llm_request_cache(self, event: AstrMessageEvent, req: ProviderRequest):
        if not self._cfg("enable_plugin", True):
            return
        try:
            session_id = event.unified_msg_origin
            message_str = getattr(event, "message_str", "") or ""
            if getattr(req, "system_prompt", ""):
                self._last_persona = req.system_prompt
            self._last_activity[session_id] = time.time()

            if session_id not in self._initialized_sessions:
                self._initialized_sessions.add(session_id)
                existing_state = await self.db.get_relation_state_safe(session_id)
                if existing_state is None:
                    platform_id = event.get_platform_id()
                    user_id = event.get_sender_id()
                    logger.info(
                        "[RelationSense] 新会话触发回溯初始化 session=%s platform=%s user=%s",
                        session_id, platform_id, user_id,
                    )
                    self._spawn_bg(self.initializer.initialize_session(
                        session_id=session_id,
                        platform_id=platform_id,
                        user_id=user_id,
                        persona_prompt=self._last_persona,
                    ))

            if message_str and message_str.strip():
                self.buffer.add_message(session_id, "user", message_str)
                await self.db.increment_msg_count(session_id)

                event_type_keyword = self.trigger.detect_event_trigger(message_str)
                if event_type_keyword:
                    event_type, keyword = event_type_keyword
                    logger.info(
                        "[RelationSense] 检测到关键事件，触发分析 会话=%s 类型=%s 关键词=%s",
                        session_id, event_type, keyword,
                    )
                    if event_type == "return":
                        self._just_returned.add(session_id)
                    self._spawn_bg(self._do_analyze(session_id, trigger="event"))
        except Exception as e:
            logger.debug("[RelationSense] 用户消息缓存失败: %s", e)

    @filter.on_llm_response()
    async def on_llm_response_cache(self, event: AstrMessageEvent, resp):
        if not self._cfg("enable_plugin", True):
            return
        try:
            session_id = event.unified_msg_origin
            completion = getattr(resp, "completion_text", "") or ""
            if completion.strip():
                self.buffer.add_message(session_id, "assistant", completion.strip())
                await self.db.increment_msg_count(session_id)
        except Exception as e:
            logger.debug("[RelationSense] Bot 回复缓存失败: %s", e)

    # ========== 常规触发分析 ==========

    @filter.on_llm_response()
    async def on_llm_response_trigger(self, event: AstrMessageEvent, resp):
        if not self._cfg("enable_plugin", True):
            return
        try:
            session_id = event.unified_msg_origin
            should = await self.trigger.should_analyze(session_id)
            if should:
                logger.info(
                    "[RelationSense] 常规触发分析 session=%s",
                    session_id,
                )
                self._spawn_bg(self._do_analyze(session_id, trigger="scheduled"))
        except Exception as e:
            logger.debug("[RelationSense] 触发条件检查失败: %s", e)

    # ========== 核心分析流程 ==========

    async def _do_analyze(self, session_id: str, trigger: str = "scheduled"):
        """执行一次异步分析。"""
        lock = self._get_lock(session_id)
        if lock.locked():
            logger.debug("[RelationSense] 分析已在执行 session=%s，跳过", session_id)
            return

        async with lock:
            try:
                if not self._cfg("enable_plugin", True):
                    return

                # 获取对话文本
                messages = self.buffer.get_recent(session_id, 80)
                if not messages:
                    logger.debug("[RelationSense] 无缓存消息 session=%s，跳过分析", session_id)
                    return

                dialogue_lines = []
                for msg in messages:
                    role_label = "用户" if msg["role"] == "user" else "你"
                    dialogue_lines.append(f"{role_label}: {msg['content']}")
                dialogue_text = "\n".join(dialogue_lines)

                # 获取当前状态
                state = await self.db.get_relation_state_safe(session_id)
                if state is None:
                    if session_id not in self._initialized_sessions:
                        logger.info(
                            "[RelationSense] 会话 %s 尚未初始化，跳过本次分析，将触发回溯初始化",
                            session_id,
                        )
                        self._initialized_sessions.add(session_id)
                        self._spawn_bg(self.initializer.initialize_session(
                            session_id=session_id,
                            platform_id="",
                            user_id="",
                            persona_prompt=self._last_persona,
                        ))
                        return
                    logger.info(
                        "[RelationSense] 会话 %s 回溯初始化未完成，跳过本次分析",
                        session_id,
                    )
                    return

                current_values = {
                    "affection": state.get("affection", 50),
                    "trust": state.get("trust", 30),
                    "depth": state.get("depth", 20),
                    "dependence": state.get("dependence", 10),
                    "return_rate": state.get("return_rate", 0),
                }

                old_vals_json = json.dumps(current_values, ensure_ascii=False)

                # 调用 LLM 分析
                result = await self.analyzer.analyze(
                    session_id=session_id,
                    dialogue_text=dialogue_text,
                    current_values=current_values,
                    bot_name="Bot",
                    user_name="对方",
                    persona_prompt=self._last_persona,
                )

                if not result:
                    logger.warning("[RelationSense] 分析失败 session=%s", session_id)
                    return

                # 应用分析结果
                new_values, has_changes = self.tracker.apply_analysis_result(
                    current_values, result,
                )

                if not has_changes:
                    logger.debug("[RelationSense] 分析结果无变化 session=%s", session_id)

                # 记录好感度变化（用于后续冲突检测）
                affection_delta = new_values["affection"] - current_values["affection"]
                self._last_affection_change[session_id] = affection_delta

                # 更新等级
                level = self.tracker.compute_level(
                    new_values["affection"],
                    new_values["trust"],
                    new_values["depth"],
                )

                # 保存摘要和 user_state/tone_hint
                summary = result.get("summary", state.get("summary", ""))
                user_state = result.get("user_state", "")
                tone_hint = result.get("tone_hint", "")
                confidence = result.get("confidence", 0.0)

                # 同时也保存 user_state 和 tone_hint 到 DB（扩展字段 → 使用 meta 表）
                await self.db.set_meta_value(
                    f"user_state_{session_id}", user_state
                )
                await self.db.set_meta_value(
                    f"tone_hint_{session_id}", tone_hint
                )

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
                    source="live_analysis",
                )

                # 重置消息计数
                await self.db.reset_msg_count(session_id)

                debug_mode = self._cfg("debug_mode", False)
                if debug_mode or has_changes:
                    logger.info(
                        "[RelationSense] 分析完成 会话=%s 触发=%s "
                        "好感度=%.1f→%.1f 信任度=%.1f→%.1f 对话深度=%.1f→%.1f 等级=%s",
                        session_id, trigger,
                        current_values["affection"], new_values["affection"],
                        current_values["trust"], new_values["trust"],
                        current_values["depth"], new_values["depth"],
                        level,
                    )

            except Exception as e:
                logger.error(
                    "[RelationSense] 分析异常 session=%s: %s",
                    session_id, e, exc_info=True,
                )

    # ========== system_prompt 注入 ==========

    @filter.on_llm_request()
    async def inject_relation_context(self, event: AstrMessageEvent, req: ProviderRequest):
        if not self._cfg("enable_plugin", True) or not self._cfg("enable_injection", True):
            return

        try:
            session_id = event.unified_msg_origin
            state = await self.db.get_relation_state_safe(session_id)
            if state is None:
                return

            # 获取额外存储的 user_state 和 tone_hint
            try:
                user_state_meta = await self.db.get_meta_value(f"user_state_{session_id}", "")
                tone_hint_meta = await self.db.get_meta_value(f"tone_hint_{session_id}", "")
                state["user_state"] = user_state_meta or state.get("summary", "")
                state["tone_hint"] = tone_hint_meta or "保持自然语气回应"
            except Exception:
                state["user_state"] = state.get("summary", "")
                state["tone_hint"] = "保持自然语气回应"

            injection = self.injector.build_injection(state, scenario=self._determine_scenario(session_id, state))
            if not injection:
                return

            if getattr(req, "system_prompt", None) is None:
                req.system_prompt = ""
            req.system_prompt += injection

            debug_mode = self._cfg("debug_mode", False)
            if debug_mode:
                logger.info(
                    "[RelationSense] 已注入关系上下文 会话=%s 好感度=%.1f 信任度=%.1f\n===== 注入内容 =====\n%s\n==================",
                    session_id, state.get("affection", 0), state.get("trust", 0), injection.strip(),
                )

        except Exception as e:
            logger.debug("[RelationSense] 注入失败: %s", e)

    # ========== 管理命令 ==========

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("关系状态", alias={"relation_status"})
    async def cmd_relation_status(self, event: AstrMessageEvent):
        session_id = event.unified_msg_origin
        result = await self.admin.get_status(session_id)
        yield event.plain_result(result)

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("关系历史", alias={"relation_history"})
    async def cmd_relation_history(self, event: AstrMessageEvent, limit: str = "5"):
        try:
            n = int(limit)
        except (ValueError, TypeError):
            n = 5
        session_id = event.unified_msg_origin
        result = await self.admin.get_history(session_id, n)
        yield event.plain_result(result)

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("解冻关系", alias={"relation_unfreeze"})
    async def cmd_relation_unfreeze(self, event: AstrMessageEvent):
        session_id = event.unified_msg_origin
        result = await self.admin.unfreeze_all(session_id)
        yield event.plain_result(result)

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("重置关系", alias={"relation_reset"})
    async def cmd_relation_reset(self, event: AstrMessageEvent):
        session_id = event.unified_msg_origin
        result = await self.admin.reset(session_id)
        yield event.plain_result(result)

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("关系分析", alias={"relation_analyze"})
    async def cmd_relation_analyze(self, event: AstrMessageEvent):
        session_id = event.unified_msg_origin
        self._spawn_bg(self._do_analyze(session_id, trigger="manual"))
        yield event.plain_result("已触发手动分析，结果将在后台生成，稍后可用「关系状态」查看。")

    # ========== 场景判定 ==========

    def _determine_scenario(self, session_id: str, state: dict) -> str:
        """根据当前状态判定注入场景策略。"""
        affection = state.get("affection", 0)
        trust = state.get("trust", 0)
        affection_threshold = float(self._cfg("affection_freeze_threshold", 90.0))
        trust_threshold = float(self._cfg("trust_freeze_threshold", 88.0))

        # 好感信任双满 → 极简
        if affection >= affection_threshold and trust >= trust_threshold:
            return "minimal"

        # 刚从回归事件中回来
        if session_id in self._just_returned:
            self._just_returned.discard(session_id)
            return "return"

        # 好感度明显下降（≥ 5）→ 冲突
        last_change = self._last_affection_change.get(session_id, 0)
        if last_change <= -5:
            self._last_affection_change[session_id] = 0
            return "conflict"

        # 好感 60-75 且信任 < 70 → 暧昧
        if 60 <= affection <= 75 and trust < 70:
            return "ambiguous"

        # 长时间无活跃 → 沉寂
        now = time.time()
        last_active = self._last_activity.get(session_id, now)
        if now - last_active > COOLING_INACTIVITY_HOURS * 3600 * 0.5:
            depth = state.get("depth", 20)
            dependence = state.get("dependence", 10)
            if depth < 40 and dependence < 40:
                return "silence"

        # 由确定的场景覆盖
        if session_id in self._scenario_flags:
            flag = self._scenario_flags.pop(session_id)
            return flag

        return "normal"

    # ========== 冷却循环 ==========

    async def _cooling_loop(self):
        """定期检查长时间不活跃的会话，施加自然衰减。"""
        while True:
            try:
                await asyncio.sleep(1800)  # 每 30 分钟检查一次
                now = time.time()
                threshold = COOLING_INACTIVITY_HOURS * 3600
                for sid, last_active in list(self._last_activity.items()):
                    if now - last_active < threshold:
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
                    logger.info(
                        "[RelationSense] 冷却衰减 会话=%s 对话深度=%.1f→%.1f 依赖度=%.1f→%.1f",
                        sid, depth, new_depth, dependence, new_dependence,
                    )
                    self._last_activity[sid] = now  # 移出冷却窗口，下次再衰减
            except asyncio.CancelledError:
                return
            except Exception as e:
                logger.warning("[RelationSense] 冷却检查异常: %s", e)

    # ========== 定期清理 ==========

    async def _cleanup_loop(self):
        while True:
            try:
                retention = int(self._cfg("history_retention_days", 60))
                deleted = await self.db.clean_expired(retention)
                if deleted > 0:
                    logger.info(
                        "[RelationSense] 清理了 %d 条过期分析记录（保留 %d 天）",
                        deleted, retention,
                    )
            except asyncio.CancelledError:
                return
            except Exception as e:
                logger.warning("[RelationSense] 清理异常: %s", e)

            # 每 24 小时清理一次
            await asyncio.sleep(86400)
