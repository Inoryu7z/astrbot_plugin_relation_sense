### v1.5.0

**🧹 功能精简：移除冻结/事件触发/记录清理**

* 移除好感度/信任度冻结线（`affection_freeze_threshold` / `trust_freeze_threshold`），五维数值不再受上限冻结
* 移除事件触发（`enable_event_trigger`），不再根据关键词即时触发分析
* 移除记录保留天数（`history_retention_days`），不再自动清理分析记录
* 移除「解冻关系」管理命令
* 移除「回归」注入场景（INJECTION_RETURN / GROUP_INJECTION_RETURN）
* 分析提示词移除冻结规则和 `frozen` 字段，输出格式简化
* 群聊模式 Bug 修复：Session Key 分隔符改为 `::`、群消息存入群组级 buffer、Bot 回复 key 修正、用户名提取改为 DB 查询、30 分钟频率限制、回复触发分析
* 管理命令适配群聊模式

### v1.4.0

**👥 群聊模式：1vN 关系感知**

* 新增 `enable_group_mode` 全局开关，开启后群聊场景自动切换到多用户关系感知逻辑，私聊行为完全不变
* 新增 `unify_cross_session` 可选开关，开启后同一用户在所有会话共享五维数据
* 新增 `group_active_days`、`group_analysis_interval_minutes`、`group_max_active_users` 群聊配置项
* 群聊中按 `platform::groupId::userId` 格式为每个用户维护独立的关系状态
* 新增群聊专用分析提示词（GROUP_ANALYZER_SYSTEM_PROMPT / GROUP_ANALYZER_USER_PROMPT），针对群聊场景调整五维评估规则
* 新增群聊批量分析提示词（GROUP_BATCH_ANALYZER），单次 LLM 调用分析多用户
* 新增群聊专用注入模板（GROUP_INJECTION_*），包含当前说话者上下文 + 活跃用户摘要
* 新增 `group_user_activity` 数据库表，追踪群内用户活跃度和分析时间
* 消息缓存扩展：`add_message` 新增 `sender_id`/`sender_name`/`is_at_bot` 参数
* 新增 `format_group_dialogue` 方法，将缓存消息格式化为带发送者名称的对话文本
* 异步后台批量补分析循环，定期检查过期活跃用户并批量更新

### v1.3.0

**🏗️ 三层架构重构：防注入累积 + 实时感知 + 自主修正**

**Layer 1 — 基础修复（默认开启，全版本兼容）**
* 每次注入前先删除旧的 RS_Injection 块，用 `<!-- RS_Injection -->` / `<!-- /RS_Injection -->` 包裹注入内容，解决 system_prompt 注入累积

**Layer 2 — 对话 LLM 实时感知（开关，默认关闭，需 v4.24.2+）**
* 新增 `enable_live_perception` 配置项
* 开启后不再从 DB 读取 `user_state` / `tone_hint`，改由对话 LLM 通过 `_no_save` 上下文消息实时感知对方状态
* 五维数值仍由异步分析 LLM 后台更新

**Layer 3 — LLM 自主修正注入（开关，默认关闭，需 v4.24.2+）**
