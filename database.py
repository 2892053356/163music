"""
Upstash Redis 数据库封装
使用 Upstash REST API，无需额外 Redis 客户端库
"""

import json
import time
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

    # ---- Cookie 存储（运行时可更新） ----
    def get_cookie(self) -> str:
        result = self._exec("GET", "bot:cookie")
        return result if result else ""

    def set_cookie(self, cookie: str):
        self._exec("SET", "bot:cookie", cookie)
        self._exec("SET", "bot:cookie_updated_at", str(int(time.time())))

    def get_cookie_updated_at(self) -> int:
        result = self._exec("GET", "bot:cookie_updated_at")
        return int(result) if result else 0

    # ---- 音质设置 ----
    def get_quality(self) -> str:
        """获取当前音质设置，默认standard"""
        result = self._exec("GET", "bot:quality")
        return result if result else "standard"

    def set_quality(self, quality: str):
        """设置音质（standard/higher）"""
        self._exec("SET", "bot:quality", quality)

    # ---- Cookie检测时间（重启后继续检测周期）----
    def get_last_cookie_check(self) -> int:
        """获取上次Cookie检测时间戳"""
        result = self._exec("GET", "bot:last_cookie_check")
        return int(result) if result else 0

    def set_last_cookie_check(self, ts: int):
        """设置上次Cookie检测时间戳"""
        self._exec("SET", "bot:last_cookie_check", str(ts))

    # ---- 歌单播放状态（重启后继续播放）----
    def save_active_playlist(self, user_id: int, playlist_id: int, songs: list, current_index: int = 0):
        """保存用户正在播放的歌单状态"""
        import json
        data = {
            "playlist_id": playlist_id,
            "current_index": current_index,
            "total": len(songs),
            "songs": songs,
            "start_time": int(time.time())
        }
        self._exec("SET", f"playlist:active:{user_id}", json.dumps(data, ensure_ascii=False), "EX", 86400)
        self._exec("SADD", "playlist:active_users", str(user_id))

    def get_active_playlist(self, user_id: int) -> dict:
        """获取用户正在播放的歌单状态"""
        import json
        result = self._exec("GET", f"playlist:active:{user_id}")
        if not result:
            return {}
        try:
            return json.loads(result)
        except Exception:
            return {}

    def update_playlist_index(self, user_id: int, current_index: int):
        """更新歌单播放进度"""
        import json
        data = self.get_active_playlist(user_id)
        if not data:
            return
        data["current_index"] = current_index
        self._exec("SET", f"playlist:active:{user_id}", json.dumps(data, ensure_ascii=False), "EX", 86400)

    def remove_active_playlist(self, user_id: int):
        """移除用户的歌单播放状态（播放完成或被停止）"""
        self._exec("DEL", f"playlist:active:{user_id}")
        self._exec("SREM", "playlist:active_users", str(user_id))

    def get_active_playlist_users(self) -> list:
        """获取所有正在播放歌单的用户ID列表"""
        result = self._exec("SMEMBERS", "playlist:active_users")
        return [int(x) for x in result] if result else []

    def set_playlist_stop_flag(self, user_id: int):
        """设置停止歌单播放的标志（管理员停止用户播放）"""
        self._exec("SET", f"playlist:stop:{user_id}", "1", "EX", 60)

    def check_playlist_stop_flag(self, user_id: int) -> bool:
        """检查是否有停止标志，并清除"""
        exists = self._exec("EXISTS", f"playlist:stop:{user_id}")
        if exists:
            self._exec("DEL", f"playlist:stop:{user_id}")
            return True
        return False

    # ---- Telegram file_id 缓存（避免重复上传音频） ----
    def get_file_id(self, song_id: int) -> str:
        result = self._exec("GET", f"cache:file_id:{song_id}")
        return result if result else ""

    def get_file_ids_batch(self, song_ids: list) -> dict:
        """批量获取file_id，返回 {song_id: file_id} 字典"""
        if not song_ids:
            return {}
        keys = [f"cache:file_id:{sid}" for sid in song_ids]
        results = self._exec("MGET", *keys)
        if not results:
            return {sid: "" for sid in song_ids}
        return {sid: (results[i] if i < len(results) and results[i] else "") for i, sid in enumerate(song_ids)}

    def set_file_id(self, song_id: int, file_id: str):
        self._exec("SET", f"cache:file_id:{song_id}", file_id)

    # ---- 用户搜索历史（用于闲时缓存扩展） ----
    def add_searched_song(self, song_id: int):
        """记录用户搜索过的歌曲ID"""
        self._exec("SADD", "cache:searched_songs", str(song_id))

    def get_uncached_searched_songs(self, limit: int = 100) -> list:
        """获取用户搜索过但未缓存的歌曲ID列表"""
        all_searched = self._exec("SMEMBERS", "cache:searched_songs") or []
        uncached = []
        for sid_str in all_searched:
            try:
                sid = int(sid_str)
                if not self.get_file_id(sid):
                    uncached.append(sid)
                    if len(uncached) >= limit:
                        break
            except (ValueError, TypeError):
                continue
        return uncached

    def clear_all_file_ids(self) -> int:
        """清除所有file_id缓存，返回删除数量"""
        keys = self._exec("KEYS", "cache:file_id:*")
        if not keys:
            return 0
        count = 0
        for k in keys:
            self._exec("DEL", k)
            count += 1
        return count

    # ---- 管理员管理（主管理员来自环境变量，附加管理员存Redis） ----
    def get_admins(self) -> list:
        """获取所有附加管理员ID列表"""
        result = self._exec("SMEMBERS", "bot:admins")
        return [int(x) for x in result] if result else []

    def add_admin(self, user_id: int):
        """添加管理员"""
        self._exec("SADD", "bot:admins", str(user_id))

    def remove_admin(self, user_id: int):
        """移除管理员"""
        self._exec("SREM", "bot:admins", str(user_id))

    def is_admin(self, user_id: int) -> bool:
        """检查是否为管理员（含主管理员）"""
        if user_id == config.ADMIN_ID:
            return True
        result = self._exec("SISMEMBER", "bot:admins", str(user_id))
        return bool(result)

    # ---- 配置管理（存储在Redis，可从database读取环境变量）----
    def get_config(self, key: str) -> str:
        """从Redis获取配置值"""
        result = self._exec("HGET", "bot:config", key)
        return result if result else ""

    def set_config(self, key: str, value: str):
        """设置配置值到Redis"""
        self._exec("HSET", "bot:config", key, value)

    def get_all_config(self) -> dict:
        """获取所有配置"""
        result = self._exec("HGETALL", "bot:config")
        if result and isinstance(result, list) and len(result) >= 2:
            # Redis HGETALL 返回 [key1, value1, key2, value2, ...]
            config_dict = {}
            for i in range(0, len(result), 2):
                if i + 1 < len(result):
                    config_dict[result[i]] = result[i + 1]
            return config_dict
        return {}

    def delete_config(self, key: str):
        """删除配置值"""
        self._exec("HDEL", "bot:config", key)


# 全局实例
db = UpstashDB()
