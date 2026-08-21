"""
网易云音乐 API 封装模块
实现 weapi 加密接口，支持搜索、获取歌曲播放地址、歌曲详情等
"""

import json
import base64
import random
import string
import hashlib
import requests
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad


# ============================================================
# weapi 加密相关
# ============================================================

# 网易云固定 AES 密钥和 IV
_AES_KEY = "0CoJUm6Qyw8W8jud"
_AES_IV = b"0102030405060708"

# 网易云 RSA 公钥模数 (十六进制)
_RSA_PUB_KEY = int(
    "00e0b509f6259df8642dbc35662901477df22677ec152b5ff68ace615bb7b725"
    "152b3ab17a876aea8a5aa76d2e417629ec4ee341f56135fccf695280104e0312"
    "ecbda92557c93870114af6c9d05c4f7f0c3685b7a46bee255932575cce10b424"
    "d813cfe4875d3e82047b97ddef52741d546b8e289dc6935b3ece0462db0a22b8e7",
    16,
)
_RSA_EXP = 65537


def _rand_str(length: int = 16) -> str:
    """生成指定长度的随机字符串（字母+数字）"""
    return "".join(random.choices(string.ascii_letters + string.digits, k=length))


def _aes_encrypt(text: str, key: str) -> str:
    """AES-CBC 加密，返回 base64"""
    cipher = AES.new(key.encode("utf-8"), AES.MODE_CBC, _AES_IV)
    encrypted = cipher.encrypt(pad(text.encode("utf-8"), AES.block_size))
    return base64.b64encode(encrypted).decode("utf-8")


def _rsa_encrypt(text: str) -> str:
    """网易云 RSA 加密（反转文本后做模幂）"""
    text = text[::-1]
    rs = int(text.encode("utf-8").hex(), 16)
    return format(pow(rs, _RSA_EXP, _RSA_PUB_KEY), "x").zfill(256)


def _weapi(data: dict) -> dict:
    """将 dict 编码为 weapi 所需的 params + encSecKey"""
    text = json.dumps(data, ensure_ascii=False)
    secret = _rand_str(16)
    params = _aes_encrypt(_aes_encrypt(text, _AES_KEY), secret)
    enc_sec_key = _rsa_encrypt(secret)
    return {"params": params, "encSecKey": enc_sec_key}


# ============================================================
# API 客户端
# ============================================================

_BASE_URL = "https://music.163.com"
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Referer": "https://music.163.com/",
    "Content-Type": "application/x-www-form-urlencoded",
}


