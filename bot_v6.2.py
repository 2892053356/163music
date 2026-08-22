"""
Telegram 网易云音乐机器人
功能：
  - /start  开始使用
  - /help   帮助
  - /music <关键词>  搜索歌曲（按钮选择播放）
  - 内联搜索：@XiOuDi163_bot <关键词>
  - 管理员：/admin /broadcast /stats /ban /unban
"""

import io
import os
import re
import json
import time
import asyncio
import logging
import hashlib
import requests
from datetime import datetime
from urllib.parse import quote

from aiohttp import web

from telegram import (
    Update,
    InlineQueryResultArticle,
    InlineQueryResultAudio,
    InlineQueryResultCachedAudio,
    InputTextMessageContent,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    InlineQueryHandler,
    ChosenInlineResultHandler,
    CallbackQueryHandler,
    MessageHandler,
    filters,
    ContextTypes,
)
from telegram.request import HTTPXRequest

# ============================================================
# 加载 .env 文件（本地部署用，自动设置环境变量）
# ============================================================
def _load_env():
    """加载当前目录下的 .env 文件到环境变量"""
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if not os.path.exists(env_path):
        return
    try:
        with open(env_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = value
    except Exception as e:
        print(f"[警告] 加载 .env 失败: {e}")

_load_env()

import config
from netease_api import NeteaseAPI
from database import db

# ============================================================
# 日志配置（rich 美化输出，未安装则回退普通格式）
# ============================================================
try:
    from rich.logging import RichHandler
    from rich.console import Console
    from rich import print as rprint

    _console = Console()
    logging.basicConfig(
        level=logging.INFO,
        format="%(message)s",
        datefmt="[%X]",
        handlers=[RichHandler(
            console=_console,
            rich_tracebacks=True,
            show_path=False,
            markup=True,
        )]
    )
    _USE_RICH = True
    logger = logging.getLogger("rich")
except ImportError:
    logging.basicConfig(
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        level=logging.INFO,
    )
    _USE_RICH = False
    _console = None
    logger = logging.getLogger(__name__)


def _print_banner():
    """打印启动横幅"""
    if not _USE_RICH:
        print("=" * 50)
        print("  网易云音乐 Telegram Bot v7.3")
        print("=" * 50)
        return
    from rich.panel import Panel
    from rich.text import Text
    from rich.table import Table

    banner = Text()
    banner.append("🎵 网易云音乐 Telegram Bot\n", style="bold magenta")
    banner.append("   v7.3 - 本地部署优化版", style="cyan")

    _console.print(Panel(banner, border_style="magenta", padding=(1, 4)))

    # 配置信息表格
    table = Table(show_header=False, box=None, padding=(0, 2))
    table.add_column(style="bold cyan")
    table.add_column(style="white")
    table.add_row("🤖 Bot Token", f"{config.BOT_TOKEN[:10]}...{config.BOT_TOKEN[-4:]}" if config.BOT_TOKEN else "未配置")
    table.add_row("👑 管理员 ID", str(config.ADMIN_ID))
    table.add_row("🎵 音质等级", config.MUSIC_QUALITY)
    table.add_row("💾 数据库类型", config.DB_TYPE)
    table.add_row("🌐 Webhook URL", config.WEBHOOK_URL or "未配置")
    table.add_row("🔊 监听端口", str(config.PORT))
    _console.print(table)
    _console.print()


def _log_status(message: str, style: str = "info"):
    """美化状态输出"""
    if not _USE_RICH:
        print(f"[{style.upper()}] {message}")
        return
    style_map = {
        "info": "cyan",
        "success": "bold green",
        "warning": "bold yellow",
        "error": "bold red",
        "cache": "magenta",
        "play": "green",
        "search": "blue",
    }
    color = style_map.get(style, "white")
    _console.print(f"[{color}]{message}[/{color}]")

# ============================================================
# 全局实例
# ============================================================

# 先从 Database 加载配置（优先于环境变量，减少 Render 环境变量配置）
# 需要的配置：bot:token, bot:admin_id, bot:cf_proxy_url, bot:cookie
_db_token = db.get_bot_token()
_db_admin_id = db.get_admin_id()
_db_cf_proxy = db.get_cf_proxy_url()
_db_cookie = db.get_cookie()

# 覆盖 config 中的值（Database 优先，环境变量作为兜底）
if _db_token:
    config.BOT_TOKEN = _db_token
if _db_admin_id:
    config.ADMIN_ID = _db_admin_id
if _db_cf_proxy:
    config.CF_PROXY_URL = _db_cf_proxy

# 使用 Database 中的 cookie 初始化 api（优先），环境变量作为兜底
_init_cookie = _db_cookie if _db_cookie else config.NETEASE_COOKIE
api = NeteaseAPI(cookie=_init_cookie)

# 用户活动时间戳（用于缓存任务优先级控制：有用户请求时暂停缓存）
last_user_activity = 0
inline_last_query = {}  # user_id -> (query, timestamp) 用于内联搜索防抖
_processed_update_ids = set()  # 去重：防止Telegram重试导致重复处理

# 内联请求活跃计数（优先级控制：有内联请求时暂停歌单缓存和闲时缓存）
# 内联请求开始时+1，内联结果返回后延迟10秒-1（给用户选择结果和发送音频的时间）
inline_request_active = 0

# 缓存任务引用（内联请求时可 cancel() 真正立即中断）
manual_cache_task = None   # 手动缓存任务（/cache 命令）
auto_cache_task = None     # 闲时自动缓存任务

# 搜索播放活动集合（优先级控制：用户搜索播放时暂停歌单播放）
# 内联搜索 > 普通搜索 > 歌单播放 > 闲时缓存
active_search_plays = set()  # 当前正在进行搜索播放的用户ID集合

# 歌单播放队列（用户已有歌单播放时，新歌单加入排队）
# key: user_id, value: [(playlist_id, songs), ...] 待播放歌单列表
playlist_queue = {}

# 歌单播放已发送歌曲记录（去重：5秒内不能发送给同一用户相同歌曲）
# key: user_id, value: {song_id: timestamp} 最近发送的歌曲及时间戳
playlist_sent_songs = {}

# 闲时自动缓存状态
auto_cache_running = False  # 是否正在执行自动缓存
auto_cache_enabled = True   # 自动缓存开关
_do_auto_cache_func = None  # 立即缓存函数引用（在run_server中赋值）
AUTO_CACHE_IDLE_THRESHOLD = 300  # 闲时阈值：5分钟无用户活动视为空闲
# 闲时缓存的排行榜列表（多个榜单合集，覆盖更多歌曲）
# 主榜单（优先缓存）
AUTO_CACHE_PRIMARY_PLAYLISTS = [
    3778678,   # 热歌榜
    3779629,   # 新歌榜
    19723756,  # 飙升榜
    2884035,   # 原创榜
    71385702,  # 网络歌曲榜
    71384707,  # 电子榜
    71385487,  # 说唱榜
    112504,    # 华语金曲榜
]
# 扩展榜单（主榜单缓存完后继续缓存）
AUTO_CACHE_EXTENDED_PLAYLISTS = [
    60198,     # 美国Billboard榜
    60131,     # 日本Oricon榜
    11641012,  # 英国Q杂志榜
    180106,    # 韩国Mnet榜
    71380410,  # 民谣榜
    71380409,  # 摇滚榜
    71380408,  # 流行榜
    71380407,  # 轻音乐榜
    71380406,  # 爵士榜
    71380405,  # R&B榜
    71380404,  # 乡村榜
    3812895,   # 古典音乐榜
    27135204,  # 台湾KKBOX榜
    112463,    # 香港电台榜
    71380403,  # 蓝调榜
    71380402,  # 雷鬼榜
]
AUTO_CACHE_PLAYLISTS = AUTO_CACHE_PRIMARY_PLAYLISTS + AUTO_CACHE_EXTENDED_PLAYLISTS

# 排行榜ID到名称的映射（用于日志显示）
PLAYLIST_NAMES = {
    3778678: "热歌榜", 3779629: "新歌榜", 19723756: "飙升榜", 2884035: "原创榜",
    71385702: "网络歌曲榜", 71384707: "电子榜", 71385487: "说唱榜", 112504: "华语金曲榜",
    60198: "美国Billboard榜", 60131: "日本Oricon榜", 11641012: "英国Q杂志榜",
    180106: "韩国Mnet榜", 71380410: "民谣榜", 71380409: "摇滚榜", 71380408: "流行榜",
    71380407: "轻音乐榜", 71380406: "爵士榜", 71380405: "R&B榜", 71380404: "乡村榜",
    3812895: "古典音乐榜", 27135204: "台湾KKBOX榜", 112463: "香港电台榜",
    71380403: "蓝调榜", 71380402: "雷鬼榜",
}
# 曲库Redis缓存过期时间：7天（每周更新一次）
AUTO_CACHE_REDIS_EXPIRE = 604800

# ============================================================
# 数据存储（Upstash Redis 持久化）
# ============================================================

def _register_user(user_id: int):
    """记录用户（去重）"""
    db.add_user(user_id)


def _is_banned(user_id: int) -> bool:
    return db.is_banned(user_id)


def _is_admin(user_id: int) -> bool:
    return db.is_admin(user_id)


async def _notify_all_admins(context, text: str):
    """向所有管理员（主管理员+附加管理员）发送通知消息"""
    admin_ids = set()
    admin_ids.add(config.ADMIN_ID)
    try:
        for aid in db.get_admins():
            admin_ids.add(aid)
    except Exception:
        pass
    for aid in admin_ids:
        try:
            await context.bot.send_message(chat_id=aid, text=text)
        except Exception as e:
            logger.warning(f"通知管理员失败 aid={aid}: {e}")


# ============================================================
# 工具函数
# ============================================================

def _fmt_duration(ms: int) -> str:
    """毫秒转 分:秒"""
    sec = ms // 1000
    return f"{sec // 60}:{sec % 60:02d}"


def _is_wrong_audio_title(actual_title: str, expected_name: str, song_id: int) -> bool:
    """
    检测音频标题是否不正确（需要删除file_id缓存并重新上传）
    
    不正确的情况：
    1. 标题为空
    2. 标题是纯数字（歌曲id）
    3. 标题等于song_id
    4. 标题是长字母串（如file_id本身，长度>40且主要是字母数字）
    5. 标题与期望名称明显不同（忽略大小写和空格，且不是期望名称的子串）
    """
    if not actual_title or not actual_title.strip():
        return True
    
    title = actual_title.strip()
    
    # 纯数字（歌曲id）
    if title.isdigit():
        return True
    
    # 等于song_id
    if title == str(song_id):
        return True
    
    # 长字母串检测：长度>40且主要是字母数字（如file_id CQACAgUAAxkDAAIE32qF...）
    if len(title) > 40:
        # 计算字母数字比例
        alnum_count = sum(1 for c in title if c.isalnum())
        if alnum_count / len(title) > 0.8:
            return True
    
    # 与期望名称明显不同（忽略大小写和空格）
    # 放宽判断：只有当标题既不等于期望名称，也不包含期望名称时才判定为不正确
    if expected_name:
        actual_clean = title.replace(" ", "").lower()
        expected_clean = expected_name.replace(" ", "").lower()
        if actual_clean != expected_clean and expected_clean not in actual_clean and actual_clean not in expected_clean:
            return True
    
    return False


def _song_caption(song: dict) -> str:
    """生成歌曲信息文本"""
    return (
        f"🎵 <b>{song['name']}</b>\n"
        f"👤 {song['artist']}\n"
        f"💿 {song['album']}\n"
        f"⏱ {_fmt_duration(song['duration'])}"
    )


async def _send_audio_with_fallback(context, chat_id, song, quality="standard", caption=None, 
                                      message_thread_id=None, use_cache=True, log_prefix="", bot=None,
                                      user_id=None):
    """
    通用音频发送函数，支持多级代理回退：file_id缓存 → CF反向代理 → Render下载
    
    参数:
        context: Bot context（可为None，如果提供了bot参数）
        chat_id: 目标聊天ID
        song: 歌曲字典（包含id, name, artist, album, duration, cover）
        quality: 音质
        caption: 标题（默认使用_song_caption）
        message_thread_id: 话题ID
        use_cache: 是否使用file_id缓存
        log_prefix: 日志前缀
        bot: Bot实例（可选，优先使用）
        user_id: 用户ID（可选，用于5秒去重时间戳记录）
    
    返回:
        (success: bool, file_id: str or None, proxy_type: str)
    """
    song_id = song["id"]
    if caption is None:
        caption = _song_caption(song)
    
    # 使用提供的bot或context.bot
    _bot = bot or (context.bot if context else None)
    if not _bot:
        logger.error(f"{log_prefix}❌ 没有可用的bot实例")
        return False, None, "none"
    
    # 记录发送时间戳的辅助函数（5秒去重）
    def _record_sent():
        if user_id is not None:
            global playlist_sent_songs
            if user_id not in playlist_sent_songs:
                playlist_sent_songs[user_id] = {}
            playlist_sent_songs[user_id][song_id] = time.time()
    
    # 1. 优先使用 file_id 缓存
    if use_cache:
        cached = db.get_file_id(song_id)
        if cached:
            try:
                logger.info(f"{log_prefix}📦 使用file_id缓存: {song['name']} - {song['artist']}")
                msg = await _bot.send_audio(
                    chat_id=chat_id,
                    message_thread_id=message_thread_id,
                    audio=cached,
                    title=song["name"],
                    performer=song["artist"],
                    caption=caption,
                    parse_mode="HTML",
                    duration=song["duration"] // 1000 if song.get("duration") else None,
                )
                
                # 检查返回的音频标题是否正确
                if msg and msg.audio:
                    actual_title = (msg.audio.title or "").strip()
                    expected_name = (song["name"] or "").strip()
                    
                    # 使用通用错误标题检测函数
                    if _is_wrong_audio_title(actual_title, expected_name, song_id):
                        logger.warning(f"{log_prefix}⚠️ file_id缓存标题不正确: 实际='{actual_title}' 期望='{expected_name}'，删除消息并清除缓存重新上传")
                        # 删除刚才发送的错误标题消息
                        try:
                            await _bot.delete_message(chat_id=chat_id, message_id=msg.message_id)
                        except Exception as del_e:
                            logger.warning(f"{log_prefix}删除错误标题消息失败: {del_e}")
                        # 清除缓存
                        db.delete_file_id(song_id)
                        # 继续使用代理或Render下载重新发送（不return）
                    else:
                        # 标题正确，记录时间戳并返回
                        _record_sent()
                        return True, cached, "file_id"
                else:
                    _record_sent()
                    return True, cached, "file_id"
                    
            except Exception as e:
                logger.warning(f"{log_prefix}file_id缓存发送失败，清除失效缓存并回退代理: {e}")
                # 发送失败，清除可能失效的file_id缓存
                db.delete_file_id(song_id)
    
    # 2. 构建代理列表（已禁用CF代理，直接使用Render下载）
    proxy_list = []
    
    # CF代理已禁用（连接超时），直接使用Render下载
    # if config.CF_PROXY_URL:
    #     direct_url = await asyncio.to_thread(api.get_first_song_url, song_id, quality)
    #     if direct_url:
    #         from urllib.parse import quote
    #         cf_proxy_url = f"{config.CF_PROXY_URL.rstrip('/')}/proxy?url={quote(direct_url)}"
    #         proxy_list.append((cf_proxy_url, "CF反向代理"))
    
    # 3. 尝试每个代理（当前为空，直接到Render下载）
    for proxy_url, proxy_type in proxy_list:
        try:
            logger.info(f"{log_prefix}🌐 使用{proxy_type}: {song['name']} - {song['artist']}")
            
            msg = await _bot.send_audio(
                chat_id=chat_id,
                message_thread_id=message_thread_id,
                audio=proxy_url,
                filename=f"{song['name']}.mp3",
                title=song["name"],
                performer=song["artist"],
                caption=caption,
                parse_mode="HTML",
                duration=song["duration"] // 1000 if song.get("duration") else None,
            )
            
            if msg and msg.audio and msg.audio.file_id:
                db.set_file_id(song_id, msg.audio.file_id)
            
            # 记录发送时间戳（5秒去重）
            _record_sent()
            return True, msg.audio.file_id if msg and msg.audio else None, proxy_type
            
        except Exception as e:
            logger.warning(f"{log_prefix}{proxy_type}代理失败，尝试下一个: {e}")
            continue
    
    # 3. 所有代理失败，回退到 Render 下载
    try:
        logger.info(f"{log_prefix}🖥️ 使用Render下载: {song['name']} - {song['artist']}")
        url = await asyncio.to_thread(api.get_first_song_url, song_id, quality)
        if not url:
            logger.warning(f"{log_prefix}❌ 无法获取播放地址: {song['name']}")
            return False, None, "none"
        
        # 使用优化的下载模块
        from downloader import download_audio
        result = await download_audio(url, timeout=60, max_retries=2, log_prefix=f"{log_prefix}[Render] ")
        
        if not result.success:
            logger.warning(f"{log_prefix}❌ Render下载失败: {result.error}")
            return False, None, "none"
        
        audio_bytes = io.BytesIO(result.content)
        audio_bytes = _tag_mp3(audio_bytes, song)
        
        msg = await _bot.send_audio(
            chat_id=chat_id,
            message_thread_id=message_thread_id,
            audio=audio_bytes,
            filename=f"{song['name']}.mp3",
            title=song["name"],
            performer=song["artist"],
            caption=caption,
            parse_mode="HTML",
            duration=song["duration"] // 1000 if song.get("duration") else None,
        )
        
        if msg and msg.audio and msg.audio.file_id:
            db.set_file_id(song_id, msg.audio.file_id)
        
        # 记录发送时间戳（5秒去重）
        _record_sent()
        return True, msg.audio.file_id if msg and msg.audio else None, "Render"
        
    except Exception as e:
        logger.error(f"{log_prefix}❌ Render下载发送失败: {e}")
        return False, None, "none"


def _tag_mp3(audio_bytes: io.BytesIO, song: dict, cover_url: str = None) -> io.BytesIO:
    """给MP3写入ID3标签（标题、艺术家、专辑、封面），确保Telegram显示正确信息"""
    try:
        from mutagen.mp3 import MP3
        from mutagen.id3 import ID3, TIT2, TPE1, TALB, APIC
        # 兼容两种字段格式：搜索结果(artist/album字符串) 和 歌曲详情(ar数组/al对象)
        name = song.get("name", "未知歌曲")
        if "artist" in song:
            artist = song["artist"]
        elif "ar" in song and song["ar"]:
            artist = "/".join([a.get("name", "") for a in song["ar"] if a.get("name")])
        else:
            artist = "未知艺术家"
        if "album" in song:
            album = song["album"]
        elif "al" in song and song["al"]:
            album = song["al"].get("name", "未知专辑")
        else:
            album = "未知专辑"
        # 获取封面URL（优先参数，其次song中的cover/picUrl/al.picUrl）
        if not cover_url:
            if "cover" in song and song["cover"]:
                cover_url = song["cover"]
            elif "picUrl" in song and song["picUrl"]:
                cover_url = song["picUrl"]
            elif "al" in song and song["al"] and song["al"].get("picUrl"):
                cover_url = song["al"]["picUrl"]
        audio_bytes.seek(0)
        audio = MP3(audio_bytes)
        if audio.tags is None:
            audio.add_tags()
        audio.tags.add(TIT2(encoding=3, text=[name]))
        audio.tags.add(TPE1(encoding=3, text=[artist]))
        audio.tags.add(TALB(encoding=3, text=[album]))
        # 嵌入专辑封面
        if cover_url:
            try:
                import requests as _req
                _cover_resp = _req.get(cover_url, timeout=5, headers={"Referer": "https://music.163.com/"})
                if _cover_resp.status_code == 200 and _cover_resp.content:
                    mime = "image/jpeg" if cover_url.endswith(".jpg") or cover_url.endswith(".jpeg") else "image/png"
                    audio.tags.add(APIC(
                        encoding=3,
                        mime=mime,
                        type=3,  # 3 = front cover
                        desc="Cover",
                        data=_cover_resp.content
                    ))
                    logger.info(f"ID3封面嵌入成功: {name} ({len(_cover_resp.content)//1024}KB)")
            except Exception as cover_err:
                logger.warning(f"ID3封面嵌入失败 {name}: {cover_err}")
        audio_bytes.seek(0)
        audio.save(audio_bytes)
        audio_bytes.seek(0)
    except Exception as e:
        logger.warning(f"写入ID3标签失败: {e}")
        audio_bytes.seek(0)
    return audio_bytes


async def audio_proxy_handler(request):
    """
    音频代理端点：根据 song_id 从网易云下载MP3，写入ID3标签后返回。
    内联搜索通过此URL让 Telegram 直接拉取音频，无需先上传到管理员私聊。
    """
    # HEAD请求快速响应（Telegram验证URL时用HEAD，不下载音频）
    if request.method == "HEAD":
        return web.Response(
            status=200,
            headers={
                "Content-Type": "audio/mpeg",
                "Accept-Ranges": "bytes",
            },
        )

    song_id = request.match_info.get("song_id")
    name = request.query.get("name", "未知歌曲")
    artist = request.query.get("artist", "未知艺术家")
    album = request.query.get("album", "")
    quality = request.query.get("quality", db.get_quality())

    try:
        sid = int(song_id)
    except (ValueError, TypeError):
        return web.Response(status=400, text="Invalid song_id")

    try:
        # 直接从网易云下载音频（内联搜索专用，不使用其他代理）
        audio_content = None
        
        # 获取播放地址（3秒超时）
        def _get_url(level):
            url_result = api.get_song_url([sid], level=level)
            for item in url_result.get("data", []):
                if item.get("id") == sid and item.get("url"):
                    return item["url"]
            return None

        # 下载音频：每种音质只试1次，连接超时5秒，双音质备用，总时间≤10秒
        for try_quality in [quality, "higher"]:
            try:
                play_url = await asyncio.wait_for(
                    asyncio.to_thread(_get_url, try_quality), timeout=3
                )
            except asyncio.TimeoutError:
                logger.warning(f"代理端点 song_id={sid} 音质={try_quality} 获取地址超时")
                continue
            if not play_url:
                continue
            if play_url.startswith("http://"):
                play_url = "https://" + play_url[7:]
            # 每种音质重试1次，连接超时15秒，读取超时30秒，总超时45秒
            for _retry in range(2):
                try:
                    resp = await asyncio.wait_for(
                        asyncio.to_thread(
                            _download_session.get, play_url,
                            timeout=(15, 30),
                            headers={"Referer": "https://music.163.com/"}
                        ),
                        timeout=45
                    )
                    if resp.status_code == 200 and resp.content and len(resp.content) > 1000:
                        audio_content = resp.content
                        _retry_info = f" (第{_retry+1}次尝试)" if _retry > 0 else ""
                        logger.info(f"代理端点 song_id={sid} 音质={try_quality} 直接下载成功{_retry_info} 大小={len(audio_content)}bytes")
                        break
                    logger.warning(f"代理端点 song_id={sid} 音质={try_quality} 状态={resp.status_code} 大小={len(resp.content) if resp.content else 0}")
                except asyncio.TimeoutError:
                    logger.warning(f"代理端点 song_id={sid} 音质={try_quality} 下载超时 (第{_retry+1}次尝试)")
                    if _retry == 0:
                        await asyncio.sleep(1)
                        continue
                except Exception as e:
                    logger.warning(f"代理端点 song_id={sid} 音质={try_quality} 异常: {type(e).__name__}: {e}")
                    if _retry == 0:
                        await asyncio.sleep(1)
                        continue
                break  # 非超时异常或成功，跳出重试循环
            if audio_content:
                break

        if not audio_content:
            return web.Response(status=502, text="Audio download failed")
        audio_bytes = io.BytesIO(audio_content)

        # 写入ID3标签（含封面）
        cover_url = request.query.get("cover", "")
        song = {"id": sid, "name": name, "artist": artist, "album": album or name}
        if cover_url:
            song["picUrl"] = cover_url
        tagged = _tag_mp3(audio_bytes, song)
        tagged.seek(0)

        return web.Response(
            body=tagged.read(),
            content_type="audio/mpeg",
            headers={
                "Content-Disposition": f'inline; filename="{quote(name)}.mp3"',
                "Cache-Control": "public, max-age=3600",
            },
        )
    except Exception as e:
        logger.error(f"音频代理失败 song_id={song_id}: {e}")
        return web.Response(status=500, text=f"Proxy error: {e}")


# ============================================================
# 命令处理
# ============================================================

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    _register_user(user.id)
    user_label = f"{user.username or user.first_name or user.id}"
    logger.info(f"/start 用户={user_label}(id={user.id})")

    if _is_banned(user.id):
        await update.message.reply_text("⛔ 你已被管理员封禁。")
        return

    # 处理 deep link：/start play_歌曲ID  → 自动播放
    if context.args and context.args[0].startswith("play_"):
        try:
            song_id = int(context.args[0].split("_", 1)[1])
            await _play_song(update, context, song_id, edit=False)
        except (ValueError, IndexError):
            pass
        return

    # 自定义欢迎语优先（从Upstash读取），其次环境变量默认
    custom_welcome = db.get_welcome()
    if not custom_welcome:
        custom_welcome = config.DEFAULT_WELCOME

    if custom_welcome:
        welcome = custom_welcome.replace("{username}", user.first_name or "朋友")
        await update.message.reply_text(welcome, parse_mode="HTML")
        return

    # 无自定义欢迎语时，显示默认问候 + 帮助菜单
    help_menu = (
        "\n\n📖 <b>使用方法：</b>\n"
        "1️⃣ /play 歌曲名 — 搜索并播放歌曲\n"
        "2️⃣ /playlist 歌单ID/链接 — 播放网易云歌单（仅限私聊）\n"
        "3️⃣ 内联搜索：在任意聊天输入 <code>@XiOuDi163_bot 歌曲名</code>\n\n"
        "💡 示例：\n"
        "• /play 邓紫棋 泡沫\n"
        "• /playlist 3778678\n"
        "• @XiOuDi163_bot 邓紫棋 泡沫\n\n"
        "输入 /help 查看更多帮助"
    )

    text = (
        f"👋 你好，{user.first_name}！\n\n"
        "我是网易云音乐机器人，可以帮你搜索并播放音乐。"
        + help_menu
    )
    await update.message.reply_text(text, parse_mode="HTML")


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "📖 <b>帮助文档</b>\n\n"
        "🎵 <b>搜索与播放</b>\n"
        "• /play 关键词 — 搜索歌曲\n"
        "• /playlist 歌单ID/链接 — 播放网易云歌单（仅限私聊）\n"
        "• 内联模式：@XiOuDi163_bot 关键词 — 在任意对话中搜索分享\n\n"
        "🔧 <b>其他</b>\n"
        "• /start — 开始\n"
        "• /help — 显示此帮助\n\n"
        "👑 <b>管理员命令</b>\n"
        "• /admin — 管理员面板\n"
        "• /broadcast 消息 — 广播消息\n"
        "• /stats — 查看统计\n"
        "• /ban 用户ID — 封禁用户\n"
        "• /unban 用户ID — 解封用户"
    )
    await update.message.reply_text(text, parse_mode="HTML")


