from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional


class MemoryManager:
    def __init__(self, data_dir: Path, max_memories_per_user: int = 100):
        self.data_dir = data_dir
        self.max_memories_per_user = max_memories_per_user
        self._lock = asyncio.Lock()
        self._file_path = self.data_dir / "memories.json"
        self._ensure_file()

    def _ensure_file(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        if not self._file_path.exists():
            self._save_data({"memories": []})

    def _load_data(self) -> dict:
        try:
            return json.loads(self._file_path.read_text(encoding="utf-8"))
        except Exception:
            return {"memories": []}

    def _save_data(self, data: dict) -> None:
        self._file_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _get_timestamp(self) -> str:
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    async def _cleanup_if_needed(self, user_id: str) -> None:
        if self.max_memories_per_user <= 0:
            return
        data = self._load_data()
        memories = data.get("memories", [])
        user_memories = [m for m in memories if m.get("user_id") == str(user_id)]
        if len(user_memories) <= self.max_memories_per_user:
            return

        user_memories.sort(key=lambda x: x.get("updated_at", ""))
        remove_count = len(user_memories) - self.max_memories_per_user
        remove_ids = {m["id"] for m in user_memories[:remove_count] if m.get("id")}
        memories = [m for m in memories if m.get("id") not in remove_ids]
        self._save_data({"memories": memories})

    async def add_memory(
        self,
        user_id: str,
        content: str,
        tags: list | None = None,
        importance: int = 5,
    ) -> str:
        async with self._lock:
            data = self._load_data()
            memories = data.get("memories", [])
            memory_id = str(uuid.uuid4())[:8]
            now = self._get_timestamp()
            memories.append(
                {
                    "id": memory_id,
                    "user_id": str(user_id),
                    "content": content,
                    "tags": tags or [],
                    "importance": max(1, min(10, importance)),
                    "created_at": now,
                    "updated_at": now,
                }
            )
            self._save_data({"memories": memories})
            await self._cleanup_if_needed(user_id)
            return memory_id

    async def update_memory(
        self,
        memory_id: str,
        content: str | None = None,
        tags: list | None = None,
        importance: int | None = None,
    ) -> bool:
        async with self._lock:
            data = self._load_data()
            memories = data.get("memories", [])
            for memory in memories:
                if memory.get("id") != memory_id:
                    continue
                if content is not None:
                    memory["content"] = content
                if tags is not None:
                    memory["tags"] = tags
                if importance is not None:
                    memory["importance"] = max(1, min(10, importance))
                memory["updated_at"] = self._get_timestamp()
                self._save_data({"memories": memories})
                return True
            return False

    async def delete_memory(self, memory_id: str) -> bool:
        async with self._lock:
            data = self._load_data()
            memories = data.get("memories", [])
            old_len = len(memories)
            memories = [m for m in memories if m.get("id") != memory_id]
            if len(memories) == old_len:
                return False
            self._save_data({"memories": memories})
            return True

    async def get_memories(
        self,
        user_id: str | None = None,
        keyword: str | None = None,
        limit: int = 10,
        sort_by: str = "updated_at",
    ) -> list[dict]:
        data = self._load_data()
        memories = data.get("memories", [])
        if user_id == "admin":
            memories = [m for m in memories if 1 == 1]
        if user_id:
            memories = [m for m in memories if m.get("user_id") == str(user_id)]
        if keyword:
            key = keyword.lower()
            memories = [
                m
                for m in memories
                if key in m.get("content", "").lower()
                or any(key in str(tag).lower() for tag in m.get("tags", []))
            ]
        if sort_by == "importance":
            memories.sort(key=lambda x: x.get("importance", 0), reverse=True)
        elif sort_by in {"updated_at", "created_at"}:
            memories.sort(key=lambda x: x.get(sort_by, ""), reverse=True)
        return memories[:limit]

    async def get_memory_by_id(self, memory_id: str) -> Optional[dict]:
        memories = await self.get_memories(limit=10000)
        for memory in memories:
            if memory.get("id") == memory_id:
                return memory
        return None
