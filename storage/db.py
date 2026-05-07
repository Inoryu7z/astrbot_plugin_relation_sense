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
                "SELECT id FROM relation_state WHERE session_id = ?",
                (session_id,),
            )
            existing = cursor.fetchone()

            if existing:
                cursor.execute(
                    """UPDATE relation_state
                       SET persona_name=?, affection=?, trust=?, depth=?,
                           dependence=?, return_rate=?, relation_level=?,
                           summary=?, updated_at=?
                       WHERE session_id=?""",
                    (
                        persona_name, affection, trust, depth,
                        dependence, return_rate, relation_level,
                        summary, now_str, session_id,
                    ),
                )
            else:
                cursor.execute(
                    """INSERT INTO relation_state
                       (session_id, persona_name, affection, trust, depth,
                        dependence, return_rate, relation_level, summary,
                        created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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
        current = self._sync_get_meta_value(meta_key, 0)
        self._sync_set_meta_value(meta_key, current + 1)

    async def increment_msg_count(self, session_id: str):
        return await self._execute(self._sync_increment_msg_count, session_id)

    # ========== 数据清理 ==========

    def _sync_clean_expired(self, days_limit: int):
        with self._connect() as conn:
            cursor = conn.cursor()
            cutoff_date = (datetime.now() - timedelta(days=days_limit)).strftime("%Y-%m-%d %H:%M:%S")
            cursor.execute("DELETE FROM analysis_log WHERE created_at < ?", (cutoff_date,))
            deleted = cursor.rowcount
            conn.commit()
            return deleted

    async def clean_expired(self, days_limit: int):
        return await self._execute(self._sync_clean_expired, days_limit)
