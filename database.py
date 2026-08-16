"""
Upstash Redis 数据库封装
使用 Upstash REST API，无需额外 Redis 客户端库
"""

import json
import requests
import config


class UpstashDB:
    """Upstash Redis 数据库操作"""

    def __init__(self):
        self.url = config.UPSTASH_REDIS_REST_URL.rstrip("/")
        self.token = config.UPSTASH_REDIS_REST_TOKEN
        self.headers = {"Authorization": f"Bearer {self.token}"}
        self.enabled = bool(self.url and self.token)

    def _exec(self, *args):
        """执行 Redis 命令（POST 方式，支持中文等任意字符）"""
        if not self.enabled:
            return None
        try:
            payload = json.dumps(list(args))
            resp = requests.post(
                self.url,
                data=payload,
                headers=self.headers,
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()
            return data.get("result")
        except Exception as e:
            print(f"[Upstash] 命令失败 {args[0]}: {e}")
            return None

    # ---- 用户集合 ----
    def add_user(self, user_id: int):
        self._exec("SADD", "bot:users", str(user_id))

    def get_users(self) -> list:
        result = self._exec("SMEMBERS", "bot:users")
        return result if result else []

    # ---- 封禁集合 ----
    def ban_user(self, user_id):
        self._exec("SADD", "bot:banned", str(user_id))

    def unban_user(self, user_id):
        self._exec("SREM", "bot:banned", str(user_id))

    def get_banned(self) -> list:
        result = self._exec("SMEMBERS", "bot:banned")
        return result if result else []

    def is_banned(self, user_id: int) -> bool:
        result = self._exec("SISMEMBER", "bot:banned", str(user_id))
        return bool(result)

    # ---- 欢迎语 ----
    def get_welcome(self) -> str:
        result = self._exec("GET", "bot:welcome")
        return result if result else ""

    def set_welcome(self, text: str):
        self._exec("SET", "bot:welcome", text)

    def reset_welcome(self):
        self._exec("DEL", "bot:welcome")

    # ---- 统计 ----
    def incr_search(self):
        self._exec("HINCRBY", "bot:stats", "total_searches", 1)

    def incr_play(self):
        self._exec("HINCRBY", "bot:stats", "total_plays", 1)

    def get_stats(self) -> dict:
        result = self._exec("HGETALL", "bot:stats")
        if result and isinstance(result, list):
            return {result[i]: int(result[i + 1]) for i in range(0, len(result), 2)}
        return {"total_searches": 0, "total_plays": 0}


# 全局实例
db = UpstashDB()
