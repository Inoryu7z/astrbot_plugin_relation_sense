import time
from collections import deque
from typing import Dict


class MessageBuffer:
    def __init__(self, max_size: int = 100):
        self.max_size = max_size
        self._buffers: Dict[str, deque] = {}

    def add_message(self, session_id: str, role: str, content: str,
                    sender_id: str = "", sender_name: str = "", is_at_bot: bool = False):
        if not content or not content.strip():
            return

        if session_id not in self._buffers:
            self._buffers[session_id] = deque(maxlen=self.max_size)

        self._buffers[session_id].append({
            "role": role,
            "content": content.strip(),
            "ts": time.time(),
            "sender_id": sender_id,
            "sender_name": sender_name,
            "is_at_bot": is_at_bot,
        })

    def get_recent(self, session_id: str, count: int = 50) -> list[dict]:
        """获取最近 N 条消息。

        Args:
            session_id: 会话标识
            count: 获取条数

        Returns:
            消息列表，每条为 {"role": "...", "content": "...", "ts": ...}
        """
        buf = self._buffers.get(session_id)
        if not buf:
            return []
        items = list(buf)
        return items[-count:] if len(items) > count else items

    def get_count_since(self, session_id: str, since_ts: float) -> int:
        """获取自某个时间戳以来的消息数量。

        Args:
            session_id: 会话标识
            since_ts: 起始时间戳

        Returns:
            消息条数
        """
        buf = self._buffers.get(session_id)
        if not buf:
            return 0
        count = 0
        for msg in reversed(buf):
            if msg["ts"] >= since_ts:
                count += 1
        return count

    def clear(self, session_id: str):
        """清除指定会话的缓存。"""
        self._buffers.pop(session_id, None)

    def get_total_count(self, session_id: str) -> int:
        buf = self._buffers.get(session_id)
        return len(buf) if buf else 0

    def format_group_dialogue(self, session_id: str, count: int = 80, bot_name: str = "你") -> str:
        messages = self.get_recent(session_id, count)
        lines = []
        for msg in messages:
            if msg["role"] == "assistant":
                lines.append(f"{bot_name}: {msg['content']}")
            else:
                name = msg.get("sender_name") or msg.get("sender_id") or "用户"
                lines.append(f"{name}: {msg['content']}")
        return "\n".join(lines)
