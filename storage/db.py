import asyncio
import json
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

from astrbot.api import logger


class RelationDatabase:
    def __init__(self, data_dir: Path):
        self.db_path = data_dir / "relation_sense.db"
        self._init_db()

    @contextmanager
    def _connect(self):
        conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        try:
            yield conn
        finally:
            conn.close()

    def _init_db(self):
        with self._connect() as conn:
            cursor = conn.cursor()

            cursor.execute("""
                CREATE TABLE IF NOT EXISTS relation_state (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    persona_name TEXT DEFAULT '',
                    affection REAL DEFAULT 50,
                    trust REAL DEFAULT 30,
                    depth REAL DEFAULT 20,
                    dependence REAL DEFAULT 10,
                    return_rate REAL DEFAULT 0,
                    relation_level TEXT DEFAULT '',
                    summary TEXT DEFAULT '',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(session_id)
                )
            """)

            cursor.execute("""
                CREATE TABLE IF NOT EXISTS analysis_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    persona_name TEXT DEFAULT '',
                    raw_json TEXT,
                    old_values TEXT,
                    new_values TEXT,
                    summary TEXT DEFAULT '',
                    confidence REAL DEFAULT 0.0,
                    trigger TEXT DEFAULT 'scheduled',
                    source TEXT DEFAULT '',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_analysis_session ON analysis_log(session_id)"
            )

            cursor.execute("""
                CREATE TABLE IF NOT EXISTS plugin_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            cursor.execute("""
                CREATE TABLE IF NOT EXISTS group_user_activity (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    platform TEXT NOT NULL,
                    group_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    user_name TEXT DEFAULT '',
                    last_active_at TIMESTAMP,
                    last_analyzed_at TIMESTAMP DEFAULT '1970-01-01 00:00:00',
                    msg_count_since_analysis INTEGER DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(platform, group_id, user_id)
                )
            """)
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_gua_group ON group_user_activity(platform, group_id)"
            )
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_gua_last_active ON group_user_activity(last_active_at)"
            )

            conn.commit()
            self._run_migrations(conn)

    def _run_migrations(self, conn: sqlite3.Connection):
        cursor = conn.cursor()
        try:
            cursor.execute("PRAGMA table_info(analysis_log)")
            columns = [row[1] for row in cursor.fetchall()]
            if "trigger" not in columns:
                cursor.execute("ALTER TABLE analysis_log ADD COLUMN trigger TEXT DEFAULT 'scheduled'")
                conn.commit()
                logger.info("[RelationSense] 已为 analysis_log 表添加 trigger 字段")
            if "source" not in columns:
                cursor.execute("ALTER TABLE analysis_log ADD COLUMN source TEXT DEFAULT ''")
                conn.commit()
                logger.info("[RelationSense] 已为 analysis_log 表添加 source 字段")
        except Exception as e:
            logger.warning("[RelationSense] 迁移失败: %s", e)

    async def _execute(self, func, *args, **kwargs):
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, lambda: func(*args, **kwargs))

    # ========== 关系状态 ==========

    def _sync_upsert_relation_state(
        self,
        session_id: str,
        persona_name: str = "",
        affection: float = 50.0,
        trust: float = 30.0,
        depth: float = 20.0,
        dependence: float = 10.0,
        return_rate: float = 0.0,
        relation_level: str = "",
        summary: str = "",
    ):
        with self._connect() as conn:
            cursor = conn.cursor()
            now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            cursor.execute(
                """INSERT INTO relation_state
                   (session_id, persona_name, affection, trust, depth,
                    dependence, return_rate, relation_level, summary,
                    created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(session_id) DO UPDATE SET
                       persona_name=excluded.persona_name,
                       affection=excluded.affection,
                       trust=excluded.trust,
                       depth=excluded.depth,
                       dependence=excluded.dependence,
                       return_rate=excluded.return_rate,
                       relation_level=excluded.relation_level,
                       summary=excluded.summary,
                       updated_at=excluded.updated_at""",
                (
                    session_id, persona_name, affection, trust, depth,
                    dependence, return_rate, relation_level, summary,
                    now_str, now_str,
                ),
            )
            conn.commit()

    async def upsert_relation_state(
        self,
        session_id: str,
        persona_name: str = "",
        affection: float = 50.0,
        trust: float = 30.0,
        depth: float = 20.0,
        dependence: float = 10.0,
        return_rate: float = 0.0,
        relation_level: str = "",
        summary: str = "",
    ):
        return await self._execute(
            self._sync_upsert_relation_state,
            session_id, persona_name, affection, trust, depth,
            dependence, return_rate, relation_level, summary,
        )

    def _sync_get_relation_state_columns(self, session_id: str) -> Optional[dict]:
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT * FROM relation_state WHERE session_id = ?",
                (session_id,),
            )
            row = cursor.fetchone()
            if row is None:
                return None
            col_names = [desc[0] for desc in cursor.description] if cursor.description else []
            return dict(zip(col_names, row)) if col_names else None

    async def get_relation_state_safe(self, session_id: str) -> Optional[dict]:
        return await self._execute(self._sync_get_relation_state_columns, session_id)

    def _sync_reset_relation_state(self, session_id: str):
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "DELETE FROM relation_state WHERE session_id = ?",
                (session_id,),
            )
            conn.commit()

    async def reset_relation_state(self, session_id: str):
        return await self._execute(self._sync_reset_relation_state, session_id)

    # ========== 分析日志 ==========

    def _sync_add_analysis_log(
        self,
        session_id: str,
        persona_name: str = "",
        raw_json: str = "",
        old_values: str = "",
        new_values: str = "",
        summary: str = "",
        confidence: float = 0.0,
        trigger: str = "scheduled",
        source: str = "",
    ):
        with self._connect() as conn:
            cursor = conn.cursor()
            now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            cursor.execute(
                """INSERT INTO analysis_log
                   (session_id, persona_name, raw_json, old_values, new_values,
                    summary, confidence, trigger, source, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    session_id, persona_name, raw_json, old_values, new_values,
                    summary, confidence, trigger, source, now_str,
                ),
            )
            conn.commit()

    async def add_analysis_log(
        self,
        session_id: str,
        persona_name: str = "",
        raw_json: str = "",
        old_values: str = "",
        new_values: str = "",
        summary: str = "",
        confidence: float = 0.0,
        trigger: str = "scheduled",
        source: str = "",
    ):
        return await self._execute(
            self._sync_add_analysis_log,
            session_id, persona_name, raw_json, old_values, new_values,
            summary, confidence, trigger, source,
        )

    def _sync_get_recent_analysis(self, session_id: str, limit: int = 5) -> list[dict]:
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """SELECT * FROM analysis_log
                   WHERE session_id = ?
                   ORDER BY id DESC LIMIT ?""",
                (session_id, limit),
            )
            rows = cursor.fetchall()
            if rows:
                col_names = [desc[0] for desc in cursor.description] if cursor.description else []
                return [dict(zip(col_names, r)) for r in rows] if col_names else []
            return []

    async def get_recent_analysis(self, session_id: str, limit: int = 5) -> list[dict]:
        return await self._execute(self._sync_get_recent_analysis, session_id, limit)

    def _sync_get_last_analysis_at(self, session_id: str) -> float:
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT created_at FROM analysis_log WHERE session_id = ? ORDER BY id DESC LIMIT 1",
                (session_id,),
            )
            row = cursor.fetchone()
            if row and row[0]:
                try:
                    dt = datetime.strptime(row[0], "%Y-%m-%d %H:%M:%S")
                    return dt.timestamp()
                except ValueError:
                    pass
            return 0.0

    async def get_last_analysis_at(self, session_id: str) -> float:
        return await self._execute(self._sync_get_last_analysis_at, session_id)

    # ========== 插件元数据 ==========

    def _sync_get_meta_value(self, key: str, default: Any = None) -> Any:
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT value FROM plugin_meta WHERE key = ?", (key,))
            row = cursor.fetchone()
            if row:
                try:
                    return json.loads(row[0])
                except (json.JSONDecodeError, TypeError):
                    return row[0]
            return default

    async def get_meta_value(self, key: str, default: Any = None) -> Any:
        return await self._execute(self._sync_get_meta_value, key, default)

    def _sync_set_meta_value(self, key: str, value: Any):
        with self._connect() as conn:
            cursor = conn.cursor()
            now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            json_val = json.dumps(value, ensure_ascii=False)
            cursor.execute(
                """INSERT INTO plugin_meta (key, value, updated_at)
                   VALUES (?, ?, ?)
                   ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at""",
                (key, json_val, now_str),
            )
            conn.commit()

    async def set_meta_value(self, key: str, value: Any):
        return await self._execute(self._sync_set_meta_value, key, value)

    def _sync_delete_meta_value(self, key: str):
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM plugin_meta WHERE key = ?", (key,))
            conn.commit()

    async def delete_meta_value(self, key: str):
        return await self._execute(self._sync_delete_meta_value, key)

    def _sync_delete_meta_by_prefix(self, prefix: str):
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM plugin_meta WHERE key LIKE ?", (f"{prefix}%",))
            deleted = cursor.rowcount
            conn.commit()
            return deleted

    async def delete_meta_by_prefix(self, prefix: str):
        return await self._execute(self._sync_delete_meta_by_prefix, prefix)

    def _sync_get_msg_count_since_last(self, session_id: str) -> int:
        meta_key = f"msg_count_{session_id}"
        return self._sync_get_meta_value(meta_key, 0)

    async def get_msg_count_since_last(self, session_id: str) -> int:
        return await self._execute(self._sync_get_msg_count_since_last, session_id)

    def _sync_reset_msg_count(self, session_id: str):
        meta_key = f"msg_count_{session_id}"
        self._sync_set_meta_value(meta_key, 0)

    async def reset_msg_count(self, session_id: str):
        return await self._execute(self._sync_reset_msg_count, session_id)

    def _sync_increment_msg_count(self, session_id: str):
        meta_key = f"msg_count_{session_id}"
        with self._connect() as conn:
            cursor = conn.cursor()
            now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            cursor.execute(
                """INSERT INTO plugin_meta (key, value, updated_at)
                   VALUES (?, '1', ?)
                   ON CONFLICT(key) DO UPDATE SET value = CAST(value AS INTEGER) + 1, updated_at = excluded.updated_at""",
                (meta_key, now_str),
            )
            conn.commit()

    async def increment_msg_count(self, session_id: str):
        return await self._execute(self._sync_increment_msg_count, session_id)

    # ========== 群聊活跃用户 ==========

    def _sync_touch_user_activity(
        self, platform: str, group_id: str, user_id: str, user_name: str
    ):
        with self._connect() as conn:
            cursor = conn.cursor()
            now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            cursor.execute(
                """INSERT INTO group_user_activity (platform, group_id, user_id, user_name, last_active_at, msg_count_since_analysis)
                   VALUES (?, ?, ?, ?, ?, 1)
                   ON CONFLICT(platform, group_id, user_id) DO UPDATE SET
                       user_name=excluded.user_name,
                       last_active_at=excluded.last_active_at,
                       msg_count_since_analysis=msg_count_since_analysis + 1""",
                (platform, group_id, user_id, user_name, now_str),
            )
            conn.commit()

    async def touch_user_activity(
        self, platform: str, group_id: str, user_id: str, user_name: str
    ):
        return await self._execute(
            self._sync_touch_user_activity, platform, group_id, user_id, user_name
        )

    def _sync_get_group_active_users(
        self, platform: str, group_id: str, active_days: int = 3
    ) -> list[dict]:
        with self._connect() as conn:
            cursor = conn.cursor()
            cutoff = (datetime.now() - timedelta(days=active_days)).strftime(
                "%Y-%m-%d %H:%M:%S"
            )
            cursor.execute(
                """SELECT platform, group_id, user_id, user_name, last_active_at,
                          last_analyzed_at, msg_count_since_analysis
                   FROM group_user_activity
                   WHERE platform=? AND group_id=? AND last_active_at >= ?
                   ORDER BY last_active_at DESC""",
                (platform, group_id, cutoff),
            )
            rows = cursor.fetchall()
            col_names = (
                [desc[0] for desc in cursor.description] if cursor.description else []
            )
            return [dict(zip(col_names, r)) for r in rows] if col_names else []

    async def get_group_active_users(
        self, platform: str, group_id: str, active_days: int = 3
    ) -> list[dict]:
        return await self._execute(
            self._sync_get_group_active_users, platform, group_id, active_days
        )

    def _sync_get_stale_active_users(
        self,
        platform: str,
        group_id: str,
        stale_hours: float = 2.0,
        min_msgs: int = 10,
        limit: int = 5,
    ) -> list[dict]:
        with self._connect() as conn:
            cursor = conn.cursor()
            cutoff = (datetime.now() - timedelta(hours=stale_hours)).strftime(
                "%Y-%m-%d %H:%M:%S"
            )
            cursor.execute(
                """SELECT platform, group_id, user_id, user_name, last_active_at,
                          last_analyzed_at, msg_count_since_analysis
                   FROM group_user_activity
                   WHERE platform=? AND group_id=?
                     AND last_analyzed_at < ?
                     AND msg_count_since_analysis >= ?
                   ORDER BY last_active_at DESC
                   LIMIT ?""",
                (platform, group_id, cutoff, min_msgs, limit),
            )
            rows = cursor.fetchall()
            col_names = (
                [desc[0] for desc in cursor.description] if cursor.description else []
            )
            return [dict(zip(col_names, r)) for r in rows] if col_names else []

    async def get_stale_active_users(
        self,
        platform: str,
        group_id: str,
        stale_hours: float = 2.0,
        min_msgs: int = 10,
        limit: int = 5,
    ) -> list[dict]:
        return await self._execute(
            self._sync_get_stale_active_users,
            platform, group_id, stale_hours, min_msgs, limit,
        )

    def _sync_mark_user_analyzed(self, platform: str, group_id: str, user_id: str):
        with self._connect() as conn:
            cursor = conn.cursor()
            now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            cursor.execute(
                """UPDATE group_user_activity
                   SET last_analyzed_at=?, msg_count_since_analysis=0
                   WHERE platform=? AND group_id=? AND user_id=?""",
                (now_str, platform, group_id, user_id),
            )
            conn.commit()

    async def mark_user_analyzed(
        self, platform: str, group_id: str, user_id: str
    ):
        return await self._execute(
            self._sync_mark_user_analyzed, platform, group_id, user_id
        )

    def _sync_clean_inactive_users(
        self, platform: str, group_id: str, active_days: int = 3
    ):
        with self._connect() as conn:
            cursor = conn.cursor()
            cutoff = (datetime.now() - timedelta(days=active_days)).strftime(
                "%Y-%m-%d %H:%M:%S"
            )
            cursor.execute(
                """DELETE FROM group_user_activity
                   WHERE platform=? AND group_id=? AND last_active_at < ?""",
                (platform, group_id, cutoff),
            )
            deleted = cursor.rowcount
            conn.commit()
            return deleted

    async def clean_inactive_users(
        self, platform: str, group_id: str, active_days: int = 3
    ):
        return await self._execute(
            self._sync_clean_inactive_users, platform, group_id, active_days
        )

    def _sync_get_user_last_analyzed_at(
        self, platform: str, group_id: str, user_id: str
    ) -> float:
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """SELECT last_analyzed_at FROM group_user_activity
                   WHERE platform=? AND group_id=? AND user_id=?""",
                (platform, group_id, user_id),
            )
            row = cursor.fetchone()
            if row and row[0]:
                try:
                    dt = datetime.strptime(row[0], "%Y-%m-%d %H:%M:%S")
                    return dt.timestamp()
                except ValueError:
                    pass
            return 0.0

    async def get_user_last_analyzed_at(
        self, platform: str, group_id: str, user_id: str
    ) -> float:
        return await self._execute(
            self._sync_get_user_last_analyzed_at, platform, group_id, user_id
        )

    def _sync_get_user_name(self, platform: str, group_id: str, user_id: str) -> str:
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT user_name FROM group_user_activity WHERE platform=? AND group_id=? AND user_id=?",
                (platform, group_id, user_id),
            )
            row = cursor.fetchone()
            return row[0] if row and row[0] else ""

    async def get_user_name(self, platform: str, group_id: str, user_id: str) -> str:
        return await self._execute(self._sync_get_user_name, platform, group_id, user_id)

    def _sync_clean_old_analysis_logs(self, retention_days: int = 90) -> int:
        with self._connect() as conn:
            cursor = conn.cursor()
            cutoff = (datetime.now() - timedelta(days=retention_days)).strftime(
                "%Y-%m-%d %H:%M:%S"
            )
            cursor.execute(
                "DELETE FROM analysis_log WHERE created_at < ?",
                (cutoff,),
            )
            deleted = cursor.rowcount
            conn.commit()
            return deleted

    async def clean_old_analysis_logs(self, retention_days: int = 90) -> int:
        return await self._execute(self._sync_clean_old_analysis_logs, retention_days)