class NeteaseAPI:
    """网易云音乐 API 客户端"""

    def __init__(self, cookie: str = ""):
        self.session = requests.Session()
        self.session.headers.update(_HEADERS)
        if cookie:
            self.session.cookies.set("MUSIC_U", cookie, domain=".music.163.com")
        # 额外设置一些必要 cookie
        self.session.cookies.set("__remember_me", "true", domain=".music.163.com")
        self.session.cookies.set("NMTID", self._gen_nmtid(), domain=".music.163.com")

    @staticmethod
    def _gen_nmtid() -> str:
        return hashlib.md5(random.randbytes(16)).hexdigest()

    def _post(self, path: str, data: dict) -> dict:
        """发送 weapi POST 请求，带3次重试"""
        url = f"{_BASE_URL}{path}"
        payload = _weapi(data)
        last_error = None
        for attempt in range(3):
            try:
                resp = self.session.post(url, data=payload, timeout=30)
                resp.raise_for_status()
                return resp.json()
            except Exception as e:
                last_error = e
                if attempt < 2:
                    import time
                    time.sleep(1 * (attempt + 1))
        raise last_error

    # ----------------------------------------------------------
    # 搜索
    # ----------------------------------------------------------
    def search(self, keyword: str, limit: int = 30, offset: int = 0) -> dict:
        """
        搜索歌曲
        返回: {"songs": [...], "songCount": N}
        """
        path = "/weapi/search/get"
        data = {
            "s": keyword,
            "type": 1,       # 1=单曲
            "limit": limit,
            "offset": offset,
        }
        return self._post(path, data)

    # ----------------------------------------------------------
    # 获取歌曲播放地址
    # ----------------------------------------------------------
    def get_song_url(self, song_ids: list, level: str = "standard") -> dict:
        """
        获取歌曲播放直链
        level: standard / higher / exhigh / lossless / hires / jyeffect / sky / jymaster
        """
        path = "/weapi/song/enhance/player/url/v1"
        data = {
            "ids": json.dumps(song_ids),
            "level": level,
            "encodeType": "mp3",
        }
        return self._post(path, data)

    # ----------------------------------------------------------
    # 获取歌曲详情（名称、歌手、专辑、封面）
    # ----------------------------------------------------------
    def get_song_detail(self, song_ids: list) -> dict:
        """获取歌曲详情"""
        path = "/weapi/v3/song/detail"
        c = json.dumps([{"id": sid} for sid in song_ids])
        data = {"c": c, "ids": json.dumps(song_ids)}
        return self._post(path, data)

    # ----------------------------------------------------------
    # 歌词
    # ----------------------------------------------------------
    def get_lyric(self, song_id: int) -> dict:
        """获取歌词"""
        path = "/weapi/song/lyric"
        data = {"id": song_id, "lv": -1, "kv": -1, "tv": -1}
        return self._post(path, data)

    # ----------------------------------------------------------
    # 便捷方法：搜索并返回精简列表
    # ----------------------------------------------------------
    def search_songs_simple(self, keyword: str, limit: int = 20) -> list:
        """
        搜索歌曲，返回精简列表：
        [{"id": int, "name": str, "artist": str, "album": str, "cover": str, "duration": int}, ...]
        """
        result = self.search(keyword, limit=limit)
        songs = result.get("result", {}).get("songs", [])
        simple_list = []
        for s in songs:
            artists = "/".join(a.get("name", "") for a in s.get("artists", []))
            album = s.get("album", {}).get("name", "")
            cover = s.get("album", {}).get("picUrl", "")
            simple_list.append({
                "id": s.get("id"),
                "name": s.get("name", ""),
                "artist": artists,
                "album": album,
                "cover": cover,
                "duration": s.get("duration", 0),  # 毫秒
            })
        return simple_list

    def get_first_song_url(self, song_id: int, level: str = "standard") -> str:
        """获取单首歌的播放直链，失败返回空字符串"""
        result = self.get_song_url([song_id], level=level)
        data_list = result.get("data", [])
        if data_list:
            return data_list[0].get("url", "") or ""
        return ""

    # ----------------------------------------------------------
    # 排行榜
    # ----------------------------------------------------------
    def get_toplist_songs(self, playlist_id: int = 3778678, limit: int = 100) -> list:
        """
        获取排行榜/歌单歌曲（默认云音乐热歌榜 3778678）
        超过500首时分批获取歌曲详情，避免API超时
        返回精简列表，同 search_songs_simple 格式
        优化：减少批次延迟到0.1秒
        """
        path = "/weapi/v6/playlist/detail"
        data = {"id": playlist_id, "n": 10000, "s": 0}
        result = self._post(path, data)
        playlist = result.get("playlist", {})
        track_ids = [t["id"] for t in playlist.get("trackIds", [])][:limit]
        if not track_ids:
            return []

        # 分批获取歌曲详情（每批500首，避免API超时或返回不完整）
        BATCH_SIZE = 500
        all_songs = []
        for i in range(0, len(track_ids), BATCH_SIZE):
            batch_ids = track_ids[i:i + BATCH_SIZE]
            try:
                detail = self.get_song_detail(batch_ids)
                songs = detail.get("songs", [])
                all_songs.extend(songs)
            except Exception as e:
                print(f"[NeteaseAPI] 歌单详情分批获取失败 (batch {i//BATCH_SIZE + 1}): {e}")
            if i + BATCH_SIZE < len(track_ids):
                import time
                time.sleep(0.1)  # 优化：批次间延迟从0.3秒减少到0.1秒

        simple_list = []
        for s in all_songs:
            artists = "/".join(a.get("name", "") for a in s.get("ar", []))
            album = s.get("al", {}).get("name", "")
            cover = s.get("al", {}).get("picUrl", "")
            simple_list.append({
                "id": s.get("id"),
                "name": s.get("name", ""),
                "artist": artists,
                "album": album,
                "cover": cover,
                "duration": s.get("dt", 0),
            })
        return simple_list

    async def get_toplist_songs_async(self, playlist_id: int = 3778678, limit: int = 10000, max_concurrent: int = 2) -> list:
        """
        异步并发获取歌单歌曲（优化版）
        使用 asyncio.gather 并发获取多批歌曲详情，并发数默认2
        返回精简列表
        """
        import asyncio
        path = "/weapi/v6/playlist/detail"
        data = {"id": playlist_id, "n": 10000, "s": 0}
        result = await asyncio.to_thread(self._post, path, data)
        playlist = result.get("playlist", {})
        track_ids = [t["id"] for t in playlist.get("trackIds", [])][:limit]
        if not track_ids:
            return []

        # 分批获取歌曲详情（每批500首）
        BATCH_SIZE = 500
        batches = [track_ids[i:i + BATCH_SIZE] for i in range(0, len(track_ids), BATCH_SIZE)]

        # 并发获取歌曲详情（控制并发数）
        semaphore = asyncio.Semaphore(max_concurrent)

        async def fetch_batch(batch_ids, batch_idx):
            async with semaphore:
                try:
                    detail = await asyncio.to_thread(self.get_song_detail, batch_ids)
                    songs = detail.get("songs", [])
                    print(f"[NeteaseAPI] 并发获取歌单详情 batch {batch_idx + 1}/{len(batches)} 成功，{len(songs)}首")
                    return songs
                except Exception as e:
                    print(f"[NeteaseAPI] 并发获取歌单详情失败 (batch {batch_idx + 1}): {e}")
                    return []

        tasks = [fetch_batch(batch, idx) for idx, batch in enumerate(batches)]
        results = await asyncio.gather(*tasks)

        all_songs = []
        for songs in results:
            all_songs.extend(songs)

        simple_list = []
        for s in all_songs:
            artists = "/".join(a.get("name", "") for a in s.get("ar", []))
            album = s.get("al", {}).get("name", "")
            cover = s.get("al", {}).get("picUrl", "")
            simple_list.append({
                "id": s.get("id"),
                "name": s.get("name", ""),
                "artist": artists,
                "album": album,
                "cover": cover,
                "duration": s.get("dt", 0),
            })
        return simple_list

    # ----------------------------------------------------------
    # 用户歌单
    # ----------------------------------------------------------
    def get_user_playlists(self, uid: int, limit: int = 30, offset: int = 0) -> list:
        """
        获取用户的歌单列表
        返回: [{"id": int, "name": str, "trackCount": int, "cover": str}, ...]
        """
        path = "/weapi/user/playlist"
        data = {
            "uid": uid,
            "limit": limit,
            "offset": offset,
            "includeVideo": True,
        }
        result = self._post(path, data)
        playlists = result.get("playlist", [])
        simple_list = []
        for p in playlists:
            simple_list.append({
                "id": p.get("id"),
                "name": p.get("name", ""),
                "trackCount": p.get("trackCount", 0),
                "cover": p.get("coverImgUrl", ""),
                "creator": p.get("creator", {}).get("nickname", ""),
            })
        return simple_list

    def get_user_playlist_songs(self, uid: int, max_per_playlist: int = 100) -> list:
        """
        获取用户所有歌单中的歌曲（去重）
        返回精简歌曲列表
        """
        all_songs = []
        seen_ids = set()
        try:
            playlists = self.get_user_playlists(uid, limit=100)
            for pl in playlists:
                try:
                    songs = self.get_toplist_songs(pl["id"], limit=max_per_playlist)
                    for s in songs:
                        if s["id"] not in seen_ids:
                            seen_ids.add(s["id"])
                            all_songs.append(s)
                except Exception:
                    continue
        except Exception:
            pass
        return all_songs

    # ----------------------------------------------------------
    # Cookie 管理
    # ----------------------------------------------------------
    def update_cookie(self, cookie: str):
        """动态更新 MUSIC_U cookie"""
        self.session.cookies.set("MUSIC_U", cookie, domain=".music.163.com")

    def get_cookie(self) -> str:
        """获取当前 MUSIC_U cookie 值"""
        for c in self.session.cookies:
            if c.name == "MUSIC_U":
                return c.value
        return ""

    def refresh_cookie(self) -> str:
        """
        调用网易云登录态刷新接口，返回新的 MUSIC_U cookie。
        刷新成功会自动更新当前 session 的 cookie，失败返回空字符串。
        """
        try:
            url = f"{_BASE_URL}/weapi/login/token/refresh"
            payload = _weapi({})
            resp = self.session.post(url, data=payload, timeout=30)
            # 从响应 Set-Cookie 中提取新的 MUSIC_U
            new_cookie = ""
            for c in resp.cookies:
                if c.name == "MUSIC_U" and c.value:
                    new_cookie = c.value
                    break
            if new_cookie:
                self.update_cookie(new_cookie)
                return new_cookie
        except Exception as e:
            print(f"[NeteaseAPI] 刷新cookie失败: {e}")
        return ""

    def check_cookie_valid(self) -> bool:
        """快速检测cookie是否有效（调用一次搜索接口判断）"""
        try:
            result = self.search("test", limit=1)
            # 有效cookie返回 code=200
            return result.get("code") == 200
        except Exception:
            return False

    # ----------------------------------------------------------
    # 用户歌单
    # ----------------------------------------------------------
    def get_user_playlists(self, uid: int, limit: int = 30, offset: int = 0) -> list:
        """
        获取指定用户的歌单列表
        返回: [{"id": int, "name": str, "trackCount": int, "coverImgUrl": str}, ...]
        """
        path = "/weapi/user/playlist"
        data = {
            "uid": uid,
            "limit": limit,
            "offset": offset,
            "includeVideo": True,
        }
        result = self._post(path, data)
        playlists = result.get("playlist", [])
        simple_list = []
        for p in playlists:
            simple_list.append({
                "id": p.get("id"),
                "name": p.get("name", ""),
                "trackCount": p.get("trackCount", 0),
                "coverImgUrl": p.get("coverImgUrl", ""),
            })
        return simple_list

    def get_current_user_id(self) -> int:
        """获取当前登录账号的用户ID（基于cookie），失败返回0"""
        try:
            path = "/weapi/nuser/account/get"
            result = self._post(path, {})
            profile = result.get("profile", {})
            return profile.get("userId", 0)
        except Exception:
            return 0

    def get_playlist_songs(self, playlist_id: int, limit: int = 1000) -> list:
        """
        获取指定歌单的所有歌曲（精简格式）
        返回格式同 search_songs_simple
        """
        return self.get_toplist_songs(playlist_id, limit=limit)