async def cmd_play(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/play 关键词 — 搜索歌曲（按钮选择播放）"""
    user = update.effective_user
    if _is_banned(user.id):
        await update.message.reply_text("⛔ 你已被管理员封禁。")
        return

    keyword = " ".join(context.args).strip()
    if not keyword:
        await update.message.reply_text("⚠️ 请输入搜索关键词，例如：/play 邓紫棋 泡沫")
        return

    _register_user(user.id)
    user_label = f"{user.username or user.first_name or user.id}"
    logger.info(f"/play 用户={user_label}(id={user.id}) 关键词='{keyword}'")
    await _do_search(update, context, keyword)


async def cmd_music(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/music 已弃用，仅提示用户使用 /play"""
    await update.message.reply_text(
        "📢 命令已更新！\n\n"
        "请使用 <b>/play</b> 搜索歌曲，例如：\n"
        "<code>/play 邓紫棋 泡沫</code>\n\n"
        "内联搜索：@XiOuDi163_bot 歌曲名",
        parse_mode="HTML"
    )


async def _do_search(update: Update, context: ContextTypes.DEFAULT_TYPE, keyword: str):
    """执行搜索并展示结果按钮（分页）"""
    status_msg = await update.message.reply_text(f"🔍 正在搜索「{keyword}」...")

    # 群组中只请求10条，私聊请求25条
    chat = update.effective_chat
    is_group = chat and chat.type in ("group", "supergroup")
    search_limit = 10 if is_group else 25

    try:
        songs = await asyncio.to_thread(api.search_songs_simple, keyword, search_limit)
    except Exception as e:
        logger.error(f"搜索失败: {e}")
        await status_msg.edit_text("❌ 搜索失败，请稍后重试。")
        return

    await asyncio.to_thread(db.incr_search)

    if not songs:
        await status_msg.edit_text(f"😢 没有找到与「{keyword}」相关的歌曲。")
        return

    # 记录搜索到的歌曲ID，供闲时自动缓存扩展使用
    for s in songs[:20]:
        try:
            await asyncio.to_thread(db.add_searched_song, s["id"])
        except Exception:
            pass

    # 存储搜索结果到user_data，供分页使用
    context.user_data["search_songs"] = songs
    context.user_data["search_keyword"] = keyword
    context.user_data["search_is_group"] = is_group

    await _render_search_page(update, context, 0, status_msg)


async def _render_search_page(update: Update, context: ContextTypes.DEFAULT_TYPE, page: int, status_msg=None):
    """渲染搜索结果的某一页"""
    songs = context.user_data.get("search_songs", [])
    keyword = context.user_data.get("search_keyword", "")
    is_group = context.user_data.get("search_is_group", False)
    # 群组和私聊都每页5条
    page_size = 5
    total = len(songs)
    total_pages = (total + page_size - 1) // page_size
    page = max(0, min(page, total_pages - 1))

    start = page * page_size
    end = min(start + page_size, total)
    page_songs = songs[start:end]

    # 构建歌曲按钮
    keyboard = []
    for i, song in enumerate(page_songs):
        idx = start + i + 1
        label = f"{idx}. {song['name']} - {song['artist']} ({_fmt_duration(song['duration'])})"
        keyboard.append([
            InlineKeyboardButton(label, callback_data=f"play:{song['id']}")
        ])

    # 分页导航按钮
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️ 上一页", callback_data=f"searchpage:{page-1}"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton("下一页 ➡️", callback_data=f"searchpage:{page+1}"))
    if nav:
        keyboard.append(nav)

    reply_markup = InlineKeyboardMarkup(keyboard)
    text = f"✅ 找到 {total} 首「{keyword}」（第 {page+1}/{total_pages} 页，点击播放）："

    if status_msg:
        await status_msg.edit_text(text, reply_markup=reply_markup)
    else:
        await update.callback_query.edit_message_text(text, reply_markup=reply_markup)


# ============================================================
# 歌单功能
# ============================================================

PLAYLIST_PAGE_SIZE = 10
PLAYLIST_MAX_SONGS = 10000  # 获取歌单完整列表（最多10000首，超过部分分批获取详情）


def _extract_playlist_id(text: str) -> int:
    """从歌单链接或纯数字中提取歌单ID"""
    text = text.strip()
    # 尝试从链接中提取 id=xxx
    m = re.search(r"[?&]id=(\d+)", text)
    if m:
        return int(m.group(1))
    # 纯数字
    if text.isdigit():
        return int(text)
    return 0


# ============================================================
# 歌单缓存优化（方案1：Upstash缓存，24小时过期）
# ============================================================
PLAYLIST_CACHE_TTL = 86400  # 24小时（秒）


def _get_cached_playlist(playlist_id: int) -> list:
    """从Upstash缓存读取歌单歌曲列表，未命中返回空列表"""
    try:
        key = f"playlist:{playlist_id}:songs"
        result = db._exec("GET", key)
        if result:
            import json
            songs = json.loads(result)
            logger.info(f"歌单缓存命中：playlist_id={playlist_id} 歌曲数={len(songs)}")
            return songs
    except Exception as e:
        logger.warning(f"歌单缓存读取失败：playlist_id={playlist_id} 错误={e}")
    return []


def _cache_playlist(playlist_id: int, songs: list):
    """将歌单歌曲列表缓存到Upstash，24小时过期"""
    try:
        key = f"playlist:{playlist_id}:songs"
        import json
        value = json.dumps(songs, ensure_ascii=False)
        db._exec("SET", key, value)
        db._exec("EXPIRE", key, PLAYLIST_CACHE_TTL)
        logger.info(f"歌单已缓存：playlist_id={playlist_id} 歌曲数={len(songs)} 过期时间=24小时")
    except Exception as e:
        logger.warning(f"歌单缓存写入失败：playlist_id={playlist_id} 错误={e}")


async def _load_playlist_songs(playlist_id: int, limit: int = 10000, use_cache: bool = True) -> list:
    """
    加载歌单歌曲列表（优化版）
    1. 优先从Upstash缓存读取（方案1）
    2. 缓存未命中时，使用异步并发获取（方案4）
    3. 获取成功后写入缓存
    """
    # 方案1：优先从缓存读取
    if use_cache:
        cached = _get_cached_playlist(playlist_id)
        if cached:
            return cached[:limit]

    # 方案4：异步并发获取歌曲详情
    logger.info(f"歌单缓存未命中，开始异步并发获取：playlist_id={playlist_id} limit={limit}")
    songs = await api.get_toplist_songs_async(playlist_id, limit=limit, max_concurrent=2)

    # 写入缓存
    if songs:
        _cache_playlist(playlist_id, songs)

    return songs


# ============================================================
# 懒加载优化（方案2：首次只加载前100首，后台异步加载剩余）
# ============================================================
PLAYLIST_LAZY_LOAD_THRESHOLD = 100  # 首次加载前100首
PLAYLIST_LAZY_LOAD_PRELOAD_AT = 80  # 播放到第80首时预加载下一批
playlist_lazy_loading = {}  # {playlist_id: "loading"/"done"/None}


async def _lazy_load_remaining(playlist_id: int, context, user_id: int, chat_id: int):
    """后台异步加载歌单剩余歌曲（懒加载）"""
    global playlist_lazy_loading
    if playlist_lazy_loading.get(playlist_id) == "loading":
        return  # 已经在加载中

    playlist_lazy_loading[playlist_id] = "loading"
    try:
        logger.info(f"歌单懒加载开始：playlist_id={playlist_id} 加载完整歌单")
        # 加载完整歌单（会自动缓存）
        all_songs = await _load_playlist_songs(playlist_id, limit=PLAYLIST_MAX_SONGS, use_cache=True)

        # 更新context.user_data中的歌单数据
        if hasattr(context, 'user_data'):
            context.user_data[f"playlist_{playlist_id}"] = all_songs

        # 更新正在播放的歌单状态（如果有）
        active = db.get_active_playlist(user_id)
        if active and active.get("playlist_id") == playlist_id:
            current_index = active.get("current_index", 0)
            db.save_active_playlist(user_id, playlist_id, all_songs, current_index)

        playlist_lazy_loading[playlist_id] = "done"
        logger.info(f"歌单懒加载完成：playlist_id={playlist_id} 总歌曲数={len(all_songs)}")

        # 通知用户（可选）
        try:
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"✅ 歌单 {playlist_id} 剩余歌曲已加载完成（共{len(all_songs)}首）"
            )
        except Exception:
            pass

    except Exception as e:
        playlist_lazy_loading[playlist_id] = None
        logger.error(f"歌单懒加载失败：playlist_id={playlist_id} 错误={e}")



