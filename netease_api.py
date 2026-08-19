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
        result = self._post(path, data)
        songs = result.get("result", {}).get("songs", [])
        # 旧端点返回空时，尝试新版cloudsearch端点
        if not songs:
            try:
                cloud_path = "/weapi/cloudsearch/get/web"
                cloud_data = {
                    "s": keyword,
                    "type": 1,
                    "limit": limit,
                    "offset": offset,
                }
                cloud_result = self._post(cloud_path, cloud_data)
                cloud_songs = cloud_result.get("result", {}).get("songs", [])
                if cloud_songs:
                    # 转换cloudsearch格式为旧格式（ar->artists, al->album, dt->duration）
                    converted = []
                    for s in cloud_songs:
                        converted.append({
                            "id": s.get("id"),
                            "name": s.get("name", ""),
                            "artists": [{"id": a.get("id"), "name": a.get("name", "")} for a in s.get("ar", [])],
                            "album": {"id": s.get("al", {}).get("id"), "name": s.get("al", {}).get("name", ""), "picUrl": s.get("al", {}).get("picUrl", "")},
                            "duration": s.get("dt", 0),
                        })
                    result = {"result": {"songs": converted, "songCount": cloud_result.get("result", {}).get("songCount", 0)}}
            except Exception as e:
                pass  # cloudsearch失败时保持原结果
        return result

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
        获取排行榜歌曲（默认云音乐热歌榜 3778678）
        返回精简列表，同 search_songs_simple 格式
        """
        path = "/weapi/v6/playlist/detail"
        data = {"id": playlist_id, "n": 1000, "s": 0}
        result = self._post(path, data)
        playlist = result.get("playlist", {})
        track_ids = [t["id"] for t in playlist.get("trackIds", [])][:limit]
        if not track_ids:
            return []
        # 批量获取歌曲详情
        detail = self.get_song_detail(track_ids)
        songs = detail.get("songs", [])
        simple_list = []
        for s in songs:
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