async def cmd_playlist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/playlist 歌单ID或链接 — 显示歌单，选择列表播放或全部播放（仅限私聊）"""
    user = update.effective_user
    chat = update.effective_chat

    # 检查歌单播放功能是否启用
    if not db.is_playlist_enabled():
        await update.message.reply_text("⚠️ 歌单播放功能已被管理员禁用。\n\n请使用 /play 搜索歌曲，或使用内联搜索 @XiOuDi163_bot 歌曲名。")
        return

    # 仅限私聊使用
    if chat and chat.type != "private":
        await update.message.reply_text("⚠️ /playlist 命令仅在与 Bot 私聊中有效。\n\n请在私聊中使用此命令，或使用内联搜索 @XiOuDi163_bot 歌曲名 在群组中分享音乐。")
        return

    if _is_banned(user.id):
        await update.message.reply_text("⛔ 你已被管理员封禁。")
        return

    arg = " ".join(context.args).strip()
    if not arg:
        await update.message.reply_text("⚠️ 用法：/playlist 歌单ID 或 歌单链接")
        return

    playlist_id = _extract_playlist_id(arg)
    if not playlist_id:
        await update.message.reply_text("❌ 无法识别歌单ID，请输入数字ID或完整链接。")
        return

    _register_user(user.id)
    user_label = f"{user.username or user.first_name or user.id}"
    logger.info(f"/playlist 用户={user_label}(id={user.id}) 歌单ID={playlist_id}")
    status = await update.message.reply_text(f"🔍 正在获取歌单 {playlist_id} ...")

    try:
        # 先获取歌单详情，得到总歌曲数量
        playlist_detail = await asyncio.to_thread(api._post, "/weapi/v6/playlist/detail", {"id": playlist_id, "n": 10000, "s": 0})
        total_track_count = len(playlist_detail.get("playlist", {}).get("trackIds", []))
        playlist_name = playlist_detail.get("playlist", {}).get("name", str(playlist_id))
        logger.info(f"/playlist 歌单ID={playlist_id} 名称={playlist_name} 总歌曲数={total_track_count}")

        # 检查缓存是否完整（缓存歌曲数等于总歌曲数）
        full_cached = _get_cached_playlist(playlist_id)
        if full_cached and len(full_cached) >= total_track_count:
            songs = full_cached
            logger.info(f"/playlist 歌单ID={playlist_id} 从缓存读取全部{len(songs)}首（完整）")
        else:
            if full_cached:
                logger.info(f"/playlist 歌单ID={playlist_id} 缓存不完整（{len(full_cached)}/{total_track_count}），清除缓存重新加载")
                db._exec("DEL", f"playlist:{playlist_id}:songs")

            # 显示加载进度
            await status.edit_text(f"🔍 正在加载歌单《{playlist_name}》（共{total_track_count}首）...")

            # 加载全部歌曲（分批处理，每批500首）
            songs = await _load_playlist_songs(playlist_id, limit=10000, use_cache=True)
            logger.info(f"/playlist 歌单ID={playlist_id} 全部加载完成，共{len(songs)}首")
    except Exception as e:
        logger.error(f"获取歌单失败: {e}")
        await status.edit_text("❌ 获取歌单失败，请检查歌单ID是否正确。")
        return

    if not songs:
        await status.edit_text("😢 该歌单为空或无法访问。")
        return

    logger.info(f"/playlist 歌单ID={playlist_id} 共加载{len(songs)}首歌曲")

    # 存储歌单歌曲到context，供回调使用
    context.user_data[f"playlist_{playlist_id}"] = songs

    # 显示选择模式
    keyboard = [
        [InlineKeyboardButton("📋 列表播放（选歌）", callback_data=f"plist:{playlist_id}:0")],
        [InlineKeyboardButton("▶️ 全部播放（自动发送）", callback_data=f"pall:{playlist_id}")],
    ]

    await status.edit_text(
        f"📀 <b>歌单《{playlist_name}》</b>（共{len(songs)}首）\n\n请选择播放方式：",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="HTML",
    )


async def _show_playlist_page(update: Update, context, playlist_id: int, page: int):
    """分页显示歌单歌曲列表"""
    songs = context.user_data.get(f"playlist_{playlist_id}", [])
    if not songs:
        await update.callback_query.edit_message_text("❌ 歌单数据已过期，请重新输入 /playlist")
        return

    total = len(songs)
    total_pages = (total + PLAYLIST_PAGE_SIZE - 1) // PLAYLIST_PAGE_SIZE
    page = max(0, min(page, total_pages - 1))
    start = page * PLAYLIST_PAGE_SIZE
    end = min(start + PLAYLIST_PAGE_SIZE, total)
    page_songs = songs[start:end]

    keyboard = []
    for i, song in enumerate(page_songs, start + 1):
        label = f"{i}. {song['name']} - {song['artist']}"
        keyboard.append([InlineKeyboardButton(label, callback_data=f"play:{song['id']}")])

    # 翻页按钮
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️ 上一页", callback_data=f"plist:{playlist_id}:{page-1}"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton("下一页 ➡️", callback_data=f"plist:{playlist_id}:{page+1}"))
    if nav:
        keyboard.append(nav)
    keyboard.append([InlineKeyboardButton("🔙 返回选择", callback_data=f"pmenu:{playlist_id}")])

    await update.callback_query.edit_message_text(
        f"📀 歌单歌曲（第{page+1}/{total_pages}页，共{total}首）：",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def _play_playlist_all(update: Update, context, playlist_id: int):
    """全部播放：后台逐个发送歌单歌曲（状态持久化到Redis，重启后继续）"""
    songs = context.user_data.get(f"playlist_{playlist_id}", [])
    if not songs:
        await update.callback_query.edit_message_text("❌ 歌单数据已过期，请重新输入 /playlist")
        return

    chat_id = update.callback_query.message.chat_id
    user_id = update.callback_query.from_user.id

    # 排队机制：如果用户已有正在播放的歌单，将新歌单加入队列
    existing = db.get_active_playlist(user_id)
    if existing:
        global playlist_queue
        if user_id not in playlist_queue:
            playlist_queue[user_id] = []
        playlist_queue[user_id].append((playlist_id, songs))
        queue_pos = len(playlist_queue[user_id])
        current_pl = existing.get("playlist_id", "?")
        current_idx = existing.get("current_index", 0)
        current_total = existing.get("total", 0)
        await update.callback_query.edit_message_text(
            f"📋 歌单已加入播放队列！\n\n"
            f"当前正在播放：歌单 {current_pl}（进度 {current_idx}/{current_total}）\n"
            f"你的排队位置：第 {queue_pos} 个\n\n"
            f"前面的歌单播放完后会自动开始播放这个歌单。"
        )
        logger.info(f"歌单排队：用户={user_id} 新歌单={playlist_id}({len(songs)}首) 队列位置={queue_pos}")
        return

    # 分批次播放：超过1000首时提示用户
    BATCH_SIZE = 1000
    total_songs = len(songs)
    total_batches = (total_songs + BATCH_SIZE - 1) // BATCH_SIZE

    if total_songs > BATCH_SIZE:
        await update.callback_query.edit_message_text(
            f"▶️ 歌单共 {total_songs} 首，将分 {total_batches} 批次播放（每批 {BATCH_SIZE} 首）...\n\n"
            f"第 1/{total_batches} 批开始播放..."
        )
    else:
        await update.callback_query.edit_message_text(
            f"▶️ 开始全部播放 {total_songs} 首歌曲..."
        )

    # 保存播放状态到Redis（从第0首开始）
    db.save_active_playlist(user_id, playlist_id, songs, 0)
    logger.info(f"歌单播放：用户={user_id} 歌单={playlist_id} 共{total_songs}首，{total_batches}批次，开始播放（状态已持久化）")

    async def _send_all():
        success = 0
        failed = 0
        current_batch = 1
        for idx, song in enumerate(songs, 1):
            # 检查管理员停止标志
            if db.check_playlist_stop_flag(user_id):
                logger.info(f"歌单播放：用户={user_id} 被管理员停止，已播放{idx-1}首")
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=f"⏹️ 歌单播放已被管理员停止。已播放{idx-1}首（成功{success}，失败{failed}）。"
                )
                db.remove_active_playlist(user_id)
                return
            # 中等优先级：最近3秒有用户活动则暂停（比缓存排行榜高，比用户单曲低）
            # 高优先级：有用户正在搜索播放时暂停（内联搜索 > 普通搜索 > 歌单播放）
            while time.time() - last_user_activity < 3 or active_search_plays:
                await asyncio.sleep(2)
                # 暂停时也检查停止标志
                if db.check_playlist_stop_flag(user_id):
                    logger.info(f"歌单播放：用户={user_id} 被管理员停止（暂停中），已播放{idx-1}首")
                    await context.bot.send_message(
                        chat_id=chat_id,
                        text=f"⏹️ 歌单播放已被管理员停止。已播放{idx-1}首（成功{success}，失败{failed}）。"
                    )
                    db.remove_active_playlist(user_id)
                    return
            try:
                # 5秒去重：同一用户5秒内不能发送相同歌曲
                global playlist_sent_songs
                now = time.time()
                if user_id not in playlist_sent_songs:
                    playlist_sent_songs[user_id] = {}
                last_sent = playlist_sent_songs[user_id].get(song["id"], 0)
                if now - last_sent < 5:
                    logger.info(f"歌单播放去重：用户={user_id} 歌曲={song['name']}({song['id']}) 5秒内已发送，跳过")
                    db.update_playlist_index(user_id, idx)
                    continue

                caption = _song_caption(song)
                cached = db.get_file_id(song["id"])
                if cached:
                    try:
                        msg = await context.bot.send_audio(
                            chat_id=chat_id, audio=cached, caption=caption, parse_mode="HTML"
                        )
                        # 检查缓存音频标题是否正确
                        _cached_title = getattr(msg.audio, 'title', '') if msg and msg.audio else ''
                        if _is_wrong_audio_title(_cached_title, song["name"], song["id"]):
                            logger.warning(f"歌单播放 file_id缓存标题不正确: 缓存标题='{_cached_title}' 正确标题='{song['name']}'，删除消息并清除缓存")
                            try:
                                await context.bot.delete_message(chat_id=chat_id, message_id=msg.message_id)
                            except Exception:
                                pass
                            db.delete_file_id(song["id"])
                            # 不continue，继续使用代理重新发送
                        else:
                            success += 1
                            db.update_playlist_index(user_id, idx)
                            await asyncio.sleep(1)
                            continue
                    except Exception as e:
                        logger.warning(f"歌单播放 file_id缓存失败: {song['name']} - {e}")
                        db.delete_file_id(song["id"])
                
                # 歌单播放：CF代理 → 网易云直链 二回退机制
                from urllib.parse import quote
                
                # 先获取网易云直链（CF代理需要通过/proxy?url=xxx转发）
                direct_url = api.get_first_song_url(song["id"], level=db.get_quality())
                cf_proxy_url = f"{config.CF_PROXY_URL}/proxy?url={quote(direct_url)}" if (config.CF_PROXY_URL and direct_url) else ""
                
                sent = False
                # 回退1：CF代理（通过/proxy转发网易云直链）
                if cf_proxy_url:
                    try:
                        logger.info(f"歌单播放 🌐 使用CF代理: {song['name']} - {song['artist']}")
                        msg = await context.bot.send_audio(
                            chat_id=chat_id,
                            audio=cf_proxy_url,
                            filename=f"{song['name']}.mp3",
                            title=song["name"],
                            performer=song["artist"],
                            caption=caption,
                            parse_mode="HTML",
                            duration=song["duration"] // 1000 if song["duration"] else None,
                        )
                        if msg and msg.audio and msg.audio.file_id:
                            db.set_file_id(song["id"], msg.audio.file_id)
                        sent = True
                    except Exception as e:
                        logger.warning(f"歌单播放 CF代理失败，尝试直链: {song['name']} - {e}")
                
                # 回退2：网易云直链
                if not sent and direct_url:
                    try:
                        logger.info(f"歌单播放 🔗 使用网易云直链: {song['name']} - {song['artist']}")
                        msg = await context.bot.send_audio(
                            chat_id=chat_id,
                            audio=direct_url,
                            filename=f"{song['name']}.mp3",
                            title=song["name"],
                            performer=song["artist"],
                            caption=caption,
                            parse_mode="HTML",
                            duration=song["duration"] // 1000 if song["duration"] else None,
                        )
                        if msg and msg.audio and msg.audio.file_id:
                            db.set_file_id(song["id"], msg.audio.file_id)
                        sent = True
                    except Exception as e:
                        logger.warning(f"歌单播放 直链失败: {song['name']} - {e}")
                
                if sent:
                    success += 1
                else:
                    logger.warning(f"歌单播放 所有回退都失败: {song['name']}")
                    failed += 1
                # 更新播放进度到Redis（每首完成后更新）
                db.update_playlist_index(user_id, idx)
            except Exception as e:
                logger.warning(f"歌单全部播放失败 {song['name']}: {e}")
                failed += 1
                db.update_playlist_index(user_id, idx)
            await asyncio.sleep(1)  # 中等优先级，间隔1秒

            # 分批次进度提示：每完成一批（1000首）发送进度
            if total_songs > BATCH_SIZE and idx % BATCH_SIZE == 0:
                current_batch = idx // BATCH_SIZE
                remaining = total_songs - idx
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=f"📊 第 {current_batch}/{total_batches} 批播放完成！\n"
                         f"已播放 {idx}/{total_songs} 首（成功{success}，失败{failed}）\n"
                         f"剩余 {remaining} 首，3秒后继续下一批..."
                )
                await asyncio.sleep(3)  # 批次间短暂暂停
                if current_batch < total_batches:
                    await context.bot.send_message(
                        chat_id=chat_id,
                        text=f"▶️ 第 {current_batch + 1}/{total_batches} 批开始播放..."
                    )

        # 播放完成，移除状态
        db.remove_active_playlist(user_id)
        logger.info(f"歌单播放完成：用户={user_id} 歌单={playlist_id} 成功{success}首，失败{failed}首")
        if total_songs > BATCH_SIZE:
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"✅ 歌单全部播放完成！共 {total_batches} 批，成功{success}首，失败{failed}首。"
            )
        else:
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"✅ 歌单播放完成！成功{success}首，失败{failed}首。"
            )

        # 检查播放队列，自动播放下一个歌单
        global playlist_queue
        if user_id in playlist_queue and playlist_queue[user_id]:
            next_playlist_id, next_songs = playlist_queue[user_id].pop(0)
            if not playlist_queue[user_id]:
                del playlist_queue[user_id]
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"📋 队列中下一个歌单开始播放：歌单 {next_playlist_id}（共{len(next_songs)}首）"
            )
            logger.info(f"歌单队列播放：用户={user_id} 下一个歌单={next_playlist_id}({len(next_songs)}首)")
            # 递归播放下一个歌单
            await _play_playlist_all_queue(context, chat_id, user_id, next_playlist_id, next_songs)

    asyncio.create_task(_send_all())


async def _play_playlist_all_queue(context, chat_id: int, user_id: int, playlist_id: int, songs: list):
    """队列播放：自动播放队列中的下一个歌单（无需callback_query）"""
    BATCH_SIZE = 1000
    total_songs = len(songs)
    total_batches = (total_songs + BATCH_SIZE - 1) // BATCH_SIZE

    # 保存播放状态
    db.save_active_playlist(user_id, playlist_id, songs, 0)
    logger.info(f"歌单队列播放：用户={user_id} 歌单={playlist_id} 共{total_songs}首，{total_batches}批次，开始播放")

    async def _send_queue():
        global playlist_queue, playlist_sent_songs
        success = 0
        failed = 0
        for idx, song in enumerate(songs, 1):
            # 检查管理员停止标志
            if db.check_playlist_stop_flag(user_id):
                logger.info(f"歌单队列播放：用户={user_id} 被管理员停止，已播放{idx-1}首")
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=f"⏹️ 歌单播放已被管理员停止。已播放{idx-1}首（成功{success}，失败{failed}）。"
                )
                db.remove_active_playlist(user_id)
                # 清空队列
                if user_id in playlist_queue:
                    del playlist_queue[user_id]
                return
            # 优先级控制
            while time.time() - last_user_activity < 3 or active_search_plays:
                await asyncio.sleep(2)
                if db.check_playlist_stop_flag(user_id):
                    logger.info(f"歌单队列播放：用户={user_id} 被管理员停止（暂停中），已播放{idx-1}首")
                    await context.bot.send_message(
                        chat_id=chat_id,
                        text=f"⏹️ 歌单播放已被管理员停止。已播放{idx-1}首（成功{success}，失败{failed}）。"
                    )
                    db.remove_active_playlist(user_id)
                    if user_id in playlist_queue:
                        del playlist_queue[user_id]
                    return
            try:
                # 5秒去重
                now = time.time()
                if user_id not in playlist_sent_songs:
                    playlist_sent_songs[user_id] = {}
                last_sent = playlist_sent_songs[user_id].get(song["id"], 0)
                if now - last_sent < 5:
                    logger.info(f"歌单队列去重：用户={user_id} 歌曲={song['name']}({song['id']}) 5秒内已发送，跳过")
                    db.update_playlist_index(user_id, idx)
                    continue

                caption = _song_caption(song)
                success_flag, file_id, proxy_type = await _send_audio_with_fallback(
                    context, chat_id, song,
                    quality=db.get_quality(),
                    caption=caption,
                    log_prefix=f"歌单队列播放 [{idx}/{len(songs)}] "
                )
                playlist_sent_songs[user_id][song["id"]] = time.time()
                if success_flag:
                    success += 1
                else:
                    failed += 1
                db.update_playlist_index(user_id, idx)
            except Exception as e:
                logger.warning(f"歌单队列播放失败 {song['name']}: {e}")
                failed += 1
                db.update_playlist_index(user_id, idx)
            await asyncio.sleep(1)

            # 分批次进度提示
            if total_songs > BATCH_SIZE and idx % BATCH_SIZE == 0:
                current_batch = idx // BATCH_SIZE
                remaining = total_songs - idx
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=f"📊 第 {current_batch}/{total_batches} 批播放完成！\n"
                         f"已播放 {idx}/{total_songs} 首（成功{success}，失败{failed}）\n"
                         f"剩余 {remaining} 首，3秒后继续下一批..."
                )
                await asyncio.sleep(3)
                if current_batch < total_batches:
                    await context.bot.send_message(
                        chat_id=chat_id,
                        text=f"▶️ 第 {current_batch + 1}/{total_batches} 批开始播放..."
                    )

        # 播放完成
        db.remove_active_playlist(user_id)
        logger.info(f"歌单队列播放完成：用户={user_id} 歌单={playlist_id} 成功{success}首，失败{failed}首")
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"✅ 歌单播放完成！成功{success}首，失败{failed}首。"
        )

        # 继续播放队列中的下一个
        if user_id in playlist_queue and playlist_queue[user_id]:
            next_playlist_id, next_songs = playlist_queue[user_id].pop(0)
            if not playlist_queue[user_id]:
                del playlist_queue[user_id]
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"📋 队列中下一个歌单开始播放：歌单 {next_playlist_id}（共{len(next_songs)}首）"
            )
            await _play_playlist_all_queue(context, chat_id, user_id, next_playlist_id, next_songs)

    asyncio.create_task(_send_queue())


async def _resume_playlist_play(application, user_id: int, playlist_id: int, songs: list, start_index: int, total: int):
    """断点续播：从指定进度继续播放歌单（服务重启后恢复）"""
    # 获取application的bot对象
    try:
        bot = application.bot
    except Exception as e:
        logger.error(f"歌单续播：无法获取bot对象 用户={user_id}: {e}")
        db.remove_active_playlist(user_id)
        return

    chat_id = user_id
    success = 0
    failed = 0

    for idx_offset, song in enumerate(songs):
        idx = start_index + idx_offset + 1  # 当前是第几首（从1开始）
        # 检查管理员停止标志
        if db.check_playlist_stop_flag(user_id):
            logger.info(f"歌单续播：用户={user_id} 被管理员停止，已播放{idx-1}首")
            try:
                await bot.send_message(
                    chat_id=chat_id,
                    text=f"⏹️ 歌单播放已被管理员停止。已播放{idx-1}首（成功{success}，失败{failed}）。"
                )
            except Exception:
                pass
            db.remove_active_playlist(user_id)
            return
        # 中等优先级：最近3秒有用户活动则暂停
        while time.time() - last_user_activity < 3:
            await asyncio.sleep(2)
            if db.check_playlist_stop_flag(user_id):
                logger.info(f"歌单续播：用户={user_id} 被管理员停止（暂停中），已播放{idx-1}首")
                try:
                    await bot.send_message(
                        chat_id=chat_id,
                        text=f"⏹️ 歌单播放已被管理员停止。已播放{idx-1}首（成功{success}，失败{failed}）。"
                    )
                except Exception:
                    pass
                db.remove_active_playlist(user_id)
                return
        try:
            # 5秒去重：同一用户5秒内不能发送相同歌曲
            global playlist_sent_songs
            now = time.time()
            if user_id not in playlist_sent_songs:
                playlist_sent_songs[user_id] = {}
            last_sent = playlist_sent_songs[user_id].get(song["id"], 0)
            if now - last_sent < 5:
                logger.info(f"歌单续播去重：用户={user_id} 歌曲={song['name']}({song['id']}) 5秒内已发送，跳过")
                db.update_playlist_index(user_id, idx)
                continue

            cached = db.get_file_id(song["id"])
            caption = _song_caption(song)
            if cached:
                try:
                    msg = await bot.send_audio(
                        chat_id=chat_id, audio=cached, caption=caption, parse_mode="HTML"
                    )
                    # 检查缓存音频标题是否正确
                    _cached_title = getattr(msg.audio, 'title', '') if msg and msg.audio else ''
                    if _is_wrong_audio_title(_cached_title, song["name"], song["id"]):
                        logger.warning(f"歌单续播 file_id缓存标题不正确: 缓存标题='{_cached_title}' 正确标题='{song['name']}'，删除消息并清除缓存")
                        try:
                            await bot.delete_message(chat_id=chat_id, message_id=msg.message_id)
                        except Exception:
                            pass
                        db.delete_file_id(song["id"])
                        # 不continue，继续使用代理重新发送
                    else:
                        success += 1
                        db.update_playlist_index(user_id, idx)
                        await asyncio.sleep(1)
                        continue
                except Exception as e:
                    logger.warning(f"歌单续播 file_id缓存失败: {song['name']} - {e}")
                    db.delete_file_id(song["id"])
            
            # 歌单播放：CF代理 → 网易云直链 二回退机制
            from urllib.parse import quote
            
            # 先获取网易云直链（CF代理需要通过/proxy?url=xxx转发）
            direct_url = api.get_first_song_url(song["id"], level=db.get_quality())
            cf_proxy_url = f"{config.CF_PROXY_URL}/proxy?url={quote(direct_url)}" if (config.CF_PROXY_URL and direct_url) else ""
            
            sent = False
            # 回退1：CF代理（通过/proxy转发网易云直链）
            if cf_proxy_url:
                try:
                    logger.info(f"歌单续播 🌐 使用CF代理: {song['name']} - {song['artist']}")
                    msg = await bot.send_audio(
                        chat_id=chat_id,
                        audio=cf_proxy_url,
                        filename=f"{song['name']}.mp3",
                        title=song["name"],
                        performer=song["artist"],
                        caption=caption,
                        parse_mode="HTML",
                        duration=song["duration"] // 1000 if song["duration"] else None,
                    )
                    if msg and msg.audio and msg.audio.file_id:
                        db.set_file_id(song["id"], msg.audio.file_id)
                    sent = True
                except Exception as e:
                    logger.warning(f"歌单续播 CF代理失败，尝试直链: {song['name']} - {e}")
            
            # 回退2：网易云直链
            if not sent and direct_url:
                try:
                    logger.info(f"歌单续播 🔗 使用网易云直链: {song['name']} - {song['artist']}")
                    msg = await bot.send_audio(
                        chat_id=chat_id,
                        audio=direct_url,
                        filename=f"{song['name']}.mp3",
                        title=song["name"],
                        performer=song["artist"],
                        caption=caption,
                        parse_mode="HTML",
                        duration=song["duration"] // 1000 if song["duration"] else None,
                    )
                    if msg and msg.audio and msg.audio.file_id:
                        db.set_file_id(song["id"], msg.audio.file_id)
                    sent = True
                except Exception as e:
                    logger.warning(f"歌单续播 直链失败: {song['name']} - {e}")
            
            if sent:
                success += 1
            else:
                logger.warning(f"歌单续播 所有回退都失败: {song['name']}")
                failed += 1
            db.update_playlist_index(user_id, idx)
        except Exception as e:
            logger.warning(f"歌单续播失败 {song['name']}: {e}")
            failed += 1
            db.update_playlist_index(user_id, idx)
        await asyncio.sleep(1)

    # 播放完成
    db.remove_active_playlist(user_id)
    logger.info(f"歌单续播完成：用户={user_id} 歌单={playlist_id} 成功{success}首，失败{failed}首")
    try:
        await bot.send_message(
            chat_id=chat_id,
            text=f"✅ 歌单播放完成！成功{success}首，失败{failed}首。"
        )
    except Exception:
        pass


# ============================================================
# 回调查询（按钮点击）
# ============================================================

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user = query.from_user
    if _is_banned(user.id):
        await query.edit_message_text("⛔ 你已被管理员封禁。")
        return

    data = query.data
    if data.startswith("play:"):
        song_id = int(data.split(":", 1)[1])
        await _play_song(update, context, song_id, edit=True)
    elif data.startswith("searchpage:"):
        page = int(data.split(":", 1)[1])
        await _render_search_page(update, context, page)
    elif data.startswith("lyric:"):
        song_id = int(data.split(":", 1)[1])
        await _send_lyrics(update, context, song_id)
    elif data.startswith("plist:"):
        # 歌单列表分页
        parts = data.split(":")
        pid = int(parts[1])
        page = int(parts[2]) if len(parts) > 2 else 0
        await _show_playlist_page(update, context, pid, page)
    elif data.startswith("pall:"):
        # 歌单全部播放
        pid = int(data.split(":", 1)[1])
        await _play_playlist_all(update, context, pid)
    elif data.startswith("pmenu:"):
        # 返回歌单选择菜单
        pid = int(data.split(":", 1)[1])
        songs = context.user_data.get(f"playlist_{pid}", [])
        keyboard = [
            [InlineKeyboardButton("📋 列表播放（选歌）", callback_data=f"plist:{pid}:0")],
            [InlineKeyboardButton("▶️ 全部播放（自动发送）", callback_data=f"pall:{pid}")],
        ]
        await query.edit_message_text(
            f"📀 <b>歌单</b>（共{len(songs)}首）\n\n请选择播放方式：",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="HTML",
        )
    elif data.startswith("stoplist:"):
        # 停止用户歌单播放（管理员可停止任意用户，普通用户只能停止自己）
        target_uid = int(data.split(":", 1)[1])
        if user.id != target_uid and not _is_admin(user.id):
            await query.answer("⛔ 你只能停止自己的歌单", show_alert=True)
            return
        db.set_playlist_stop_flag(target_uid)
        # 如果是管理员停止其他用户，通知被停止的用户
        if user.id != target_uid:
            try:
                await context.bot.send_message(target_uid, "⏹️ 您的歌单播放已被管理员停止。")
            except Exception:
                pass
        await query.answer("✅ 已发送停止指令", show_alert=True)
        await query.edit_message_text(f"✅ 已停止用户 {target_uid} 的歌单播放。")
    elif data == "cache_now":
        # 管理员立即缓存
        if not _is_admin(user.id):
            await query.answer("⛔ 权限不足", show_alert=True)
            return
        if auto_cache_running:
            await query.answer("🔄 正在缓存中，请稍候...", show_alert=True)
            return
        if _do_auto_cache_func:
            # 将last_user_activity设为15秒前，避免按钮点击自身触发优先级暂停
            global last_user_activity
            last_user_activity = time.time() - 15
            asyncio.create_task(_do_auto_cache_func())
            await query.answer("⚡ 立即缓存已启动！", show_alert=True)
            await query.edit_message_text("⚡ 立即缓存已启动！\n\n正在缓存今日排行榜，有用户活动时自动暂停。")
        else:
            await query.answer("⚠️ 缓存功能未就绪", show_alert=True)
    elif data == "cache_status_refresh":
        # 刷新缓存状态
        if not _is_admin(user.id):
            await query.answer("⛔ 权限不足", show_alert=True)
            return
        try:
            cached_count = db.count_file_ids()
        except Exception:
            cached_count = "未知"
        idle_time = int(time.time() - last_user_activity) if last_user_activity else "从未"
        running = "🔄 正在缓存中" if auto_cache_running else "⏸️ 未在缓存"
        enabled = "✅ 已开启" if auto_cache_enabled else "❌ 已关闭"
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("⚡ 立即缓存", callback_data="cache_now"),
            InlineKeyboardButton("🔄 刷新状态", callback_data="cache_status_refresh"),
        ]])
        await query.edit_message_text(
            f"📊 缓存状态\n\n"
            f"♻️ 自动缓存：{enabled}\n"
            f"🔄 当前状态：{running}\n"
            f"📚 曲库榜单：{len(AUTO_CACHE_PLAYLISTS)} 个\n"
            f"💾 已缓存歌曲：{cached_count} 首\n"
            f"⏱️ 距上次用户活动：{idle_time}秒\n"
            f"📋 闲时阈值：{AUTO_CACHE_IDLE_THRESHOLD}秒（5分钟）",
            reply_markup=keyboard,
        )


async def _play_song(update: Update, context: ContextTypes.DEFAULT_TYPE, song_id: int, edit: bool = False):
    """获取歌曲信息，下载音频并发送（私聊带歌词按钮，群组带在bot中播放按钮，支持话题群组）"""
    user = update.effective_user
    user_label = f"{user.username or user.first_name or user.id}"
    logger.info(f"播放歌曲 用户={user_label}(id={user.id}) song_id={song_id}")

    # 5秒去重：同一用户5秒内不能发送相同歌曲
    global playlist_sent_songs, active_search_plays
    now = time.time()
    if user.id not in playlist_sent_songs:
        playlist_sent_songs[user.id] = {}
    last_sent = playlist_sent_songs[user.id].get(song_id, 0)
    if now - last_sent < 5:
        logger.info(f"播放歌曲去重：用户={user.id} 歌曲={song_id} 5秒内已发送，跳过")
        if edit:
            try:
                await update.callback_query.answer("⏳ 请稍候，5秒内不要重复发送相同歌曲")
            except Exception:
                pass
        active_search_plays.discard(user.id)
        return

    # 标记搜索播放活动（优先级：搜索播放 > 歌单播放）
    active_search_plays.add(user.id)

    # 获取 chat_id 和 message_thread_id（支持话题群组）
    if edit:
        chat_id = update.callback_query.message.chat_id
        message_thread_id = getattr(update.callback_query.message, 'message_thread_id', None)
    else:
        chat_id = update.message.chat_id
        message_thread_id = getattr(update.message, 'message_thread_id', None)

    # 获取歌曲详情
    try:
        detail = api.get_song_detail([song_id])
        songs_detail = detail.get("songs", [])
        if not songs_detail:
            if edit:
                await update.callback_query.edit_message_text("❌ 未找到该歌曲信息。")
            else:
                await update.message.reply_text("❌ 未找到该歌曲信息。")
            active_search_plays.discard(user.id)
            return

        sd = songs_detail[0]
        song = {
            "id": sd["id"],
            "name": sd["name"],
            "artist": "/".join(a["name"] for a in sd.get("ar", [])),
            "album": sd.get("al", {}).get("name", ""),
            "cover": sd.get("al", {}).get("picUrl", ""),
            "duration": sd.get("dt", 0),
        }
    except Exception as e:
        logger.error(f"获取歌曲详情失败: {e}")
        if edit:
            await update.callback_query.edit_message_text("❌ 获取歌曲信息失败。")
        active_search_plays.discard(user.id)
        return

    # 获取播放地址
    try:
        url = api.get_first_song_url(song_id, level=config.MUSIC_QUALITY)
    except Exception as e:
        logger.error(f"获取播放地址失败: {e}")
        url = ""

    if not url:
        msg = f"❌ 无法获取播放地址，该歌曲可能需要VIP或已下架。\n\n{_song_caption(song)}"
        if edit:
            await update.callback_query.edit_message_text(msg, parse_mode="HTML")
        else:
            await update.message.reply_text(msg, parse_mode="HTML")
        active_search_plays.discard(user.id)
        return

    db.incr_play()

    caption = _song_caption(song)

    # 构建按钮：私聊显示歌词按钮，群组显示在bot中播放按钮
    chat = update.effective_chat
    reply_markup = None
    bot_uname = context.bot.username or "XiOuDi163_bot"
    if chat and chat.type == "private":
        reply_markup = InlineKeyboardMarkup([[
            InlineKeyboardButton("📝 获取歌词", callback_data=f"lyric:{song_id}")
        ]])
    else:
        # 群组/超级群组：显示在bot中播放按钮（deep link）
        reply_markup = InlineKeyboardMarkup([[
            InlineKeyboardButton("🎵 在bot中播放", url=f"https://t.me/{bot_uname}?start=play_{song_id}")
        ]])

    # 检查 file_id 缓存，命中则直接转发（零带宽、秒发）
    cached_file_id = await asyncio.to_thread(db.get_file_id, song_id)
    if cached_file_id:
        try:
            logger.info(f"播放歌曲 📦 使用file_id缓存: {song['name']} - {song['artist']} (用户={user_label})")
            msg = await context.bot.send_audio(
                chat_id=chat_id,
                message_thread_id=message_thread_id,
                audio=cached_file_id,
                title=song["name"],
                performer=song["artist"],
                caption=caption,
                parse_mode="HTML",
                duration=song["duration"] // 1000 if song.get("duration") else None,
                reply_markup=reply_markup,
            )
            # 检查缓存音频标题是否正确（修复历史缓存标题为数字ID或长字母的问题）
            _cached_title = getattr(msg.audio, 'title', '') if msg and msg.audio else ''
            if _is_wrong_audio_title(_cached_title, song["name"], song_id):
                logger.warning(f"file_id缓存标题不正确: 缓存标题='{_cached_title}' 正确标题='{song['name']}'，删除消息并清除缓存重新上传")
                # 删除刚才发送的错误标题消息
                try:
                    await context.bot.delete_message(chat_id=chat_id, message_id=msg.message_id)
                except Exception as del_e:
                    logger.warning(f"删除错误标题消息失败: {del_e}")
                # 清除缓存
                await asyncio.to_thread(db.delete_file_id, song_id)
                # 不return，继续执行后面的代理/下载逻辑重新上传
            else:
                # 标题正确，记录发送时间戳并返回
                playlist_sent_songs[user.id][song_id] = time.time()
                if edit:
                    await update.callback_query.delete_message()
                active_search_plays.discard(user.id)
                return
        except Exception as e:
            logger.warning(f"file_id缓存发送失败，回退代理: {e}")

    # 3级回退：CF代理 → 网易云直链 → Render下载
    # 每一步发送成功后检查音频文件名是否正确，不正确则删除消息继续下一个回退
    from urllib.parse import quote
    
    # 构建回退列表（按用户要求的顺序）
    fallback_list = []
    
    # 1. CF 代理（优先）- 通过/proxy?url=xxx转发网易云直链
    if config.CF_PROXY_URL and url:
        cf_url = f"{config.CF_PROXY_URL.rstrip('/')}/proxy?url={quote(url)}"
        fallback_list.append(("CF代理", cf_url))
    
    # 2. 网易云直链（Telegram自行下载，零Render流量）
    if url:
        fallback_list.append(("网易云直链", url))
    
    # 尝试每个回退
    for proxy_type, proxy_url in fallback_list:
        try:
            logger.info(f"播放歌曲 🌐 使用{proxy_type}: {song['name']} - {song['artist']} (用户={user_label}) -> {proxy_url[:80]}...")
            msg = await context.bot.send_audio(
                chat_id=chat_id,
                message_thread_id=message_thread_id,
                audio=proxy_url,
                filename=f"{song['name']}.mp3",
                title=song["name"],
                performer=song["artist"],
                caption=caption,
                parse_mode="HTML",
                thumbnail=song["cover"] if song["cover"] else None,
                duration=song["duration"] // 1000 if song["duration"] else None,
                reply_markup=reply_markup,
            )
            
            # 检查音频文件名称是否正确（修复数字ID文件名问题）
            actual_filename = (msg.audio.file_name or "") if msg and msg.audio else ""
            actual_title = (msg.audio.title or "") if msg and msg.audio else ""
            
            # 文件名检查：不为空、包含歌曲名称、不是纯数字ID、不以song_数字ID开头
            expected_name_clean = song["name"].replace(" ", "").lower()
            actual_filename_clean = actual_filename.replace(" ", "").lower()
            filename_contains_name = expected_name_clean in actual_filename_clean
            filename_is_digit = actual_filename.replace(".mp3", "").replace(".m4a", "").isdigit()
            filename_starts_song_id = actual_filename.startswith(f"song_{song_id}")
            filename_ok = bool(actual_filename) and filename_contains_name and not filename_is_digit and not filename_starts_song_id
            
            # 标题检查：不为空、不是纯数字、不等于song_id、与歌曲名匹配
            title_ok = bool(actual_title) and not actual_title.isdigit() and actual_title != str(song_id) and actual_title == song["name"]
            
            if not filename_ok or not title_ok:
                logger.warning(f"播放歌曲 ⚠️ {proxy_type}文件名/标题不正确: 文件名='{actual_filename}' 标题='{actual_title}' (包含歌名={filename_contains_name} 纯数字={filename_is_digit})，删除消息继续下一个回退")
                try:
                    await context.bot.delete_message(chat_id=chat_id, message_id=msg.message_id)
                except Exception as del_e:
                    logger.warning(f"删除文件名不正确的消息失败: {del_e}")
                continue
            
            # 文件名和标题都正确，保存 file_id 并返回
            if edit:
                await update.callback_query.delete_message()
            if msg and msg.audio and msg.audio.file_id:
                await asyncio.to_thread(db.set_file_id, song_id, msg.audio.file_id)
            # 记录发送时间戳（5秒去重）
            playlist_sent_songs[user.id][song_id] = time.time()
            logger.info(f"播放歌曲 ✅ {proxy_type}成功: {song['name']} - {song['artist']} (文件名='{actual_filename}' 标题='{actual_title}')")
            active_search_plays.discard(user.id)
            return
        except Exception as e:
            logger.warning(f"播放歌曲 ❌ {proxy_type}失败: {song['name']} - {e}")
            continue
    
    # 3. 所有回退失败，使用 Render 下载+上传（使用优化的下载模块）
    logger.info(f"播放歌曲 🖥️ 使用Render下载（优化版）: {song['name']} - {song['artist']} (用户={user_label})")
    if not url:
        err_msg = f"❌ 无法获取播放地址，该歌曲可能需要VIP或已下架。\n\n{_song_caption(song)}"
        if edit:
            await update.callback_query.edit_message_text(err_msg, parse_mode="HTML")
        else:
            await update.message.reply_text(err_msg, parse_mode="HTML")
        active_search_plays.discard(user.id)
        return
    try:
        # 使用优化的下载模块（并发限制+自动重试+超时+MD5校验）
        from downloader import download_audio
        result = await download_audio(
            url,
            timeout=60,
            max_retries=3,
            log_prefix=f"[播放 {song['name']}] "
        )
        
        if not result.success:
            err_msg = f"❌ 音频下载失败: {result.error}\n\n{_song_caption(song)}"
            if edit:
                await update.callback_query.edit_message_text(err_msg, parse_mode="HTML")
            else:
                await update.message.reply_text(err_msg, parse_mode="HTML")
            active_search_plays.discard(user.id)
            return
        
        audio_bytes = io.BytesIO(result.content)
        audio_bytes = _tag_mp3(audio_bytes, song)
        msg = await context.bot.send_audio(
            chat_id=chat_id,
            message_thread_id=message_thread_id,
            audio=audio_bytes,
            filename=f"{song['name']}.mp3",
            title=song["name"],
            performer=song["artist"],
            caption=caption,
            parse_mode="HTML",
            thumbnail=song["cover"] if song["cover"] else None,
            duration=song["duration"] // 1000 if song["duration"] else None,
            reply_markup=reply_markup,
        )
        if edit:
            await update.callback_query.delete_message()
        if msg and msg.audio and msg.audio.file_id:
            await asyncio.to_thread(db.set_file_id, song_id, msg.audio.file_id)
        # 记录发送时间戳（5秒去重）
        playlist_sent_songs[user.id][song_id] = time.time()
        active_search_plays.discard(user.id)
        return
    except Exception as e:
        logger.error(f"Render下载发送失败: {e}")
        err_msg = f"❌ 音频发送失败: {e}\n\n{_song_caption(song)}"
        if edit:
            await update.callback_query.edit_message_text(err_msg, parse_mode="HTML")
        else:
            await update.message.reply_text(err_msg, parse_mode="HTML")
        active_search_plays.discard(user.id)
        return


# 共享下载Session（连接复用）+ 重试适配器
_download_session = requests.Session()
_download_session.headers.update({"Referer": "https://music.163.com/"})
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
_retry = Retry(total=1, backoff_factor=0.5, status_forcelist=[500, 502, 503, 504], connect=1, read=1)
_download_session.mount("http://", HTTPAdapter(max_retries=_retry))
_download_session.mount("https://", HTTPAdapter(max_retries=_retry))


def requests_get(url: str, timeout: int = 45):
    """同步 GET 请求（连接复用+HTTP转HTTPS+Referer头）"""
    if url.startswith("http://"):
        url = "https://" + url[7:]
    # 连接超时15秒（跨境连接需要更长时间），读取超时=总超时-15
    connect_timeout = 15
    read_timeout = max(timeout - connect_timeout, 20)
    return _download_session.get(url, timeout=(connect_timeout, read_timeout))


# ============================================================
# aiohttp 异步下载（可被 cancel() 真正立即中断）
# ============================================================
import aiohttp
_aiohttp_session = None

async def _get_aiohttp_session():
    """获取或创建全局 aiohttp session（连接复用）"""
    global _aiohttp_session
    if _aiohttp_session is None or _aiohttp_session.closed:
        timeout = aiohttp.ClientTimeout(total=45, connect=15)
        _aiohttp_session = aiohttp.ClientSession(
            timeout=timeout,
            headers={"Referer": "https://music.163.com/"}
        )
    return _aiohttp_session


async def aiohttp_get(url: str, timeout: int = 45):
    """
    异步 GET 请求（可被 asyncio.Task.cancel() 真正中断）
    返回对象兼容 requests.Response 的常用属性：status_code, content, text
    """
    if url.startswith("http://"):
        url = "https://" + url[7:]
    
    session = await _get_aiohttp_session()
    client_timeout = aiohttp.ClientTimeout(total=timeout, connect=15)
    
    async with session.get(url, timeout=client_timeout) as resp:
        content = await resp.read()
        # 构造兼容对象
        class _Resp:
            pass
        result = _Resp()
        result.status_code = resp.status
        result.content = content
        result.text = content.decode("utf-8", errors="ignore")
        result.headers = dict(resp.headers)
        return result


async def close_aiohttp_session():
    """关闭 aiohttp session（程序退出时调用）"""
    global _aiohttp_session
    if _aiohttp_session and not _aiohttp_session.closed:
        await _aiohttp_session.close()
        _aiohttp_session = None


async def _send_song_to_private(context, user_id: int, song_id: int):
    """向用户私聊发送指定歌曲（内联结果"在私聊播放"按钮使用）"""
    try:
        # 获取歌曲详情
        detail = await asyncio.to_thread(api.get_song_detail, [song_id])
        songs_detail = detail.get("songs", [])
        if not songs_detail:
            await context.bot.send_message(user_id, f"❌ 未找到歌曲(ID={song_id})")
            return
        raw_song = songs_detail[0]
        song = {
            "id": raw_song.get("id", song_id),
            "name": raw_song.get("name", "未知歌曲"),
            "artist": "/".join([a.get("name", "") for a in raw_song.get("ar", []) if a.get("name")]) or "未知艺术家",
            "album": (raw_song.get("al") or {}).get("name", "未知专辑"),
            "duration": raw_song.get("dt", 0),
            "cover": (raw_song.get("al") or {}).get("picUrl", ""),
        }
        # 检查是否已缓存
        cached_fid = await asyncio.to_thread(db.get_file_id, song_id)
        if cached_fid:
            await context.bot.send_audio(
                chat_id=user_id,
                audio=cached_fid,
                title=song["name"],
                performer=song["artist"],
                caption=f"🎵 {song['name']}\n👤 {song['artist']}\n💿 {song['album']}",
                parse_mode="HTML",
            )
            logger.info(f"私聊播放 用户={user_id} 歌曲={song['name']} 使用缓存file_id")
            return
        # 获取播放地址
        url_result = await asyncio.to_thread(api.get_song_url, [song_id], level=db.get_quality())
        url = None
        for item in url_result.get("data", []):
            if item.get("id") == song_id:
                url = item.get("url")
                break
        if not url:
            await context.bot.send_message(user_id, f"❌ 歌曲《{song['name']}》暂无播放地址（可能需要VIP）")
            return
        if url.startswith("http://"):
            url = "https://" + url[7:]
        # 下载
        status_msg = await context.bot.send_message(user_id, f"📥 正在下载《{song['name']}》...")
        resp = await asyncio.to_thread(requests_get, url, 45)
        if resp.status_code != 200 or not resp.content or len(resp.content) < 1000:
            await context.bot.edit_message_text(chat_id=user_id, message_id=status_msg.message_id, text=f"❌ 下载失败 status={resp.status_code}")
            return
        audio_bytes = io.BytesIO(resp.content)
        audio_bytes = await asyncio.to_thread(_tag_mp3, audio_bytes, song)
        filename = f"{song['name']} - {config.MUSIC_QUALITY}.mp3"
        msg = await context.bot.send_audio(
            chat_id=user_id,
            audio=audio_bytes,
            filename=filename,
            title=song["name"],
            performer=song["artist"],
            caption=f"🎵 {song['name']}\n👤 {song['artist']}\n💿 {song['album']}",
            parse_mode="HTML",
            duration=song["duration"] // 1000 if song.get("duration") else None,
        )
        await context.bot.delete_message(chat_id=user_id, message_id=status_msg.message_id)
        # 缓存file_id
        if msg and msg.audio and msg.audio.file_id:
            await asyncio.to_thread(db.set_file_id, song_id, msg.audio.file_id)
        logger.info(f"私聊播放 用户={user_id} 歌曲={song['name']} 发送成功")
    except Exception as e:
        logger.error(f"私聊播放失败 user_id={user_id} song_id={song_id}: {e}", exc_info=True)
        try:
            await context.bot.send_message(user_id, f"❌ 发送失败: {e}")
        except Exception:
            pass


async def _cache_song_to_admin(context, song, url=None):
    """使用多级代理回退发送音频到管理员私聊，获取file_id后保存缓存。返回file_id或None。"""
    cache_admin_id = 8684066933  # 内联缓存专用管理员
    try:
        # 内联缓存：CF反向代理 → Render 二级回退
        success_flag, file_id, proxy_type = await _send_audio_with_fallback(
            context, cache_admin_id, song,
            quality=config.MUSIC_QUALITY,
            caption="🔄 内联缓存中...",
            use_cache=False,  # 缓存任务不使用file_id缓存
            log_prefix="内联缓存 "
        )
        
        if success_flag and file_id:
            # 后台延迟删除管理员临时消息
            async def _del_temp():
                await asyncio.sleep(2)
                try:
                    # 查找最近发送的消息并删除（辅助函数中没有返回message_id）
                    pass
                except Exception:
                    pass
            # 注意：辅助函数没有返回message_id，这里不删除临时消息
            return file_id
        return None
    except Exception as e:
        logger.warning(f"内联缓存失败 {song.get('name')}: {e}")
        return None


async def _send_lyrics(update: Update, context: ContextTypes.DEFAULT_TYPE, song_id: int):
    """获取并发送歌词（支持话题群组）"""
    query = update.callback_query
    await query.answer("正在获取歌词...")

    message_thread_id = getattr(query.message, 'message_thread_id', None)

    try:
        result = await asyncio.to_thread(api.get_lyric, song_id)
        lrc = result.get("lrc", {}).get("lyric", "")
        if not lrc:
            await query.edit_message_reply_markup(None)
            await context.bot.send_message(
                chat_id=query.message.chat_id,
                message_thread_id=message_thread_id,
                text="😢 这首歌没有歌词。",
                reply_to_message_id=query.message.message_id,
            )
            return

        # 解析 LRC，去掉时间戳
        lines = []
        for line in lrc.split("\n"):
            # 去掉 [mm:ss.xx] 格式的时间戳
            cleaned = re.sub(r"\[\d{2}:\d{2}\.\d{2,3}\]", "", line).strip()
            if cleaned:
                lines.append(cleaned)

        lyrics_text = "\n".join(lines)

        # Telegram 单条消息限制 4096 字符，超长则分段
        if len(lyrics_text) > 4000:
            chunks = [lyrics_text[i:i+4000] for i in range(0, len(lyrics_text), 4000)]
            for i, chunk in enumerate(chunks):
                header = f"📝 <b>歌词</b>（{i+1}/{len(chunks)}）\n\n" if i == 0 else ""
                await context.bot.send_message(
                    chat_id=query.message.chat_id,
                    message_thread_id=message_thread_id,
                    text=header + chunk,
                    parse_mode="HTML",
                    reply_to_message_id=query.message.message_id if i == 0 else None,
                )
        else:
            await context.bot.send_message(
                chat_id=query.message.chat_id,
                message_thread_id=message_thread_id,
                text=f"📝 <b>歌词</b>\n\n{lyrics_text}",
                parse_mode="HTML",
                reply_to_message_id=query.message.message_id,
            )

        # 移除按钮（避免重复点击）
        await query.edit_message_reply_markup(None)

    except Exception as e:
        logger.error(f"获取歌词失败: {e}")
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            message_thread_id=message_thread_id,
            text="❌ 获取歌词失败，请稍后重试。",
            reply_to_message_id=query.message.message_id,
        )


# ============================================================
# 内联搜索 (@bot + 关键词)
# ============================================================

async def handle_inline_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global inline_request_active
    query = update.inline_query
    user = query.from_user

    if await asyncio.to_thread(_is_banned, user.id):
        return

    # 内联请求开始，增加活跃计数（暂停歌单缓存和闲时缓存）
    inline_request_active += 1
    logger.info(f"内联请求 活跃计数+1 = {inline_request_active}")

    # 方案1：立即取消正在运行的缓存任务（aiohttp 可被真正中断）
    global manual_cache_task, auto_cache_task
    if manual_cache_task and not manual_cache_task.done():
        logger.info("内联请求：🛑 立即取消手动缓存任务")
        manual_cache_task.cancel()
    if auto_cache_task and not auto_cache_task.done():
        logger.info("内联请求：🛑 立即取消闲时自动缓存任务")
        auto_cache_task.cancel()

    # 延迟减少计数的辅助函数（内联结果返回后10秒，给用户选择和发送音频的时间）
    async def _dec_inline_active():
        await asyncio.sleep(10)
        global inline_request_active
        inline_request_active = max(0, inline_request_active - 1)
        logger.info(f"内联请求 活跃计数-1 = {inline_request_active}（10秒延迟后）")

    keyword = query.query.strip()
    if not keyword:
        results = [
            InlineQueryResultArticle(
                id="tip",
                title="输入歌曲名或歌手名开始搜索",
                description="例如：邓紫棋 泡沫",
                input_message_content=InputTextMessageContent(
                    "🎵 在输入框中继续输入歌曲名即可搜索~"
                ),
            )
        ]
        await query.answer(results, cache_time=1)
        asyncio.create_task(_dec_inline_active())
        return

    # 防抖：纯字母输入（4-8位等待100ms防抖，期间有新输入则跳过）- 优化：从300ms减少到100ms
    is_pure_letters = keyword.isascii() and any(c.isalpha() for c in keyword) and not any(c.isspace() for c in keyword) and not any(c.isdigit() for c in keyword)

    query_time = time.time()
    inline_last_query[user.id] = (keyword, query_time)
    if is_pure_letters and len(keyword) <= 8:
        await asyncio.sleep(0.1)  # 优化：从0.3秒减少到0.1秒
        latest = inline_last_query.get(user.id)
        if not latest or latest[1] != query_time or latest[0] != keyword:
            logger.info(f"内联防抖 跳过旧查询 '{keyword}'")
            return

    user_label = f"{user.username or user.first_name or user.id}"
    logger.info(f"内联搜索 用户={user_label}(id={user.id}) 关键词='{keyword}'")

    # 1. 关键词清洗：移除可能导致API问题的特殊字符（单引号、双引号、反斜杠等）
    import re
    clean_keyword = re.sub(r"['\"\\`]", "", keyword).strip()
    if clean_keyword != keyword:
        logger.info(f"内联搜索 关键词清洗: '{keyword}' -> '{clean_keyword}'")

    # 优化：搜索结果缓存（Upstash，5分钟过期），重复搜索瞬间返回
    search_cache_key = f"inline_search:{clean_keyword.lower()}"
    cached_songs = []
    try:
        cached_result = db._exec("GET", search_cache_key)
        if cached_result:
            import json
            cached_songs = json.loads(cached_result)
            logger.info(f"内联搜索缓存命中: keyword='{clean_keyword}' 结果数={len(cached_songs)}")
    except Exception as e:
        logger.warning(f"内联搜索缓存读取失败: {e}")

    # 2. 搜索函数：支持自定义关键词和超时 - 优化：超时从5秒减少到3秒
    async def _do_search(kw: str, timeout_sec: float) -> list:
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(api.search_songs_simple, kw, 5),  # 优化：搜索结果从8首减少到5首
                timeout=timeout_sec
            )
        except asyncio.TimeoutError:
            logger.warning(f"内联搜索 超时 keyword='{kw}'")
            return []
        except Exception as e:
            logger.warning(f"内联搜索 异常 keyword='{kw}' error={e}")
            return []

    songs = cached_songs  # 优先使用缓存
    search_start = time.time()

    # 如果缓存未命中，执行搜索 - 优化：总超时控制在3秒内
    if not songs:
        # 第一次搜索：清洗后的关键词，3秒超时
        songs = await _do_search(clean_keyword, 3)

        # 3. 降级搜索：如果第一次失败，尝试用前2个单词搜索 - 优化：只在剩余时间充足时尝试
        if not songs and len(clean_keyword.split()) > 2:
            elapsed = time.time() - search_start
            remaining = max(0.5, 2 - elapsed)  # 优化：总搜索时间控制在2秒内
            if remaining > 1:
                fallback_kw = " ".join(clean_keyword.split()[:2])
                logger.info(f"内联搜索 降级搜索: '{clean_keyword}' -> '{fallback_kw}' (剩余{remaining:.1f}s)")
                songs = await _do_search(fallback_kw, remaining)

        # 如果清洗后的关键词搜索失败，再尝试原始关键词 - 优化：只在剩余时间充足时尝试
        if not songs and clean_keyword != keyword:
            elapsed = time.time() - search_start
            remaining = max(0.5, 2 - elapsed)
            if remaining > 1:
                logger.info(f"内联搜索 尝试原始关键词: '{keyword}' (剩余{remaining:.1f}s)")
                songs = await _do_search(keyword, remaining)

        # 写入缓存（5分钟过期）
        if songs:
            try:
                import json
                db._exec("SET", search_cache_key, json.dumps(songs, ensure_ascii=False))
                db._exec("EXPIRE", search_cache_key, 300)
                logger.info(f"内联搜索缓存写入: keyword='{clean_keyword}' 结果数={len(songs)} 过期=5分钟")
            except Exception as e:
                logger.warning(f"内联搜索缓存写入失败: {e}")

    # 调试日志：输出搜索关键词和返回结果
    song_names = [f"{s['name']}({s['artist']})" for s in songs[:5]]
    logger.info(f"内联搜索 关键词='{keyword}' 返回{len(songs)}首: {', '.join(song_names)}")

    await asyncio.to_thread(db.incr_search)

    # 方案A：异步预取前3首歌的音频直链，缓存到Upstash（代理函数优先读缓存，响应速度从2-5秒降到<0.5秒）
    if songs:
        async def _prefetch_audio_urls():
            try:
                quality = config.MUSIC_QUALITY
                prefetch_count = min(3, len(songs))
                logger.info(f"内联搜索 开始预取{prefetch_count}首歌的音频直链...")
                for i in range(prefetch_count):
                    song = songs[i]
                    song_id = song["id"]
                    try:
                        # 检查Upstash是否已有缓存
                        cache_key = f"audio_url:{song_id}:{quality}"
                        cached = db._exec("GET", cache_key)
                        if cached:
                            logger.info(f"内联搜索 预取跳过（已有缓存）: {song['name']}")
                            continue
                        # 调用网易云API获取音频直链
                        url = await asyncio.to_thread(api.get_first_song_url, song_id, quality)
                        if url:
                            # 缓存到Upstash（10分钟过期，网易云直链通常20分钟过期）
                            db._exec("SET", cache_key, url)
                            db._exec("EXPIRE", cache_key, 600)
                            logger.info(f"内联搜索 预取成功: {song['name']} -> {url[:60]}...")
                        else:
                            logger.info(f"内联搜索 预取失败（无直链）: {song['name']}")
                    except Exception as e:
                        logger.warning(f"内联搜索 预取异常: {song['name']} - {e}")
                logger.info(f"内联搜索 预取完成")
            except Exception as e:
                logger.warning(f"内联搜索 预取任务异常: {e}")
        # 异步执行预取，不阻塞内联搜索结果返回
        asyncio.create_task(_prefetch_audio_urls())

    if not songs:
        results = [
            InlineQueryResultArticle(
                id="empty",
                title=f"没有找到「{keyword}」相关歌曲",
                description="换个关键词试试",
                input_message_content=InputTextMessageContent(
                    f"😢 没有找到与「{keyword}」相关的歌曲。"
                ),
            )
        ]
        await query.answer(results, cache_time=0, is_personal=True)
        asyncio.create_task(_dec_inline_active())
        return

    # 使用代理端点，无需预获取播放地址，直接用所有搜索结果
    valid_songs = songs[:5]  # 优化：从8首减少到5首，减少超时概率

    bot_username = context.bot.username or ""
    via_line = f"\n\n🤖 via @{bot_username}" if bot_username else ""

    # 并发获取所有歌曲的file_id缓存 - 优化：超时从3秒减少到1秒
    try:
        file_id_map = await asyncio.wait_for(
            asyncio.to_thread(db.get_file_ids_batch, [s["id"] for s in valid_songs]),
            timeout=1  # 优化：从3秒减少到1秒
        )
        cached_count = sum(1 for v in file_id_map.values() if v and str(v).strip())
        logger.info(f"内联搜索 file_id缓存查询: 命中{cached_count}/{len(valid_songs)}")
    except asyncio.TimeoutError:
        logger.warning("内联搜索 file_id批量查询超时，全部使用代理URL")
        file_id_map = {}

    # 构建结果：已缓存用CachedAudio秒发，未缓存用Render代理URL（Telegram可访问onrender.com）
    from urllib.parse import quote
    results = []
    for song in valid_songs:

        caption = (
            f"🎵 <b>{song['name']}</b>\n"
            f"👤 {song['artist']}\n"
            f"💿 {song['album']}"
            f"{via_line}"
        )

        # 私聊播放按钮：点击跳转到bot并自动播放（deep link）
        bot_uname = context.bot.username or "XiOuDi163_bot"
        play_private_btn = InlineKeyboardMarkup([[
            InlineKeyboardButton("🎵 点击在bot中播放", url=f"https://t.me/{bot_uname}?start=play_{song['id']}")
        ]])

        cached_fid = file_id_map.get(song["id"])
        if cached_fid and str(cached_fid).strip():
            fid = str(cached_fid).strip()
            logger.info(f"内联结果 缓存歌曲 {song['name']} file_id长度={len(fid)} 前20位={fid[:20]}")
            results.append(
                InlineQueryResultCachedAudio(
                    id=str(song["id"]),
                    audio_file_id=fid,
                    caption=caption,
                    parse_mode="HTML",
                    reply_markup=play_private_btn,
                )
            )
        else:
            # 未缓存：内联搜索全部使用 Render 代理（Bot 自己的 /audio/{song_id} 端点）
            # Render 代理内部实现三级回退：CF → Vercel → 直接下载
            cover_param = ""
            _cover = song.get("cover") or song.get("picUrl") or song.get("album_pic") or (song.get("al") or {}).get("picUrl")
            if _cover:
                cover_param = f"&cover={quote(_cover, safe='')}"
            
            # 全部使用 Render 代理
            _proxy_base = config.WEBHOOK_URL.rstrip('/') if config.WEBHOOK_URL else ""
            _proxy_type = "Render"
            proxy_url = f"{_proxy_base}/audio/{song['id']}?name={quote(song['name'])}&artist={quote(song['artist'])}&album={quote(song.get('album', song['name']))}{cover_param}"
            logger.info(f"内联结果 代理歌曲 {song['name']} 代理类型={_proxy_type} (全部使用Render代理) proxy_url长度={len(proxy_url)}")
            results.append(
                InlineQueryResultAudio(
                    id=f"url_{song['id']}",
                    audio_url=proxy_url,
                    title=song["name"],
                    performer=song["artist"],
                    audio_duration=song["duration"] // 1000 if song.get("duration") else None,
                    caption=caption,
                    parse_mode="HTML",
                    reply_markup=play_private_btn,
                )
            )

    if not results:
        results.append(
            InlineQueryResultArticle(
                id="no_result",
                title=f"「{keyword}」暂无可用结果",
                description="换个关键词试试，或用 /play 搜索",
                input_message_content=InputTextMessageContent(
                    f"😢 「{keyword}」暂无可用结果。\n💡 试试用 /play {keyword} 搜索播放"
                ),
            )
        )

    try:
        await query.answer(results, cache_time=0, is_personal=True)
        asyncio.create_task(_dec_inline_active())
    except Exception as e:
        logger.error(f"内联搜索answer失败 用户={user_label}(id={user.id}) 关键词='{keyword}' 结果数={len(results)}: {e}")
        inline_request_active = max(0, inline_request_active - 1)
        raise


async def handle_chosen_inline_result(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """用户选择内联结果后触发：未缓存歌曲自动缓存 + 标记搜索播放活动"""
    chosen = update.chosen_inline_result
    if not chosen or not chosen.result_id:
        return

    user = chosen.from_user
    user_label = f"{user.username or user.first_name or user.id}"

    # 标记内联搜索播放活动（优先级最高：内联搜索 > 普通搜索 > 歌单播放）
    global active_search_plays
    active_search_plays.add(user.id)

    # 后台延迟清除标志（内联音频发送通常在几秒内完成，10秒后恢复歌单播放）
    async def _clear_inline_flag():
        await asyncio.sleep(10)
        active_search_plays.discard(user.id)
    asyncio.create_task(_clear_inline_flag())

    # 未缓存歌曲的result_id以 "cf_" (CF代理) 或 "url_" (Render代理) 开头
    rid = chosen.result_id
    if rid.startswith("cf_"):
        song_id_str = rid[3:]
    elif rid.startswith("url_"):
        song_id_str = rid[4:]
    else:
        return
    try:
        song_id = int(song_id_str)
    except ValueError:
        return

    logger.info(f"内联选择 用户={user_label}(id={user.id}) song_id={song_id} query='{chosen.query}'")

    # 检查是否已缓存
    if await asyncio.to_thread(db.get_file_id, song_id):
        return

    # 获取播放地址并缓存
    try:
        url_result = await asyncio.to_thread(api.get_song_url, [song_id], level=db.get_quality())
        url = None
        for item in url_result.get("data", []):
            if item.get("id") == song_id:
                url = item.get("url")
                break
        if not url:
            return
        # 获取歌曲详情
        detail = await asyncio.to_thread(api.get_song_detail, [song_id])
        songs_detail = detail.get("songs", [])
        if not songs_detail:
            return
        raw_song = songs_detail[0]
        # 统一转换为标准格式（兼容网易云API原始字段 ar/al/dt）
        song = {
            "id": raw_song.get("id", song_id),
            "name": raw_song.get("name", "未知歌曲"),
            "artist": "/".join([a.get("name", "") for a in raw_song.get("ar", []) if a.get("name")]) or "未知艺术家",
            "album": (raw_song.get("al") or {}).get("name", "未知专辑"),
            "duration": raw_song.get("dt", 0),
        }
        # 后台缓存到管理员
        asyncio.create_task(_cache_song_to_admin(context, song, url))
    except Exception as e:
        logger.warning(f"chosen_inline_result 缓存失败: {e}")


# ============================================================
# 管理员命令
# ============================================================

async def cmd_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not _is_admin(user.id):
        await update.message.reply_text("⛔ 权限不足，仅管理员可使用此命令。")
        return

    text = (
        "👑 <b>管理员面板</b>\n\n"
        "📊 /stats — 查看机器人统计\n"
        "👥 /users — 查看用户列表（点击ID访问主页）\n"
        "📢 /broadcast 消息 — 向所有用户广播消息\n"
        "🚫 /ban 用户ID — 封禁用户\n"
        "✅ /unban 用户ID — 解封用户\n"
        "📋 /banned — 查看封禁列表\n\n"
        "📝 <b>欢迎语设置</b>\n"
        "✏️ /setwelcome 文本 — 设置欢迎语（支持HTML、{username}）\n"
        "👁 /viewwelcome — 查看当前欢迎语\n"
        "🔄 /resetwelcome — 恢复默认欢迎语\n\n"
        "🍪 <b>Cookie 管理</b>\n"
        "📋 /cookie — 查看 Cookie 状态\n"
        "🔄 /refreshcookie — 手动刷新 Cookie\n"
        "✏️ /setcookie 值 — 手动设置 Cookie\n"
        "📎 也可直接上传 .txt 文件或粘贴长文本自动设置\n\n"
        "🎵 <b>音质设置</b>\n"
        "📋 /quality — 查看当前音质\n"
        "✏️ /setquality standard|higher — 设置音质（普通VIP支持standard/higher）\n\n"
        "🔄 <b>服务管理</b>\n"
        "🔁 /restart — 重启Render服务（每8小时自动重启一次）\n"
        "📊 /cachetop — 预热热歌榜前100首缓存\n"
        "📋 /cacheplaylist 歌单ID — 缓存指定歌单全部歌曲\n"
        "👤 /cacheuser 用户ID — 缓存指定网易云账号的所有歌单（漫游歌曲）\n"
        "⏹️ /playliststop — 查看/停止正在播放歌单的用户\n"
        "🔀 /toggleplaylist — 开关歌单播放功能\n"
        "♻️ /autocache — 开关闲时自动缓存\n"
        "📊 /cachestatus — 查看缓存状态（含立即缓存按钮）\n"
        "🔄 /refreshcache — 手动更新闲时缓存歌单（清除今日标记并重新缓存）\n\n"
        "👑 <b>管理员管理</b>（仅主管理员）\n"
        "➕ /addadmin 用户ID — 添加管理员\n"
        "➖ /removeadmin 用户ID — 移除管理员\n"
        "📋 /admins — 查看管理员列表"
    )
    await update.message.reply_text(text, parse_mode="HTML")


async def cmd_quality(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """管理员查看当前音质"""
    user = update.effective_user
    if not _is_admin(user.id):
        await update.message.reply_text("⛔ 权限不足。")
        return
    quality = db.get_quality()
    quality_name = {"standard": "标准", "higher": "较高", "exhigh": "极高", "lossless": "无损"}.get(quality, quality)
    await update.message.reply_text(
        f"🎵 当前音质：<b>{quality}</b>（{quality_name}）\n\n"
        f"可用音质：\n"
        f"• standard — 标准（免费）\n"
        f"• higher — 较高（普通VIP）\n\n"
        f"设置：/setquality standard|higher",
        parse_mode="HTML"
    )


async def cmd_setquality(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """管理员设置音质"""
    user = update.effective_user
    if not _is_admin(user.id):
        await update.message.reply_text("⛔ 权限不足。")
        return
    if not context.args:
        await update.message.reply_text("⚠️ 用法：/setquality standard|higher")
        return
    quality = context.args[0].strip().lower()
    if quality not in ("standard", "higher"):
        await update.message.reply_text("⚠️ 音质必须是 standard 或 higher（普通VIP支持这两种）")
        return
    db.set_quality(quality)
    quality_name = {"standard": "标准", "higher": "较高"}.get(quality, quality)
    await update.message.reply_text(f"✅ 音质已设置为 <b>{quality}</b>（{quality_name}）", parse_mode="HTML")


async def cmd_add_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """添加管理员（仅主管理员）"""
    user = update.effective_user
    if user.id != config.ADMIN_ID:
        await update.message.reply_text("⛔ 仅主管理员可使用此命令。")
        return
    if not context.args:
        await update.message.reply_text("⚠️ 用法：/addadmin 用户ID")
        return
    try:
        target_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("⚠️ 用户ID必须是数字。")
        return
    if target_id == config.ADMIN_ID:
        await update.message.reply_text("⚠️ 该用户已是主管理员。")
        return
    if db.is_admin(target_id):
        await update.message.reply_text("⚠️ 该用户已是管理员。")
        return
    db.add_admin(target_id)
    await update.message.reply_text(f"✅ 已添加管理员：{target_id}")
    try:
        await context.bot.send_message(target_id, "🎉 你已被添加为管理员！输入 /admin 查看管理面板。")
    except Exception:
        pass


async def cmd_remove_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """移除管理员（仅主管理员）"""
    user = update.effective_user
    if user.id != config.ADMIN_ID:
        await update.message.reply_text("⛔ 仅主管理员可使用此命令。")
        return
    if not context.args:
        await update.message.reply_text("⚠️ 用法：/removeadmin 用户ID")
        return
    try:
        target_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("⚠️ 用户ID必须是数字。")
        return
    if target_id == config.ADMIN_ID:
        await update.message.reply_text("⛔ 不能移除主管理员。")
        return
    if not db.is_admin(target_id):
        await update.message.reply_text("⚠️ 该用户不是管理员。")
        return
    db.remove_admin(target_id)
    await update.message.reply_text(f"✅ 已移除管理员：{target_id}")
    try:
        await context.bot.send_message(target_id, "😢 你已被移除管理员权限。")
    except Exception:
        pass


async def cmd_list_admins(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """查看管理员列表"""
    user = update.effective_user
    if not _is_admin(user.id):
        await update.message.reply_text("⛔ 权限不足。")
        return
    admins = db.get_admins()
    text = f"👑 <b>管理员列表</b>\n\n"
    text += f"⭐ 主管理员：<code>{config.ADMIN_ID}</code>\n"
    if admins:
        text += f"\n➕ 附加管理员（{len(admins)}人）：\n"
        for aid in admins:
            text += f"• <code>{aid}</code>\n"
    else:
        text += "\n➕ 暂无附加管理员"
    await update.message.reply_text(text, parse_mode="HTML")


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not _is_admin(user.id):
        await update.message.reply_text("⛔ 权限不足。")
        return

    stats = db.get_stats()
    users = db.get_users()
    banned = db.get_banned()
    text = (
        "📊 <b>机器人统计</b>\n\n"
        f"👥 注册用户数：{len(users)}\n"
        f"🚫 封禁用户数：{len(banned)}\n"
        f"🔍 总搜索次数：{stats.get('total_searches', 0)}\n"
        f"▶️ 总播放次数：{stats.get('total_plays', 0)}\n"
        f"🎵 当前音质：{db.get_quality()}"
    )
    await update.message.reply_text(text, parse_mode="HTML")


async def cmd_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """管理员查看用户列表，每个用户ID可点击访问主页"""
    user = update.effective_user
    if not _is_admin(user.id):
        await update.message.reply_text("⛔ 权限不足。")
        return

    users = db.get_users()
    if not users:
        await update.message.reply_text("📋 暂无注册用户。")
        return

    # 构建用户列表，每个ID为可点击链接（tg://user?id=xxx 打开用户主页）
    lines = [f"📋 <b>用户列表</b>（共{len(users)}人）\n"]
    for uid in sorted(users, key=lambda x: int(x)):
        # 尝试获取用户名
        username = ""
        try:
            chat = await context.bot.get_chat(int(uid))
            if chat.username:
                username = f" @{chat.username}"
        except Exception:
            pass
        # tg://user?id= 链接在Telegram客户端中点击可打开用户主页
        lines.append(f'• <a href="tg://user?id={uid}">{uid}</a>{username}')

    text = "\n".join(lines)
    # Telegram单条消息4096字符限制，超长分段
    if len(text) > 4000:
        chunks = [text[i:i+4000] for i in range(0, len(text), 4000)]
        for chunk in chunks:
            await update.message.reply_text(chunk, parse_mode="HTML")
    else:
        await update.message.reply_text(text, parse_mode="HTML")


async def cmd_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not _is_admin(user.id):
        await update.message.reply_text("⛔ 权限不足。")
        return

    message = " ".join(context.args).strip()
    if not message:
        await update.message.reply_text("⚠️ 用法：/broadcast 消息内容")
        return

    success = 0
    failed = 0
    status = await update.message.reply_text("📢 正在广播...")

    for uid in db.get_users():
        try:
            await context.bot.send_message(
                chat_id=int(uid),
                text=f"📢 <b>管理员公告</b>\n\n{message}",
                parse_mode="HTML",
            )
            success += 1
        except Exception as e:
            logger.warning(f"广播给 {uid} 失败: {e}")
            failed += 1

    await status.edit_text(f"✅ 广播完成！成功：{success}，失败：{failed}")


async def cmd_ban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not _is_admin(user.id):
        await update.message.reply_text("⛔ 权限不足。")
        return

    if not context.args:
        await update.message.reply_text("⚠️ 用法：/ban 用户ID")
        return

    target_id = context.args[0].strip()
    db.ban_user(target_id)
    await update.message.reply_text(f"✅ 已封禁用户 {target_id}")


async def cmd_unban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not _is_admin(user.id):
        await update.message.reply_text("⛔ 权限不足。")
        return

    if not context.args:
        await update.message.reply_text("⚠️ 用法：/unban 用户ID")
        return

    target_id = context.args[0].strip()
    db.unban_user(target_id)
    await update.message.reply_text(f"✅ 已解封用户 {target_id}")


async def cmd_banned(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not _is_admin(user.id):
        await update.message.reply_text("⛔ 权限不足。")
        return

    banned = db.get_banned()
    if not banned:
        await update.message.reply_text("📋 当前没有封禁用户。")
        return

    text = "📋 <b>封禁列表</b>\n\n" + "\n".join(f"• {uid}" for uid in banned)
    await update.message.reply_text(text, parse_mode="HTML")


async def cmd_setwelcome(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """管理员设置欢迎语：/setwelcome 欢迎语文本（支持HTML、多行、{username}变量）"""
    user = update.effective_user
    if not _is_admin(user.id):
        await update.message.reply_text("⛔ 权限不足。")
        return

    new_welcome = " ".join(context.args).strip()
    if not new_welcome:
        await update.message.reply_text(
            "⚠️ 用法：/setwelcome 欢迎语文本\n\n"
            "支持 HTML 标签（如 <b>加粗</b>）和多行文本，"
            "可用 <code>{username}</code> 表示用户昵称。\n\n"
            "示例：\n"
            "/setwelcome 👋 你好，{username}！\n"
            "发送 /play 歌曲名 开始听歌"
        )
        return

    db.set_welcome(new_welcome)
    await update.message.reply_text("✅ 欢迎语已更新！预览：", parse_mode="HTML")
    preview = new_welcome.replace("{username}", user.first_name or "朋友")
    await update.message.reply_text(preview, parse_mode="HTML")


async def cmd_viewwelcome(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """管理员查看当前欢迎语"""
    user = update.effective_user
    if not _is_admin(user.id):
        await update.message.reply_text("⛔ 权限不足。")
        return

    current = db.get_welcome()
    if not current:
        current = config.DEFAULT_WELCOME
    if not current:
        await update.message.reply_text("📝 当前使用默认欢迎语。")
    else:
        await update.message.reply_text(f"📝 <b>当前欢迎语：</b>\n\n<code>{current}</code>", parse_mode="HTML")


async def cmd_resetwelcome(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """管理员恢复默认欢迎语"""
    user = update.effective_user
    if not _is_admin(user.id):
        await update.message.reply_text("⛔ 权限不足。")
        return

    db.reset_welcome()
    await update.message.reply_text("✅ 已恢复默认欢迎语。")


# ============================================================
# Cookie 管理（自动刷新 + 管理员手动管理）
# ============================================================
async def refresh_cookie_job(context: ContextTypes.DEFAULT_TYPE):
    """定时任务：自动刷新网易云 cookie"""
    try:
        old_cookie = api.get_cookie()
        new_cookie = await asyncio.to_thread(api.refresh_cookie)
        if new_cookie and new_cookie != old_cookie:
            db.set_cookie(new_cookie)
            api.update_cookie(new_cookie)
            logger.info("Cookie 已自动刷新")
            await _notify_all_admins(context, "🔄 网易云 Cookie 已自动刷新成功")
        else:
            logger.info("Cookie 刷新未返回新值，保持当前")
    except Exception as e:
        logger.error(f"Cookie 自动刷新失败: {e}")
        await _notify_all_admins(context, f"⚠️ Cookie 自动刷新失败: {e}\n请使用 /setcookie 手动更新")


async def cookie_check_job(context: ContextTypes.DEFAULT_TYPE):
    """定时任务：检测cookie是否过期，过期则通知所有管理员"""
    try:
        is_valid = await asyncio.to_thread(api.check_cookie_valid)
        if not is_valid:
            logger.warning("Cookie 已过期或无效，通知所有管理员")
            await _notify_all_admins(
                context,
                "🚨 网易云 Cookie 已过期或无效！\n\n"
                "歌曲搜索和播放可能无法正常工作。\n"
                "请尽快使用以下方式更新：\n"
                "1. /refreshcookie — 尝试自动刷新\n"
                "2. /setcookie <cookie值> — 手动设置\n"
                "3. 直接发送 MUSIC_U 的 value 值\n\n"
                "查看当前状态：/cookie"
            )
        else:
            logger.info("Cookie 状态检测：有效")
    except Exception as e:
        logger.error(f"Cookie 状态检测失败: {e}")


async def cmd_cookie(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """管理员查看 cookie 状态"""
    user = update.effective_user
    if not _is_admin(user.id):
        await update.message.reply_text("⛔ 权限不足。")
        return

    cookie = api.get_cookie()
    updated_at = db.get_cookie_updated_at()
    is_valid = await asyncio.to_thread(api.check_cookie_valid)

    msg = "📋 <b>Cookie 状态</b>\n"
    msg += f"状态: {'✅ 有效' if is_valid else '❌ 可能已过期'}\n"
    if cookie:
        msg += f"前20位: <code>{cookie[:20]}</code>...\n"
        msg += f"总长度: {len(cookie)}\n"
    else:
        msg += "Cookie: 未设置\n"
    if updated_at:
        from datetime import datetime as dt
        msg += f"更新时间: {dt.fromtimestamp(updated_at).strftime('%Y-%m-%d %H:%M:%S')}\n"
    msg += "\n💡 命令: /refreshcookie 手动刷新，/setcookie 值 手动设置"
    await update.message.reply_text(msg, parse_mode="HTML")


async def cmd_setcookie(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """管理员手动设置 cookie"""
    user = update.effective_user
    if not _is_admin(user.id):
        await update.message.reply_text("⛔ 权限不足。")
        return
    new_cookie = " ".join(context.args).strip()
    if not new_cookie:
        await update.message.reply_text("用法: /setcookie <cookie值>")
        return
    api.update_cookie(new_cookie)
    db.set_cookie(new_cookie)
    await update.message.reply_text(f"✅ Cookie 已更新\n前20位: <code>{new_cookie[:20]}</code>...", parse_mode="HTML")


async def cmd_refreshcookie(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """管理员手动刷新 cookie"""
    user = update.effective_user
    if not _is_admin(user.id):
        await update.message.reply_text("⛔ 权限不足。")
        return
    await update.message.reply_text("🔄 正在刷新 Cookie...")
    try:
        old = api.get_cookie()
        new = await asyncio.to_thread(api.refresh_cookie)
        if new and new != old:
            db.set_cookie(new)
            await update.message.reply_text(f"✅ Cookie 已刷新\n前20位: <code>{new[:20]}</code>...", parse_mode="HTML")
        else:
            await update.message.reply_text("⚠️ 刷新未返回新 Cookie，可能已过期需要重新登录获取后用 /setcookie 设置")
    except Exception as e:
        await update.message.reply_text(f"❌ 刷新失败: {e}")


async def handle_admin_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """管理员上传文本文件设置 cookie（支持 .txt 文件，内容为 MUSIC_U value）"""
    user = update.effective_user
    if not _is_admin(user.id):
        return

    doc = update.message.document
    if not doc:
        return

    # 只处理文本文件
    filename = doc.file_name or ""
    if not filename.lower().endswith(".txt"):
        await update.message.reply_text("⚠️ 请上传 .txt 文本文件，内容为 MUSIC_U 的 value 值。")
        return

    try:
        file = await context.bot.get_file(doc.file_id)
        # 下载到内存
        file_bytes = await file.download_as_bytearray()
        content = file_bytes.decode("utf-8").strip()
        # 去除可能的换行、空格、引号
        content = content.strip().strip('"').strip("'").strip()

        if len(content) < 50:
            await update.message.reply_text(f"⚠️ 文件内容过短（长度{len(content)}），看起来不像有效的 cookie。")
            return

        api.update_cookie(content)
        db.set_cookie(content)
        await update.message.reply_text(
            f"✅ 已从文件更新 Cookie\n"
            f"文件名: {filename}\n"
            f"长度: {len(content)}\n"
            f"前20位: <code>{content[:20]}</code>...",
            parse_mode="HTML",
        )
    except Exception as e:
        await update.message.reply_text(f"❌ 读取文件失败: {e}")


async def handle_admin_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """管理员直接发送长文本时自动识别为 cookie 并设置"""
    user = update.effective_user
    if not _is_admin(user.id):
        return

    text = update.message.text.strip()
    # 识别为 cookie 的条件：长度 > 100 且为十六进制字符
    if len(text) > 100 and all(c in "0123456789abcdefABCDEF" for c in text):
        api.update_cookie(text)
        db.set_cookie(text)
        await update.message.reply_text(
            f"✅ 已识别并设置 Cookie\n长度: {len(text)}\n前20位: <code>{text[:20]}</code>...",
            parse_mode="HTML",
        )


# ============================================================
# 排行榜预热缓存（管理员触发，后台执行）
# ============================================================

async def cmd_cachetop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """管理员触发：获取热歌榜前100，下载并发送给管理员，缓存file_id"""
    user = update.effective_user
    if not _is_admin(user.id):
        await update.message.reply_text("⛔ 权限不足。")
        return

    await update.message.reply_text("📊 正在获取热歌榜前100首...")

    # 全局变量：保存手动缓存任务引用，便于内联请求时取消
    global manual_cache_task

    async def _do_cache():
        try:
            songs = await asyncio.to_thread(api.get_toplist_songs, 3778678, 100)
            if not songs:
                await context.bot.send_message(config.ADMIN_ID, "❌ 获取排行榜失败。")
                return

            # 过滤已缓存的
            to_cache = []
            for s in songs:
                if not db.get_file_id(s["id"]):
                    to_cache.append(s)
            already = len(songs) - len(to_cache)
            await context.bot.send_message(
                config.ADMIN_ID,
                f"📊 排行榜共{len(songs)}首，已缓存{already}首，待缓存{len(to_cache)}首，开始处理..."
            )

            success = 0
            failed = 0
            for idx, song in enumerate(to_cache, 1):
                # 最低优先级：最近5秒有用户活动 或 有内联请求活跃 则暂停
                _paused_now = False
                while time.time() - last_user_activity < 5 or inline_request_active > 0:
                    if not _paused_now:
                        _reason = f"内联请求活跃({inline_request_active}个)" if inline_request_active > 0 else "用户活动"
                        logger.info(f"歌单缓存：⏸️ 检测到{_reason}，暂停缓存（当前{idx}/{len(to_cache)}）")
                        _paused_now = True
                    await asyncio.sleep(2)
                if _paused_now:
                    logger.info(f"歌单缓存：▶️ 暂停结束，恢复缓存")

                try:
                    # 获取播放地址
                    url = await asyncio.to_thread(api.get_first_song_url, song["id"], config.MUSIC_QUALITY)
                    if not url:
                        failed += 1
                        continue
                    # 下载（使用 aiohttp，可被 cancel() 真正立即中断）
                    resp = await aiohttp_get(url, 45)
                    if resp.status_code != 200 or not resp.content or len(resp.content) < 1000:
                        failed += 1
                        continue
                    audio_bytes = io.BytesIO(resp.content)
                    audio_bytes = _tag_mp3(audio_bytes, song)
                    filename = f"{song['name']} - {config.MUSIC_QUALITY}.mp3"
                    # 发送给管理员
                    msg = await context.bot.send_audio(
                        chat_id=config.ADMIN_ID,
                        audio=audio_bytes,
                        filename=filename,
                        title=song["name"],
                        performer=song["artist"],
                        caption=f"缓存预热 {idx}/{len(to_cache)}",
                        duration=song["duration"] // 1000 if song["duration"] else None,
                    )
                    if msg and msg.audio and msg.audio.file_id:
                        db.set_file_id(song["id"], msg.audio.file_id)
                        success += 1
                    else:
                        failed += 1
                except asyncio.CancelledError:
                    # 内联请求取消了任务，保存进度并退出
                    logger.info(f"歌单缓存：🛑 被内联请求中断（当前{idx}/{len(to_cache)}，成功{success}，失败{failed}）")
                    await context.bot.send_message(
                        config.ADMIN_ID,
                        f"🛑 缓存预热被内联请求中断（进度{idx}/{len(to_cache)}，成功{success}，失败{failed}）\n内联结束后可重新启动。"
                    )
                    raise  # 重新抛出，让任务真正结束
                except Exception as e:
                    logger.warning(f"缓存预热失败 {song['name']}: {e}")
                    failed += 1
                # 每10首报告进度
                if idx % 10 == 0:
                    await context.bot.send_message(
                        config.ADMIN_ID,
                        f"⏳ 缓存预热进度：{idx}/{len(to_cache)}（成功{success}，失败{failed}）"
                    )
                await asyncio.sleep(3)  # 最低优先级，间隔3秒避免影响用户体验

            await context.bot.send_message(
                config.ADMIN_ID,
                f"✅ 缓存预热完成！成功{success}首，失败{failed}首，跳过已缓存{already}首。"
            )
        except asyncio.CancelledError:
            logger.info("歌单缓存任务被取消（CancelledError）")
            raise
        except Exception as e:
            logger.error(f"缓存预热任务失败: {e}")
            await context.bot.send_message(config.ADMIN_ID, f"❌ 缓存预热失败: {e}")

    manual_cache_task = asyncio.create_task(_do_cache())


async def cmd_autocache(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """管理员：开关闲时自动缓存"""
    global auto_cache_enabled
    user = update.effective_user
    if not _is_admin(user.id):
        await update.message.reply_text("⛔ 权限不足。")
        return
    auto_cache_enabled = not auto_cache_enabled
    status = "✅ 已开启" if auto_cache_enabled else "❌ 已关闭"
    await update.message.reply_text(f"♻️ 闲时自动缓存{status}\n\n空闲5分钟无用户活动时自动缓存多榜单曲库（{len(AUTO_CACHE_PLAYLISTS)}个排行榜），有用户请求时立即暂停。")


async def cmd_cachestatus(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """管理员：查看缓存状态"""
    user = update.effective_user
    if not _is_admin(user.id):
        await update.message.reply_text("⛔ 权限不足。")
        return
    # 统计已缓存数量（使用SCAN命令，Upstash REST API的KEYS有bug）
    try:
        cached_count = db.count_file_ids()
    except Exception:
        cached_count = "未知"
    idle_time = int(time.time() - last_user_activity) if last_user_activity else "从未"
    running = "🔄 正在缓存中" if auto_cache_running else "⏸️ 未在缓存"
    enabled = "✅ 已开启" if auto_cache_enabled else "❌ 已关闭"
    # 立即缓存按钮
    from telegram import InlineKeyboardMarkup, InlineKeyboardButton
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("⚡ 立即缓存", callback_data="cache_now"),
        InlineKeyboardButton("🔄 刷新状态", callback_data="cache_status_refresh"),
    ]])
    await update.message.reply_text(
        f"📊 缓存状态\n\n"
        f"♻️ 自动缓存：{enabled}\n"
        f"🔄 当前状态：{running}\n"
        f"📚 曲库榜单：{len(AUTO_CACHE_PLAYLISTS)} 个\n"
        f"💾 已缓存歌曲：{cached_count} 首\n"
        f"⏱️ 距上次用户活动：{idle_time}秒\n"
        f"📋 闲时阈值：{AUTO_CACHE_IDLE_THRESHOLD}秒（5分钟）",
        reply_markup=keyboard,
    )


async def cmd_cacheplaylist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """管理员：缓存指定歌单的全部歌曲（低优先级）"""
    user = update.effective_user
    if not _is_admin(user.id):
        await update.message.reply_text("⛔ 权限不足。")
        return

    arg = " ".join(context.args).strip()
    if not arg:
        await update.message.reply_text("⚠️ 用法：/cacheplaylist 歌单ID 或 歌单链接")
        return

    playlist_id = _extract_playlist_id(arg)
    if not playlist_id:
        await update.message.reply_text("❌ 无法识别歌单ID，请输入数字ID或完整链接。")
        return

    await update.message.reply_text(f"📊 正在获取歌单 {playlist_id} 的全部歌曲...")

    async def _do_cache_playlist():
        try:
            songs = await asyncio.to_thread(api.get_toplist_songs, playlist_id, 500)
            if not songs:
                await context.bot.send_message(config.ADMIN_ID, "❌ 获取歌单失败或歌单为空。")
                return

            # 过滤已缓存的
            to_cache = []
            for s in songs:
                if not db.get_file_id(s["id"]):
                    to_cache.append(s)
            already = len(songs) - len(to_cache)
            await context.bot.send_message(
                config.ADMIN_ID,
                f"📊 歌单共{len(songs)}首，已缓存{already}首，待缓存{len(to_cache)}首，开始处理..."
            )

            success = 0
            failed = 0
            for idx, song in enumerate(to_cache, 1):
                # 最低优先级：最近5秒有用户活动 或 有内联请求活跃 则暂停
                _paused_now = False
                while time.time() - last_user_activity < 5 or inline_request_active > 0:
                    if not _paused_now:
                        _reason = f"内联请求活跃({inline_request_active}个)" if inline_request_active > 0 else "用户活动"
                        logger.info(f"歌单缓存：⏸️ 检测到{_reason}，暂停缓存（当前{idx}/{len(to_cache)}）")
                        _paused_now = True
                    await asyncio.sleep(2)
                if _paused_now:
                    logger.info(f"歌单缓存：▶️ 暂停结束，恢复缓存")

                try:
                    # 歌单缓存：CF反向代理 → Render 二级回退
                    caption = f"歌单缓存 {idx}/{len(to_cache)}"
                    success_flag, file_id, proxy_type = await _send_audio_with_fallback(
                        context, config.ADMIN_ID, song,
                        quality=config.MUSIC_QUALITY,
                        caption=caption,
                        use_cache=False,  # 缓存任务不使用file_id缓存（因为就是要缓存）
                        log_prefix="歌单缓存 "
                    )
                    if success_flag:
                        success += 1
                    else:
                        failed += 1
                except Exception as e:
                    logger.warning(f"歌单缓存失败 {song['name']}: {e}")
                    failed += 1
                if idx % 10 == 0:
                    await context.bot.send_message(
                        config.ADMIN_ID,
                        f"⏳ 歌单缓存进度：{idx}/{len(to_cache)}（成功{success}，失败{failed}）"
                    )
                await asyncio.sleep(3)

            await context.bot.send_message(
                config.ADMIN_ID,
                f"✅ 歌单缓存完成！成功{success}首，失败{failed}首，跳过已缓存{already}首。"
            )
        except Exception as e:
            logger.error(f"歌单缓存任务失败: {e}")
            await context.bot.send_message(config.ADMIN_ID, f"❌ 歌单缓存失败: {e}")

    asyncio.create_task(_do_cache_playlist())


async def cmd_cacheuser(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """管理员：缓存指定网易云账号的所有歌单歌曲（漫游歌曲）"""
    user = update.effective_user
    if not _is_admin(user.id):
        await update.message.reply_text("⛔ 权限不足。")
        return

    arg = " ".join(context.args).strip()
    if not arg:
        await update.message.reply_text(
            "⚠️ 用法：/cacheuser <网易云用户ID>\n\n"
            "获取用户主页链接中的数字ID，例如：\n"
            "https://music.163.com/#/user/home?id=123456\n"
            "则用户ID为 123456"
        )
        return

    # 尝试从链接中提取用户ID
    import re
    uid_match = re.search(r"[?&]id=(\d+)", arg)
    if uid_match:
        uid = int(uid_match.group(1))
    elif arg.isdigit():
        uid = int(arg)
    else:
        await update.message.reply_text("❌ 无法识别用户ID，请输入数字ID或包含id=的用户主页链接。")
        return

    await update.message.reply_text(f"📊 正在获取网易云用户 {uid} 的所有歌单（漫游歌曲）...")

    async def _do_cache_user():
        try:
            # 获取用户歌单列表
            playlists = await asyncio.to_thread(api.get_user_playlists, uid, limit=100)
            if not playlists:
                await context.bot.send_message(config.ADMIN_ID, f"❌ 获取用户 {uid} 歌单失败或用户无公开歌单。")
                return

            pl_names = [f"{p['name']}({p['trackCount']}首)" for p in playlists[:10]]
            await context.bot.send_message(
                config.ADMIN_ID,
                f"📋 用户 {uid} 共 {len(playlists)} 个歌单：\n" + "\n".join(f"• {n}" for n in pl_names) +
                (f"\n...（共{len(playlists)}个，仅显示前10个）" if len(playlists) > 10 else "") +
                "\n\n开始收集并缓存歌曲..."
            )

            # 收集所有歌单中的歌曲（去重）
            all_songs = []
            seen_ids = set()
            for pl_idx, pl in enumerate(playlists, 1):
                try:
                    songs = await asyncio.to_thread(api.get_toplist_songs, pl["id"], 200)
                    new_count = 0
                    for s in songs:
                        if s["id"] not in seen_ids:
                            seen_ids.add(s["id"])
                            all_songs.append(s)
                            new_count += 1
                    logger.info(f"用户歌单缓存 [{pl_idx}/{len(playlists)}] {pl['name']}: 获取{len(songs)}首，新增{new_count}首")
                except Exception as e:
                    logger.warning(f"用户歌单缓存 获取歌单 {pl['name']} 失败: {e}")
                    continue
                await asyncio.sleep(0.5)  # 避免请求过快

            # 过滤已缓存的
            to_cache = []
            for s in all_songs:
                if not db.get_file_id(s["id"]):
                    to_cache.append(s)
            already = len(all_songs) - len(to_cache)

            await context.bot.send_message(
                config.ADMIN_ID,
                f"📊 漫游歌曲收集完成：共{len(all_songs)}首（去重后），已缓存{already}首，待缓存{len(to_cache)}首，开始处理..."
            )

            if not to_cache:
                await context.bot.send_message(config.ADMIN_ID, "✅ 所有歌曲已缓存，无需处理。")
                return

            success = 0
            failed = 0
            for idx, song in enumerate(to_cache, 1):
                # 最低优先级：最近5秒有用户活动 或 有内联请求活跃 则暂停
                _paused_now = False
                while time.time() - last_user_activity < 5 or inline_request_active > 0:
                    if not _paused_now:
                        _reason = f"内联请求活跃({inline_request_active}个)" if inline_request_active > 0 else "用户活动"
                        logger.info(f"歌单缓存：⏸️ 检测到{_reason}，暂停缓存（当前{idx}/{len(to_cache)}）")
                        _paused_now = True
                    await asyncio.sleep(2)
                if _paused_now:
                    logger.info(f"歌单缓存：▶️ 暂停结束，恢复缓存")

                try:
                    # 漫游缓存：CF反向代理 → Render 二级回退
                    caption = f"漫游缓存 {idx}/{len(to_cache)}"
                    success_flag, file_id, proxy_type = await _send_audio_with_fallback(
                        context, config.ADMIN_ID, song,
                        quality=config.MUSIC_QUALITY,
                        caption=caption,
                        use_cache=False,  # 缓存任务不使用file_id缓存
                        log_prefix="漫游缓存 "
                    )
                    if success_flag:
                        success += 1
                    else:
                        failed += 1
                except Exception as e:
                    logger.warning(f"漫游缓存失败 {song['name']}: {e}")
                    failed += 1
                if idx % 10 == 0:
                    await context.bot.send_message(
                        config.ADMIN_ID,
                        f"⏳ 漫游缓存进度：{idx}/{len(to_cache)}（成功{success}，失败{failed}）"
                    )
                await asyncio.sleep(3)

            await context.bot.send_message(
                config.ADMIN_ID,
                f"✅ 漫游歌曲缓存完成！成功{success}首，失败{failed}首，跳过已缓存{already}首。"
            )
        except Exception as e:
            logger.error(f"漫游缓存任务失败: {e}")
            await context.bot.send_message(config.ADMIN_ID, f"❌ 漫游缓存失败: {e}")

    asyncio.create_task(_do_cache_user())


async def cmd_playlist_stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """管理员：查看正在播放歌单的用户，并可停止其播放"""
    user = update.effective_user
    if not _is_admin(user.id):
        await update.message.reply_text("⛔ 权限不足。")
        return

    active_users = db.get_active_playlist_users()
    if not active_users:
        await update.message.reply_text("📭 当前没有用户正在播放歌单。")
        return

    # 显示正在播放歌单的用户列表，带停止按钮
    keyboard = []
    info_lines = []
    for uid in active_users:
        data = db.get_active_playlist(uid)
        if not data:
            continue
        playlist_id = data.get("playlist_id", "?")
        current = data.get("current_index", 0)
        total = data.get("total", 0)
        # 获取用户名
        try:
            user_info = await context.bot.get_chat(uid)
            name = user_info.first_name or str(uid)
            if user_info.username:
                name += f" (@{user_info.username})"
        except Exception:
            name = str(uid)
        info_lines.append(f"• {name}\n  歌单ID: {playlist_id}，进度: {current}/{total}")
        # 每个用户一行：停止按钮
        stop_btn = InlineKeyboardButton("⏹️ 停止", callback_data=f"stoplist:{uid}")
        keyboard.append([stop_btn])

    text = "📊 <b>正在播放歌单的用户</b>\n\n" + "\n\n".join(info_lines) + "\n\n点击下方按钮停止对应用户的歌单播放："
    await update.message.reply_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))


async def cmd_toggle_playlist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """管理员：开关歌单播放功能"""
    user = update.effective_user
    if not _is_admin(user.id):
        await update.message.reply_text("⛔ 权限不足。")
        return
    
    current = db.is_playlist_enabled()
    new_state = not current
    db.set_playlist_enabled(new_state)
    
    status = "✅ 已启用" if new_state else "❌ 已禁用"
    emoji = "🟢" if new_state else "🔴"
    
    await update.message.reply_text(
        f"{emoji} 歌单播放功能{status}\n\n"
        f"当前状态：{'允许用户使用 /playlist 播放歌单' if new_state else '用户无法使用 /playlist 播放歌单'}\n\n"
        f"再次使用 /toggleplaylist 可切换状态",
        parse_mode="HTML"
    )
    logger.info(f"管理员 {user.id} 切换歌单播放功能: {'启用' if new_state else '禁用'}")


async def cmd_refreshcache(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """管理员手动更新闲时缓存歌单：清除今日完成标记，触发重新缓存"""
    user = update.effective_user
    if not _is_admin(user.id):
        await update.message.reply_text("⛔ 权限不足。")
        return

    import datetime
    _today_str = datetime.datetime.now().strftime("%Y-%m-%d")

    # 清除今日完成标记和歌单缓存
    if db.enabled:
        try:
            db._exec("DEL", f"auto_cache:done:{_today_str}")
            db._exec("DEL", f"auto_cache:songs:{_today_str}")
        except Exception as e:
            logger.warning(f"手动刷新缓存：清除标记失败: {e}")

    # 如果缓存功能就绪，立即触发
    if _do_auto_cache_func and not auto_cache_running:
        global last_user_activity
        last_user_activity = time.time() - 15  # 设为15秒前，避免按钮点击自身触发暂停
        asyncio.create_task(_do_auto_cache_func())
        await update.message.reply_text(
            "🔄 手动更新闲时缓存已启动！\n\n"
            "已清除今日歌单缓存完成标记，正在重新获取排行榜并缓存歌曲。\n"
            "有用户活动时会自动暂停，活动结束后继续。\n\n"
            "查看进度：/cachestatus"
        )
        logger.info(f"管理员手动刷新缓存触发 用户={user.id}")
    elif auto_cache_running:
        await update.message.reply_text("🔄 缓存正在进行中，请稍候...\n\n查看进度：/cachestatus")
    else:
        await update.message.reply_text("⚠️ 缓存功能未就绪，请稍后重试。")


# ============================================================
# 重启功能（管理员手动 + 定时自动）
# ============================================================

async def cmd_restart(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """管理员手动重启Render服务（进程退出后Render自动重启）"""
    user = update.effective_user
    if not _is_admin(user.id):
        await update.message.reply_text("⛔ 权限不足。")
        return

    await update.message.reply_text("🔄 正在重启服务，约10秒后恢复...")
    logger.info("管理员触发重启")
    await asyncio.sleep(1)
    os._exit(1)


# ============================================================
# 错误处理
# ============================================================

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    err = context.error
    err_type = type(err).__name__ if err else "None"
    err_msg = str(err) if err else ""
    if update.inline_query:
        q = update.inline_query
        user = q.from_user
        user_label = f"{user.username or user.first_name or user.id}"
        logger.error(f"内联查询错误 用户={user_label}(id={user.id}) 关键词='{q.query}' 类型={err_type} 错误={err_msg}")
    elif update.callback_query:
        cb = update.callback_query
        user = cb.from_user
        user_label = f"{user.username or user.first_name or user.id}"
        logger.error(f"回调错误 用户={user_label}(id={user.id}) data='{cb.data}' 类型={err_type} 错误={err_msg}")
    else:
        logger.error(f"更新 {update} 引发错误: 类型={err_type} 错误={err_msg}")


# ============================================================
# 主入口
# ============================================================

def main():
    # 打印美化启动横幅
    _print_banner()

    # 构建 Application，设置长超时（上传音频需要较长的 write_timeout）
    builder = ApplicationBuilder().token(config.BOT_TOKEN)
    request_kwargs = dict(
        connect_timeout=30.0,
        read_timeout=60.0,
        write_timeout=120.0,
        pool_timeout=30.0,
    )
    if config.PROXY_URL:
        _log_status(f"🌐 使用代理: {config.PROXY_URL}", "info")
        request_kwargs["proxy"] = config.PROXY_URL
    request = HTTPXRequest(**request_kwargs)
    builder = builder.request(request)
    application = builder.build()

    # 命令
    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("help", cmd_help))
    application.add_handler(CommandHandler("play", cmd_play))
    application.add_handler(CommandHandler("music", cmd_music))
    application.add_handler(CommandHandler("playlist", cmd_playlist))
    application.add_handler(CommandHandler("admin", cmd_admin))
    application.add_handler(CommandHandler("addadmin", cmd_add_admin))
    application.add_handler(CommandHandler("removeadmin", cmd_remove_admin))
    application.add_handler(CommandHandler("admins", cmd_list_admins))
    application.add_handler(CommandHandler("stats", cmd_stats))
    application.add_handler(CommandHandler("users", cmd_users))
    application.add_handler(CommandHandler("broadcast", cmd_broadcast))
    application.add_handler(CommandHandler("ban", cmd_ban))
    application.add_handler(CommandHandler("unban", cmd_unban))
    application.add_handler(CommandHandler("banned", cmd_banned))
    application.add_handler(CommandHandler("setwelcome", cmd_setwelcome))
    application.add_handler(CommandHandler("viewwelcome", cmd_viewwelcome))
    application.add_handler(CommandHandler("resetwelcome", cmd_resetwelcome))
    application.add_handler(CommandHandler("cookie", cmd_cookie))
    application.add_handler(CommandHandler("setcookie", cmd_setcookie))
    application.add_handler(CommandHandler("refreshcookie", cmd_refreshcookie))
    application.add_handler(CommandHandler("quality", cmd_quality))
    application.add_handler(CommandHandler("setquality", cmd_setquality))
    application.add_handler(CommandHandler("restart", cmd_restart))
    application.add_handler(CommandHandler("cachetop", cmd_cachetop))
    application.add_handler(CommandHandler("autocache", cmd_autocache))
    application.add_handler(CommandHandler("cachestatus", cmd_cachestatus))
    application.add_handler(CommandHandler("cacheplaylist", cmd_cacheplaylist))
    application.add_handler(CommandHandler("cacheuser", cmd_cacheuser))
    application.add_handler(CommandHandler("playliststop", cmd_playlist_stop))
    application.add_handler(CommandHandler("toggleplaylist", cmd_toggle_playlist))
    application.add_handler(CommandHandler("refreshcache", cmd_refreshcache))

    # 管理员上传 .txt 文件设置 cookie
    application.add_handler(MessageHandler(filters.Document.ALL, handle_admin_document))
    # 管理员直接发送长十六进制文本自动识别为 cookie
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_admin_text))

    # 内联搜索
    application.add_handler(InlineQueryHandler(handle_inline_query))
    application.add_handler(ChosenInlineResultHandler(handle_chosen_inline_result))

    # 按钮回调
    application.add_handler(CallbackQueryHandler(handle_callback))

    # 错误
    application.add_error_handler(error_handler)

    # 配置来源信息（初始化时已从 Database 加载）
    token_source = "数据库" if _db_token else "环境变量"
    admin_source = "数据库" if _db_admin_id else "环境变量"
    cf_source = "数据库" if _db_cf_proxy else "环境变量/默认"
    cookie_source = "数据库" if _db_cookie else "环境变量"

    print(f"🍪 Cookie 来源: {cookie_source} (长度: {len(api.get_cookie())})")
    print(f"🤖 Bot Token 来源: {token_source} (长度: {len(config.BOT_TOKEN)})")
    print(f"👑 管理员 ID 来源: {admin_source} ({config.ADMIN_ID})")
    print(f"🌐 CF代理 来源: {cf_source} ({config.CF_PROXY_URL if config.CF_PROXY_URL else '未配置'})")

    print("✅ Bot 已启动")
    print(f"🎵 音质等级: {db.get_quality()}")
    print(f"💾 数据库: {getattr(config, 'DB_TYPE', 'sqlite')} ({'已连接' if db.enabled else '未连接'})")
    print("=" * 50)

    if config.WEBHOOK_URL:
        webhook_url = f"{config.WEBHOOK_URL.rstrip('/')}/webhook"
        print(f"🌐 Webhook + 音频代理模式")
        print(f"   监听端口: {config.PORT}")
        print(f"   Webhook URL: {webhook_url}")
        print(f"   音频代理: {config.WEBHOOK_URL.rstrip('/')}/audio/<song_id>")
        print("=" * 50)

        async def run_server():
            await application.initialize()
            await application.start()
            await application.bot.set_webhook(webhook_url)

            app = web.Application()

            async def webhook_handler(request):
                global last_user_activity
                if request.can_read_body:
                    try:
                        data = await request.json()
                        update = Update.de_json(data, application.bot)
                        if update:
                            # update_id去重，防止Telegram重试导致重复处理
                            if hasattr(update, 'update_id') and update.update_id:
                                if update.update_id in _processed_update_ids:
                                    return web.Response(text="OK")
                                _processed_update_ids.add(update.update_id)
                                # 只保留最近100个
                                if len(_processed_update_ids) > 100:
                                    _processed_update_ids.clear()

                            # 管理员非播放请求不算用户活动：管理员的管理操作不更新last_user_activity，不影响闲时自动缓存
                            _is_admin_command = False
                            if update.message and update.message.text and update.message.text.startswith('/'):
                                _cmd = update.message.text.split()[0].lower()
                                _admin_commands = ['/admin', '/addadmin', '/removeadmin', '/admins', '/stats', '/users',
                                                   '/broadcast', '/ban', '/unban', '/banned', '/setwelcome', '/viewwelcome',
                                                   '/resetwelcome', '/cookie', '/setcookie', '/refreshcookie', '/quality',
                                                   '/setquality', '/restart', '/cachetop', '/autocache', '/cachestatus',
                                                   '/cacheplaylist', '/cacheuser', '/playliststop', '/refreshcache',
                                                   '/pauseall', '/resumeall']
                                if _cmd in _admin_commands and _is_admin(update.effective_user.id):
                                    _is_admin_command = True
                                    logger.debug(f"管理员命令 {_cmd} 不计入用户活动")

                            if not _is_admin_command:
                                last_user_activity = time.time()

                            await application.update_queue.put(update)
                    except Exception as e:
                        logger.error(f"Webhook处理失败: {e}")
                return web.Response(text="OK")

            async def health_handler(request):
                return web.Response(text="OK")

            app.router.add_post("/webhook", webhook_handler)
            app.router.add_get("/audio/{song_id}", audio_proxy_handler)
            app.router.add_get("/", health_handler)

            runner = web.AppRunner(app)
            await runner.setup()
            site = web.TCPSite(runner, "0.0.0.0", config.PORT)
            await site.start()
            print("✅ 服务器已启动，等待请求...")

            # 后台任务：每24小时自动刷新 cookie
            async def _daily_refresh():
                while True:
                    await asyncio.sleep(24 * 3600)
                    try:
                        old = api.get_cookie()
                        new = await asyncio.to_thread(api.refresh_cookie)
                        if new and new != old:
                            db.set_cookie(new)
                            api.update_cookie(new)
                            logger.info("Cookie 已自动刷新")
                            await _notify_all_admins(application, "🔄 网易云 Cookie 已自动刷新成功")
                    except Exception as e:
                        logger.error(f"定时刷新失败: {e}")

            asyncio.create_task(_daily_refresh())

            # 每小时检测cookie是否过期，每次检测后都把结果发送给管理员
            # 断点续检：记录上次检测时间到Redis，服务重启后继续之前的周期
            async def _hourly_cookie_check():
                import time as _time
                # 读取上次检测时间
                last_check_str = db._exec("GET", "bot:last_cookie_check") if db.enabled else None
                last_check = int(last_check_str) if last_check_str else 0
                now = _time.time()
                elapsed = now - last_check if last_check > 0 else 999999

                if elapsed >= 3600:
                    # 距离上次检测超过1小时，2分钟后立即检测
                    wait_time = 120
                    logger.info(f"Cookie检测：距离上次检测{int(elapsed)}秒，{wait_time}秒后检测")
                else:
                    # 距离上次检测不足1小时，等待剩余时间
                    wait_time = 3600 - int(elapsed)
                    logger.info(f"Cookie检测：距离上次检测{int(elapsed)}秒，{wait_time}秒后继续检测周期")

                await asyncio.sleep(wait_time)

                while True:
                    try:
                        logger.info("Cookie状态检测：开始检测...")
                        is_valid = await asyncio.to_thread(api.check_cookie_valid)
                        # 记录本次检测时间
                        if db.enabled:
                            db._exec("SET", "bot:last_cookie_check", str(int(_time.time())))
                        if is_valid:
                            logger.info("Cookie状态检测：有效 ✅")
                            await _notify_all_admins(
                                application,
                                "✅ 网易云 Cookie 状态检测：有效\n\n"
                                "歌曲搜索和播放功能正常。"
                            )
                        else:
                            logger.warning("Cookie状态检测：已过期或无效 ❌，通知所有管理员")
                            await _notify_all_admins(
                                application,
                                "🚨 网易云 Cookie 已过期或无效！\n\n"
                                "歌曲搜索和播放可能无法正常工作。\n"
                                "请尽快更新：\n"
                                "1. /refreshcookie — 尝试自动刷新\n"
                                "2. /setcookie <值> — 手动设置\n"
                                "3. 直接发送 MUSIC_U value\n\n"
                                "查看状态：/cookie"
                            )
                    except Exception as e:
                        logger.error(f"Cookie状态检测异常: {e}", exc_info=True)
                        # 异常也记录检测时间，避免重启后立即重复检测
                        if db.enabled:
                            db._exec("SET", "bot:last_cookie_check", str(int(_time.time())))
                        await _notify_all_admins(
                            application,
                            f"⚠️ Cookie 状态检测异常：{e}\n\n"
                            "可能是网络问题，将在下小时重试。\n"
                            "手动查看状态：/cookie"
                        )
                    # 每小时检测一次
                    await asyncio.sleep(3600)

            asyncio.create_task(_hourly_cookie_check())
            logger.info("Cookie状态检测任务已启动（断点续检，根据上次检测时间决定等待时长）")

            # 启动时恢复未完成的歌单播放（断点续播）
            async def _resume_active_playlists():
                await asyncio.sleep(60)  # 等待服务完全启动
                try:
                    active_users = db.get_active_playlist_users()
                    if not active_users:
                        logger.info("歌单续播：无未完成的歌单播放")
                        return
                    logger.info(f"歌单续播：发现{len(active_users)}个未完成的歌单播放，开始恢复")
                    for user_id in active_users:
                        try:
                            data = db.get_active_playlist(user_id)
                            if not data:
                                db.remove_active_playlist(user_id)
                                continue
                            playlist_id = data.get("playlist_id")
                            songs = data.get("songs", [])
                            current_index = data.get("current_index", 0)
                            total = data.get("total", len(songs))
                            if not songs or current_index >= total:
                                db.remove_active_playlist(user_id)
                                continue
                            # 从断点处继续播放
                            remaining = songs[current_index:]
                            logger.info(f"歌单续播：用户={user_id} 歌单={playlist_id} 进度={current_index}/{total} 剩余{len(remaining)}首")
                            asyncio.create_task(_resume_playlist_play(application, user_id, playlist_id, remaining, current_index, total))
                            # 通知用户
                            try:
                                await application.bot.send_message(
                                    chat_id=user_id,
                                    text=f"▶️ 服务已恢复，继续播放歌单（进度{current_index}/{total}，剩余{len(remaining)}首）"
                                )
                            except Exception:
                                pass
                        except Exception as e:
                            logger.error(f"歌单续播失败 用户={user_id}: {e}")
                            db.remove_active_playlist(user_id)
                except Exception as e:
                    logger.error(f"歌单续播任务异常: {e}")

            asyncio.create_task(_resume_active_playlists())

            # 定时自动重启（每8小时），Render检测到进程退出后自动重启
            async def _auto_restart():
                while True:
                    await asyncio.sleep(8 * 3600)
                    try:
                        logger.info("定时自动重启触发")
                        try:
                            await application.bot.send_message(
                                chat_id=config.ADMIN_ID,
                                text="🔄 定时自动重启中，约10秒后恢复..."
                            )
                        except Exception:
                            pass
                        await asyncio.sleep(1)
                        os._exit(1)
                    except Exception as e:
                        logger.error(f"自动重启失败: {e}")

            asyncio.create_task(_auto_restart())

            # 闲时自动缓存核心逻辑（可被闲时检测或立即缓存按钮调用）
            async def _do_auto_cache():
                global auto_cache_running
                if auto_cache_running:
                    return
                auto_cache_running = True
                _cache_start = time.time()
                logger.info(f"闲时自动缓存：检测到空闲（{AUTO_CACHE_IDLE_THRESHOLD}秒无活动），开始缓存今日排行榜")
                try:
                    import datetime
                    _today = datetime.datetime.now().weekday()  # 0=周一, 6=周日
                    _date_str = datetime.datetime.now().strftime("%Y-%m-%d")
                    _day_names = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
                    _per_day = [4, 4, 4, 3, 3, 3, 3]
                    _start = sum(_per_day[:_today])
                    _end = _start + _per_day[_today]
                    _today_playlists = AUTO_CACHE_PLAYLISTS[_start:_end]
                    _redis_key = f"auto_cache:songs:{_date_str}"

                    logger.info(f"闲时缓存：📅 今天{_day_names[_today]}，加载{len(_today_playlists)}个排行榜（第{_start+1}-{_end}个）")

                    # 优先从Redis读取今日歌单（每日获取后存储）
                    all_songs = []
                    if db.enabled:
                        cached = db._exec("GET", _redis_key)
                        if cached:
                            try:
                                all_songs = json.loads(cached)
                                logger.info(f"闲时缓存：✅ 从Redis读取今日歌单{len(all_songs)}首（{_date_str}）")
                            except Exception as e:
                                logger.warning(f"闲时缓存：Redis歌单解析失败: {e}")
                                all_songs = []

                    # Redis无缓存，从API加载并存储
                    if not all_songs:
                        seen_ids = set()
                        _pl_loaded = 0
                        for pl_idx, pl_id in enumerate(_today_playlists, 1):
                            # 有用户活动则立即停止加载排行榜，优先处理用户请求
                            if time.time() - last_user_activity < 10:
                                logger.info(f"闲时缓存：⚠️ 检测到用户活动，停止加载排行榜（已加载{_pl_loaded}/{len(_today_playlists)}个，共{len(all_songs)}首）")
                                break
                            try:
                                _pl_start = time.time()
                                songs = await asyncio.to_thread(api.get_toplist_songs, pl_id, 100)
                                _pl_time = time.time() - _pl_start
                                if songs:
                                    _new = 0
                                    for s in songs:
                                        if s["id"] not in seen_ids:
                                            seen_ids.add(s["id"])
                                            all_songs.append(s)
                                            _new += 1
                                    _pl_loaded += 1
                                    _pl_name = PLAYLIST_NAMES.get(pl_id, f"未知榜({pl_id})")
                                    logger.info(f"闲时缓存：排行榜[{pl_idx}/{len(_today_playlists)}] {_pl_name}(ID={pl_id}) 获取{len(songs)}首，新增{_new}首（耗时{_pl_time:.1f}s）")
                                else:
                                    _pl_name = PLAYLIST_NAMES.get(pl_id, f"未知榜({pl_id})")
                                    logger.warning(f"闲时缓存：排行榜[{pl_idx}/{len(_today_playlists)}] {_pl_name}(ID={pl_id}) 返回空")
                                await asyncio.sleep(0.5)  # 避免请求过快
                            except Exception as e:
                                _pl_name = PLAYLIST_NAMES.get(pl_id, f"未知榜({pl_id})")
                                logger.warning(f"闲时缓存：排行榜[{pl_idx}/{len(_today_playlists)}] {_pl_name}(ID={pl_id}) 获取失败: {e}")

                        # 加载完成或被打断后，只要有歌曲就存到Redis（下次可直接读取）
                        if all_songs and db.enabled:
                            try:
                                db._exec("SET", _redis_key, json.dumps(all_songs, ensure_ascii=False), "EX", 172800)
                                _status = "全部加载" if _pl_loaded == len(_today_playlists) else f"部分加载({_pl_loaded}/{len(_today_playlists)})"
                                logger.info(f"闲时缓存：💾 今日歌单{len(all_songs)}首已存入Redis（{_date_str}，{_status}，保留2天）")
                            except Exception as e:
                                logger.warning(f"闲时缓存：存入Redis失败: {e}")

                    logger.info(f"闲时缓存：📊 今日歌单共{len(all_songs)}首")

                    if not all_songs:
                        logger.warning("闲时自动缓存：❌ 获取歌单失败，无歌曲可缓存")
                        return

                    # 批量查询已缓存的file_id（1次Redis请求，避免逐个查询的延迟）
                    _all_ids = [s["id"] for s in all_songs]
                    _cached_map = db.get_file_ids_batch(_all_ids) if db.enabled else {}
                    # 读取多次失败的歌曲ID（放弃缓存）
                    _failed_ids = set()
                    if db.enabled:
                        _failed = db._exec("SMEMBERS", "auto_cache:failed")
                        if _failed:
                            _failed_ids = set(int(fid) for fid in _failed)
                    _cached_count = sum(1 for sid in _all_ids if _cached_map.get(sid))
                    _failed_count = sum(1 for sid in _all_ids if sid in _failed_ids)
                    to_cache = [s for s in all_songs if not _cached_map.get(s["id"]) and s["id"] not in _failed_ids]
                    logger.info(f"闲时缓存：歌单共{len(all_songs)}首，已缓存{_cached_count}首，多次失败放弃{_failed_count}首，待缓存{len(to_cache)}首")
                    if not to_cache:
                        logger.info("闲时自动缓存：✅ 今日歌单已全部缓存或放弃，无需处理")
                        # 标记今日已完成，今日不再闲时缓存
                        if db.enabled:
                            db._exec("SET", f"auto_cache:done:{_date_str}", "1", "EX", 86400)
                            logger.info(f"闲时缓存：🏁 今日歌单已处理完毕，标记今日完成（{_date_str}），次日自动开启")
                        return

                    success = 0
                    failed = 0
                    _paused_count = 0
                    _interrupted = False
                    for idx, song in enumerate(to_cache, 1):
                        # 最低优先级：最近10秒有用户活动 或 有内联请求活跃 则暂停
                        _paused = False
                        _pause_reason = ""
                        while time.time() - last_user_activity < 10 or inline_request_active > 0:
                            if not _paused:
                                if inline_request_active > 0:
                                    _pause_reason = f"内联请求活跃({inline_request_active}个)"
                                else:
                                    _pause_reason = "用户活动"
                                logger.info(f"闲时缓存：⏸️ 检测到{_pause_reason}，暂停缓存（当前{idx}/{len(to_cache)}）")
                                _paused = True
                                _paused_count += 1
                            await asyncio.sleep(3)
                        if _paused:
                            logger.info(f"闲时缓存：▶️ {_pause_reason}结束，恢复缓存")
                        # 再次检查开关
                        if not auto_cache_enabled:
                            logger.info("闲时缓存：🔕 自动缓存已关闭，停止")
                            _interrupted = True
                            break

                        try:
                            _song_start = time.time()
                            logger.info(f"闲时缓存 [{idx}/{len(to_cache)}] 🎵 开始处理《{song['name']}》- {song['artist']}")
                            # 闲时缓存：CF反向代理 → Render 二级回退
                            caption = f"♻️ 闲时缓存 {idx}/{len(to_cache)}"
                            success_flag, file_id, proxy_type = await _send_audio_with_fallback(
                                None, 8684066933, song,
                                quality=config.MUSIC_QUALITY,
                                caption=caption,
                                use_cache=False,  # 缓存任务不使用file_id缓存
                                log_prefix=f"闲时缓存 [{idx}/{len(to_cache)}] ",
                                bot=application.bot
                            )
                            if success_flag and file_id:
                                success += 1
                                _total_time = time.time() - _song_start
                                logger.info(f"闲时缓存 [{idx}/{len(to_cache)}] ✅ {song['name']} - {song['artist']} (代理类型={proxy_type}, 总耗时{_total_time:.1f}s)")
                            else:
                                failed += 1
                                logger.info(f"闲时缓存 [{idx}/{len(to_cache)}] ❌ {song['name']} - 所有代理和Render都失败")
                        except asyncio.CancelledError:
                            # 内联请求取消了任务，保存进度并退出
                            logger.info(f"闲时缓存：🛑 被内联请求中断（当前{idx}/{len(to_cache)}，成功{success}，失败{failed}）")
                            _interrupted = True
                            raise  # 重新抛出，让任务真正结束
                        except Exception as e:
                            failed += 1
                            logger.warning(f"闲时缓存 [{idx}/{len(to_cache)}] ❌ {song['name']} - 异常: {e}")

                        # 最低优先级：每首之间间隔3秒，有用户活动时暂停更久
                        await asyncio.sleep(3)

                    _total_time = time.time() - _cache_start
                    logger.info(f"闲时自动缓存完成：✅ 成功{success}首，❌ 失败{failed}首，⏸️ 暂停{_paused_count}次，总耗时{_total_time:.1f}s")
                    # 正常完成（未被中断）则标记今日完成，今日不再闲时缓存
                    if not _interrupted and db.enabled:
                        db._exec("SET", f"auto_cache:done:{_date_str}", "1", "EX", 86400)
                        logger.info(f"闲时缓存：🏁 今日歌单已处理完毕，标记今日完成（{_date_str}），次日自动开启")
                except Exception as e:
                    logger.error(f"闲时自动缓存异常: {e}")
                finally:
                    auto_cache_running = False

            # ============================================================
            # 自动更新 Webhook URL（监控 Cloudflare Tunnel 地址变化）
            # ============================================================
            _current_webhook_url = config.WEBHOOK_URL  # 记录当前 URL，用于检测变化

            def _get_cf_tunnel_url() -> str:
                """从 Cloudflare Tunnel 日志文件中读取当前公网地址"""
                import re
                log_files = ["cf_tunnel_err.log", "cf_tunnel.log"]
                base_dir = os.path.dirname(os.path.abspath(__file__))
                for log_file in log_files:
                    log_path = os.path.join(base_dir, log_file)
                    if not os.path.exists(log_path):
                        continue
                    try:
                        with open(log_path, "r", encoding="utf-8", errors="ignore") as f:
                            content = f.read()
                        # 匹配 https://xxx.trycloudflare.com
                        matches = re.findall(r"https://[a-zA-Z0-9-]+\.trycloudflare\.com", content)
                        if matches:
                            return matches[-1]  # 返回最后一个（最新的）
                    except Exception:
                        continue
                return ""

            def _update_env_webhook_url(new_url: str):
                """更新 .env 文件中的 WEBHOOK_URL"""
                env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
                if not os.path.exists(env_path):
                    return
                try:
                    with open(env_path, "r", encoding="utf-8") as f:
                        content = f.read()
                    import re
                    content = re.sub(r"WEBHOOK_URL=.*", f"WEBHOOK_URL={new_url}", content)
                    with open(env_path, "w", encoding="utf-8") as f:
                        f.write(content)
                    logger.info(f"已更新 .env 中的 WEBHOOK_URL 为: {new_url}")
                except Exception as e:
                    logger.warning(f"更新 .env WEBHOOK_URL 失败: {e}")

            async def _auto_update_webhook_url():
                """后台任务：每30秒检查 Cloudflare Tunnel 地址，变化时自动更新 webhook"""
                nonlocal _current_webhook_url
                await asyncio.sleep(10)  # 启动后等待10秒再开始检查
                while True:
                    try:
                        cf_url = _get_cf_tunnel_url()
                        if cf_url and cf_url != _current_webhook_url:
                            logger.info(f"🔄 检测到 Cloudflare Tunnel 地址变化: {_current_webhook_url} -> {cf_url}")
                            # 更新 config 和环境变量
                            config.WEBHOOK_URL = cf_url
                            os.environ["WEBHOOK_URL"] = cf_url
                            _current_webhook_url = cf_url
                            # 更新 .env 文件
                            _update_env_webhook_url(cf_url)
                            # 重新设置 Telegram webhook
                            new_webhook = f"{cf_url.rstrip('/')}/webhook"
                            try:
                                await application.bot.set_webhook(new_webhook)
                                logger.info(f"✅ Telegram Webhook 已更新为: {new_webhook}")
                                await _notify_all_admins(
                                    application,
                                    f"🔄 Webhook URL 已自动更新\n\n"
                                    f"新地址: {cf_url}\n"
                                    f"音频代理: {cf_url}/audio/歌曲ID"
                                )
                            except Exception as e:
                                logger.error(f"更新 Telegram Webhook 失败: {e}")
                    except Exception as e:
                        logger.warning(f"自动更新 Webhook URL 检查异常: {e}")
                    await asyncio.sleep(30)  # 每30秒检查一次

            asyncio.create_task(_auto_update_webhook_url())
            logger.info("🔄 自动更新 Webhook URL 任务已启动（每30秒检查 Cloudflare Tunnel 地址）")

            # 每日0点和12点自动更新闲时缓存歌单：清除今日完成标记，触发重新缓存
            _last_cache_refresh = {"day_hour": ""}  # 记录上次刷新的 日期-小时，避免重复触发

            async def _scheduled_cache_refresh():
                while True:
                    await asyncio.sleep(60)  # 每分钟检查一次
                    if not auto_cache_enabled:
                        continue
                    import datetime
                    now = datetime.datetime.now()
                    current_hour = now.hour
                    # 仅在0点或12点触发
                    if current_hour not in (0, 12):
                        continue
                    day_hour_key = now.strftime("%Y-%m-%d-%H")
                    if _last_cache_refresh["day_hour"] == day_hour_key:
                        continue  # 该小时已触发过，跳过
                    _last_cache_refresh["day_hour"] = day_hour_key

                    _today_str = now.strftime("%Y-%m-%d")
                    logger.info(f"⏰ 定时缓存刷新触发（{current_hour}:00），清除今日完成标记和歌单缓存")

                    # 清除今日完成标记和今日歌单缓存，强制重新获取排行榜
                    if db.enabled:
                        try:
                            db._exec("DEL", f"auto_cache:done:{_today_str}")
                            db._exec("DEL", f"auto_cache:songs:{_today_str}")
                            logger.info("✅ 已清除今日缓存完成标记和歌单数据")
                        except Exception as e:
                            logger.warning(f"清除今日缓存标记失败: {e}")

                    # 通知管理员
                    try:
                        await _notify_all_admins(
                            application,
                            f"⏰ 定时缓存刷新已触发（{current_hour}:00）\n\n"
                            f"已清除今日歌单缓存，将在闲时自动重新缓存排行榜歌曲。"
                        )
                    except Exception:
                        pass

                    # 如果当前空闲，立即触发缓存；否则等闲时自动触发
                    if time.time() - last_user_activity >= AUTO_CACHE_IDLE_THRESHOLD and not auto_cache_running:
                        global auto_cache_task
                        auto_cache_task = asyncio.create_task(_do_auto_cache())
                        logger.info("当前空闲，立即开始缓存")
                    else:
                        logger.info("当前有用户活动或正在缓存，等待闲时自动触发")

            asyncio.create_task(_scheduled_cache_refresh())
            logger.info("⏰ 每日0点/12点定时缓存刷新任务已启动")

            # 闲时自动缓存循环：每分钟检测是否空闲，空闲则调用_do_auto_cache
            async def _idle_auto_cache():
                while True:
                    await asyncio.sleep(60)
                    if not auto_cache_enabled:
                        continue
                    if auto_cache_running:
                        continue
                    if time.time() - last_user_activity < AUTO_CACHE_IDLE_THRESHOLD:
                        continue
                    # 今日歌单已缓存完毕则跳过，次日自动开启
                    import datetime
                    _today_str = datetime.datetime.now().strftime("%Y-%m-%d")
                    if db.enabled and db._exec("EXISTS", f"auto_cache:done:{_today_str}"):
                        continue
                    global auto_cache_task
                    auto_cache_task = asyncio.create_task(_do_auto_cache())

            # 保存引用，供管理员"立即缓存"按钮调用
            global _do_auto_cache_func
            _do_auto_cache_func = _do_auto_cache

            asyncio.create_task(_idle_auto_cache())

            try:
                while True:
                    await asyncio.sleep(3600)
            except (KeyboardInterrupt, SystemExit):
                pass
            finally:
                await application.stop()
                await application.shutdown()
                await runner.cleanup()

        asyncio.run(run_server())
    else:
        # Long Polling 模式（本地调试）
        print("🔄 Long Polling 模式（未设置 WEBHOOK_URL）")
        print("=" * 50)
        application.run_polling()


if __name__ == "__main__":
    main()
