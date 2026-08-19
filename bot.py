"""
Telegram 网易云音乐机器人
功能：
  - /start  开始使用
  - /help   帮助
  - /music <关键词>  搜索歌曲（按钮选择播放）
  - 内联搜索：@机器人用户名 <关键词>
  - 管理员：/admin /broadcast /stats /ban /unban
"""

import io
import os
import re
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

import config
from netease_api import NeteaseAPI
from database import db

# ============================================================
# 日志
# ============================================================
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ============================================================
# 全局实例
# ============================================================
api = NeteaseAPI(cookie=config.NETEASE_COOKIE)

# 用户活动时间戳（用于缓存任务优先级控制：有用户请求时暂停缓存）
last_user_activity = 0
inline_last_query = {}  # user_id -> (query, timestamp) 用于内联搜索防抖
_processed_update_ids = set()  # 去重：防止Telegram重试导致重复处理

# 闲时自动缓存状态
auto_cache_running = False  # 是否正在执行自动缓存
auto_cache_enabled = True   # 自动缓存开关
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


# ============================================================
# 工具函数
# ============================================================

def _fmt_duration(ms: int) -> str:
    """毫秒转 分:秒"""
    sec = ms // 1000
    return f"{sec // 60}:{sec % 60:02d}"


def _song_caption(song: dict) -> str:
    """生成歌曲信息文本"""
    return (
        f"🎵 <b>{song['name']}</b>\n"
        f"👤 {song['artist']}\n"
        f"💿 {song['album']}\n"
        f"⏱ {_fmt_duration(song['duration'])}"
    )


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
    quality = request.query.get("quality", config.MUSIC_QUALITY)

    try:
        sid = int(song_id)
    except (ValueError, TypeError):
        return web.Response(status=400, text="Invalid song_id")

    try:
        # 获取播放地址（3秒超时）
        def _get_url(level):
            url_result = api.get_song_url([sid], level=level)
            for item in url_result.get("data", []):
                if item.get("id") == sid and item.get("url"):
                    return item["url"]
            return None

        # 下载音频：每种音质只试1次，连接超时5秒，双音质备用，总时间≤10秒
        audio_content = None
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
            try:
                resp = await asyncio.wait_for(
                    asyncio.to_thread(
                        _download_session.get, play_url,
                        timeout=(8, 25),
                        headers={"Referer": "https://music.163.com/"}
                    ),
                    timeout=33
                )
                if resp.status_code == 200 and resp.content and len(resp.content) > 1000:
                    audio_content = resp.content
                    logger.info(f"代理端点 song_id={sid} 音质={try_quality} 下载成功 大小={len(audio_content)}bytes")
                    break
                logger.warning(f"代理端点 song_id={sid} 音质={try_quality} 状态={resp.status_code} 大小={len(resp.content) if resp.content else 0}")
            except asyncio.TimeoutError:
                logger.warning(f"代理端点 song_id={sid} 音质={try_quality} 下载超时")
            except Exception as e:
                logger.warning(f"代理端点 song_id={sid} 音质={try_quality} 异常: {type(e).__name__}: {e}")

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
        "1️⃣ /music 歌曲名 — 搜索并播放歌曲\n"
        "2️⃣ /playlist 歌单ID/链接 — 播放网易云歌单\n"
        "3️⃣ 内联搜索：在任意聊天输入 <code>@本机器人用户名 歌曲名</code>\n\n"
        "💡 示例：\n"
        "• /music 邓紫棋 泡沫\n"
        "• /playlist 3778678\n"
        "• @XiOuDi163_bot 句号\n\n"
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
        "• /music 关键词 — 搜索歌曲\n"
        "• /playlist 歌单ID/链接 — 播放网易云歌单\n"
        "• 内联模式：@机器人用户名 关键词 — 在任意对话中搜索分享\n\n"
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


async def cmd_music(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if _is_banned(user.id):
        await update.message.reply_text("⛔ 你已被管理员封禁。")
        return

    keyword = " ".join(context.args).strip()
    if not keyword:
        await update.message.reply_text("⚠️ 请输入搜索关键词，例如：/music 周杰伦 晴天")
        return

    _register_user(user.id)
    user_label = f"{user.username or user.first_name or user.id}"
    logger.info(f"/music 用户={user_label}(id={user.id}) 关键词='{keyword}'")
    await _do_search(update, context, keyword)


async def _do_search(update: Update, context: ContextTypes.DEFAULT_TYPE, keyword: str):
    """执行搜索并展示结果按钮（分页）"""
    status_msg = await update.message.reply_text(f"🔍 正在搜索「{keyword}」...")

    try:
        songs = await asyncio.to_thread(api.search_songs_simple, keyword, 50)
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

    await _render_search_page(update, context, 0, status_msg)


async def _render_search_page(update: Update, context: ContextTypes.DEFAULT_TYPE, page: int, status_msg=None):
    """渲染搜索结果的某一页"""
    songs = context.user_data.get("search_songs", [])
    keyword = context.user_data.get("search_keyword", "")
    page_size = 10
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
PLAYLIST_MAX_SONGS = 50


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


async def cmd_playlist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/playlist 歌单ID或链接 — 显示歌单，选择列表播放或全部播放"""
    user = update.effective_user
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
        songs = await asyncio.to_thread(api.get_toplist_songs, playlist_id, PLAYLIST_MAX_SONGS)
    except Exception as e:
        logger.error(f"获取歌单失败: {e}")
        await status.edit_text("❌ 获取歌单失败，请检查歌单ID是否正确。")
        return

    if not songs:
        await status.edit_text("😢 该歌单为空或无法访问。")
        return

    # 存储歌单歌曲到context，供回调使用
    context.user_data[f"playlist_{playlist_id}"] = songs

    # 显示选择模式
    keyboard = [
        [InlineKeyboardButton("📋 列表播放（选歌）", callback_data=f"plist:{playlist_id}:0")],
        [InlineKeyboardButton("▶️ 全部播放（自动发送）", callback_data=f"pall:{playlist_id}")],
    ]
    await status.edit_text(
        f"📀 <b>歌单</b>（共{len(songs)}首，显示前{min(len(songs), PLAYLIST_MAX_SONGS)}首）\n\n请选择播放方式：",
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
    """全部播放：后台逐个发送歌单歌曲"""
    songs = context.user_data.get(f"playlist_{playlist_id}", [])
    if not songs:
        await update.callback_query.edit_message_text("❌ 歌单数据已过期，请重新输入 /playlist")
        return

    chat_id = update.callback_query.message.chat_id
    await update.callback_query.edit_message_text(f"▶️ 开始全部播放 {len(songs)} 首歌曲...")

    async def _send_all():
        success = 0
        failed = 0
        for idx, song in enumerate(songs, 1):
            # 中等优先级：最近3秒有用户活动则暂停（比缓存排行榜高，比用户单曲低）
            while time.time() - last_user_activity < 3:
                await asyncio.sleep(2)
            try:
                cached = db.get_file_id(song["id"])
                caption = _song_caption(song)
                if cached:
                    await context.bot.send_audio(
                        chat_id=chat_id, audio=cached, caption=caption, parse_mode="HTML"
                    )
                else:
                    url = await asyncio.to_thread(api.get_first_song_url, song["id"], config.MUSIC_QUALITY)
                    if not url:
                        failed += 1
                        continue
                    resp = await asyncio.to_thread(requests_get, url, 45)
                    if resp.status_code != 200 or not resp.content or len(resp.content) < 1000:
                        failed += 1
                        continue
                    audio_bytes = io.BytesIO(resp.content)
                    audio_bytes = _tag_mp3(audio_bytes, song)
                    msg = await context.bot.send_audio(
                        chat_id=chat_id,
                        audio=audio_bytes,
                        filename=f"{song['name']}.mp3",
                        title=song["name"],
                        performer=song["artist"],
                        caption=caption,
                        parse_mode="HTML",
                        duration=song["duration"] // 1000 if song["duration"] else None,
                    )
                    if msg and msg.audio and msg.audio.file_id:
                        db.set_file_id(song["id"], msg.audio.file_id)
                success += 1
            except Exception as e:
                logger.warning(f"歌单全部播放失败 {song['name']}: {e}")
                failed += 1
            await asyncio.sleep(1)  # 中等优先级，间隔1秒

        await context.bot.send_message(
            chat_id=chat_id,
            text=f"✅ 歌单播放完成！成功{success}首，失败{failed}首。"
        )

    asyncio.create_task(_send_all())


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


async def _play_song(update: Update, context: ContextTypes.DEFAULT_TYPE, song_id: int, edit: bool = False):
    """获取歌曲信息，下载音频并发送（带歌词按钮）"""
    user = update.effective_user
    user_label = f"{user.username or user.first_name or user.id}"
    logger.info(f"播放歌曲 用户={user_label}(id={user.id}) song_id={song_id}")
    # 获取歌曲详情
    try:
        detail = api.get_song_detail([song_id])
        songs_detail = detail.get("songs", [])
        if not songs_detail:
            if edit:
                await update.callback_query.edit_message_text("❌ 未找到该歌曲信息。")
            else:
                await update.message.reply_text("❌ 未找到该歌曲信息。")
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
        return

    db.incr_play()

    caption = _song_caption(song)

    # 仅在私聊中显示歌词按钮
    chat = update.effective_chat
    reply_markup = None
    if chat and chat.type == "private":
        reply_markup = InlineKeyboardMarkup([[
            InlineKeyboardButton("📝 获取歌词", callback_data=f"lyric:{song_id}")
        ]])

    # 检查 file_id 缓存，命中则直接转发（零带宽、秒发）
    cached_file_id = await asyncio.to_thread(db.get_file_id, song_id)
    if cached_file_id:
        try:
            if edit:
                await context.bot.send_audio(
                    chat_id=update.callback_query.message.chat_id,
                    audio=cached_file_id,
                    caption=caption,
                    parse_mode="HTML",
                    reply_markup=reply_markup,
                )
                await update.callback_query.delete_message()
            else:
                await context.bot.send_audio(
                    chat_id=update.message.chat_id,
                    audio=cached_file_id,
                    caption=caption,
                    parse_mode="HTML",
                    reply_markup=reply_markup,
                )
            return
        except Exception as e:
            logger.warning(f"file_id缓存发送失败，回退下载: {e}")

    # 缓存未命中，下载音频到内存
    try:
        if edit:
            await update.callback_query.edit_message_text("📥 正在下载并发送音频...")
        resp = requests_get(url, timeout=45)
        if resp.status_code != 200 or not resp.content or len(resp.content) < 1000:
            raise Exception(f"下载异常: 状态={resp.status_code} 大小={len(resp.content) if resp.content else 0}")
        audio_bytes = io.BytesIO(resp.content)
        # 写入ID3标签，确保Telegram显示正确的标题和艺术家
        audio_bytes = _tag_mp3(audio_bytes, song)
        filename = f"{song['name']} - {config.MUSIC_QUALITY}.mp3"
    except Exception as e:
        logger.error(f"下载音频失败: {e}")
        if edit:
            await update.callback_query.edit_message_text("❌ 音频下载失败，请稍后重试。")
        else:
            await update.message.reply_text("❌ 音频下载失败，请稍后重试。")
        return

    try:
        if edit:
            msg = await context.bot.send_audio(
                chat_id=update.callback_query.message.chat_id,
                audio=audio_bytes,
                filename=filename,
                title=song["name"],
                performer=song["artist"],
                caption=caption,
                parse_mode="HTML",
                thumbnail=song["cover"] if song["cover"] else None,
                duration=song["duration"] // 1000 if song["duration"] else None,
                reply_markup=reply_markup,
            )
            await update.callback_query.delete_message()
        else:
            msg = await context.bot.send_audio(
                chat_id=update.message.chat_id,
                audio=audio_bytes,
                filename=filename,
                title=song["name"],
                performer=song["artist"],
                caption=caption,
                parse_mode="HTML",
                thumbnail=song["cover"] if song["cover"] else None,
                duration=song["duration"] // 1000 if song["duration"] else None,
                reply_markup=reply_markup,
            )
        # 发送成功后保存 file_id 到缓存
        if msg and msg.audio and msg.audio.file_id:
            await asyncio.to_thread(db.set_file_id, song_id, msg.audio.file_id)
    except Exception as e:
        logger.error(f"发送音频失败: {e}")
        if edit:
            await update.callback_query.edit_message_text(
                "⚠️ 音频发送超时，请稍等片刻查看是否已收到音频；如未收到请重试。"
            )
        else:
            await update.message.reply_text(
                "⚠️ 音频发送超时，请稍等片刻查看是否已收到音频；如未收到请重试。"
            )


# 共享下载Session（连接复用）+ 重试适配器
_download_session = requests.Session()
_download_session.headers.update({"Referer": "https://music.163.com/"})
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
_retry = Retry(total=1, backoff_factor=0.5, status_forcelist=[500, 502, 503, 504], connect=1, read=1)
_download_session.mount("http://", HTTPAdapter(max_retries=_retry))
_download_session.mount("https://", HTTPAdapter(max_retries=_retry))


def requests_get(url: str, timeout: int = 45):
    """同步 GET 请求（连接复用+1次重试+HTTP转HTTPS+Referer头）"""
    if url.startswith("http://"):
        url = "https://" + url[7:]
    # 连接超时10秒（CDN不可达时快速失败），读取超时=总超时-10
    connect_timeout = 10
    read_timeout = max(timeout - connect_timeout, 15)
    return _download_session.get(url, timeout=(connect_timeout, read_timeout))


async def _cache_song_to_admin(context, song, url):
    """下载歌曲并上传到管理员私聊，获取file_id后删除临时消息，保存缓存。返回file_id或None。"""
    cache_admin_id = 8684066933  # 内联缓存专用管理员
    try:
        # 下载
        resp = await asyncio.to_thread(requests_get, url, 45)
        if resp.status_code != 200 or not resp.content or len(resp.content) < 1000:
            logger.warning(f"内联缓存下载失败 song_id={song.get('id')} 状态={resp.status_code} 大小={len(resp.content) if resp.content else 0}")
            return None
        audio_bytes = io.BytesIO(resp.content)
        audio_bytes = _tag_mp3(audio_bytes, song)
        filename = f"{song['name']} - {config.MUSIC_QUALITY}.mp3"

        # 上传到管理员私聊
        msg = await context.bot.send_audio(
            chat_id=cache_admin_id,
            audio=audio_bytes,
            filename=filename,
            title=song["name"],
            performer=song["artist"],
            caption="🔄 内联缓存中...",
            duration=song["duration"] // 1000 if song.get("duration") else None,
        )

        if msg and msg.audio and msg.audio.file_id:
            fid = msg.audio.file_id
            # 保存缓存
            await asyncio.to_thread(db.set_file_id, song["id"], fid)

            # 后台延迟删除管理员临时消息（延迟2秒确保消息完全处理）
            async def _del_temp():
                await asyncio.sleep(2)
                try:
                    await context.bot.delete_message(chat_id=cache_admin_id, message_id=msg.message_id)
                except Exception as del_err:
                    logger.warning(f"删除管理员临时消息失败: {del_err}")
                    # 删除失败则编辑消息标记为已缓存
                    try:
                        await context.bot.edit_message_caption(
                            chat_id=cache_admin_id,
                            message_id=msg.message_id,
                            caption="✅ 已缓存"
                        )
                    except Exception:
                        pass
            asyncio.create_task(_del_temp())

            return fid
        return None
    except Exception as e:
        logger.warning(f"内联缓存失败 {song.get('name')}: {e}")
        return None


async def _send_lyrics(update: Update, context: ContextTypes.DEFAULT_TYPE, song_id: int):
    """获取并发送歌词"""
    query = update.callback_query
    await query.answer("正在获取歌词...")

    try:
        result = await asyncio.to_thread(api.get_lyric, song_id)
        lrc = result.get("lrc", {}).get("lyric", "")
        if not lrc:
            await query.edit_message_reply_markup(None)
            await context.bot.send_message(
                chat_id=query.message.chat_id,
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
                    text=header + chunk,
                    parse_mode="HTML",
                    reply_to_message_id=query.message.message_id if i == 0 else None,
                )
        else:
            await context.bot.send_message(
                chat_id=query.message.chat_id,
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
            text="❌ 获取歌词失败，请稍后重试。",
            reply_to_message_id=query.message.message_id,
        )


# ============================================================
# 内联搜索 (@bot + 关键词)
# ============================================================

async def handle_inline_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.inline_query
    user = query.from_user

    if await asyncio.to_thread(_is_banned, user.id):
        return

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
        return

    # 防抖：纯字母输入
    is_pure_letters = keyword.isascii() and any(c.isalpha() for c in keyword) and not any(c.isspace() for c in keyword) and not any(c.isdigit() for c in keyword)

    # 短输入(≤3字母)直接提示，不搜索
    if is_pure_letters and len(keyword) <= 3:
        results = [
            InlineQueryResultArticle(
                id="typing",
                title=f"继续输入...（当前：{keyword}）",
                description="输入更多字母以搜索歌曲",
                input_message_content=InputTextMessageContent(
                    f"🎵 继续输入更多字母搜索歌曲~"
                ),
            )
        ]
        await query.answer(results, cache_time=1)
        return

    # 纯字母(4-8位)等待800ms防抖，期间有新输入则跳过
    query_time = time.time()
    inline_last_query[user.id] = (keyword, query_time)
    if is_pure_letters and len(keyword) <= 8:
        await asyncio.sleep(0.8)
        latest = inline_last_query.get(user.id)
        if not latest or latest[1] != query_time or latest[0] != keyword:
            logger.info(f"内联防抖 跳过旧查询 '{keyword}'")
            return

    user_label = f"{user.username or user.first_name or user.id}"
    logger.info(f"内联搜索 用户={user_label}(id={user.id}) 关键词='{keyword}'")

    songs = []
    search_start = time.time()
    for attempt in range(1):  # 只尝试1次，总超时2秒，避免查询过期
        try:
            remaining = 2 - (time.time() - search_start)
            if remaining <= 0:
                break
            songs = await asyncio.wait_for(
                asyncio.to_thread(api.search_songs_simple, keyword, 12),
                timeout=remaining
            )
            if songs:
                break
        except asyncio.TimeoutError:
            logger.warning(f"内联搜索 第{attempt+1}次超时")
            break
        except Exception as e:
            logger.warning(f"内联搜索 第{attempt+1}次失败: {e}")
            if attempt < 1:
                await asyncio.sleep(0.5)
    if not songs:
        logger.error("内联搜索失败（3次重试后）")

    # 调试日志：输出搜索关键词和返回结果
    song_names = [f"{s['name']}({s['artist']})" for s in songs[:5]]
    logger.info(f"内联搜索 关键词='{keyword}' 返回{len(songs)}首: {', '.join(song_names)}")

    await asyncio.to_thread(db.incr_search)

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
        return

    # 使用代理端点，无需预获取播放地址，直接用所有搜索结果
    valid_songs = songs[:12]  # 最多12首

    bot_username = context.bot.username or ""
    via_line = f"\n\n🤖 via @{bot_username}" if bot_username else ""

    # 并发获取所有歌曲的file_id缓存（1秒超时，避免Redis慢导致查询过期）
    # 批量获取所有歌曲的file_id缓存（3秒超时，MGET单次查询减少网络往返）
    try:
        file_id_map = await asyncio.wait_for(
            asyncio.to_thread(db.get_file_ids_batch, [s["id"] for s in valid_songs]),
            timeout=3
        )
        cached_count = sum(1 for v in file_id_map.values() if v and str(v).strip())
        logger.info(f"内联搜索 file_id缓存查询: 命中{cached_count}/{len(valid_songs)} map={ {k: (v[:20]+'...' if v else 'None') for k,v in list(file_id_map.items())[:3]} }")
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
                )
            )
        else:
            # 未缓存：使用Render代理端点（Telegram→Render→CDN，稳定可靠）
            cover_param = ""
            _cover = song.get("cover") or song.get("picUrl") or song.get("album_pic") or (song.get("al") or {}).get("picUrl")
            if _cover:
                cover_param = f"&cover={quote(_cover, safe='')}"
            proxy_url = f"{config.WEBHOOK_URL.rstrip('/')}/audio/{song['id']}?name={quote(song['name'])}&artist={quote(song['artist'])}&album={quote(song.get('album', song['name']))}{cover_param}"
            logger.info(f"内联结果 Render代理歌曲 {song['name']} proxy_url长度={len(proxy_url)}")
            results.append(
                InlineQueryResultAudio(
                    id=f"url_{song['id']}",
                    audio_url=proxy_url,
                    title=song["name"],
                    performer=song["artist"],
                    audio_duration=song["duration"] // 1000 if song.get("duration") else None,
                    caption=caption,
                    parse_mode="HTML",
                )
            )

    if not results:
        results.append(
            InlineQueryResultArticle(
                id="no_result",
                title=f"「{keyword}」暂无可用结果",
                description="换个关键词试试，或用 /music 搜索",
                input_message_content=InputTextMessageContent(
                    f"😢 「{keyword}」暂无可用结果。\n💡 试试用 /music {keyword} 搜索播放"
                ),
            )
        )

    try:
        await query.answer(results, cache_time=0, is_personal=True)
    except Exception as e:
        logger.error(f"内联搜索answer失败 用户={user_label}(id={user.id}) 关键词='{keyword}' 结果数={len(results)}: {e}")
        raise


async def handle_chosen_inline_result(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """用户选择内联结果后触发：未缓存歌曲自动缓存"""
    chosen = update.chosen_inline_result
    if not chosen or not chosen.result_id:
        return
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

    user = chosen.from_user
    user_label = f"{user.username or user.first_name or user.id}"
    logger.info(f"内联选择 用户={user_label}(id={user.id}) song_id={song_id} query='{chosen.query}'")

    # 检查是否已缓存
    if await asyncio.to_thread(db.get_file_id, song_id):
        return

    # 获取播放地址并缓存
    try:
        url_result = await asyncio.to_thread(api.get_song_url, [song_id], level=config.MUSIC_QUALITY)
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
        "🔄 <b>服务管理</b>\n"
        "🔁 /restart — 重启Render服务（每4小时自动重启一次）\n"
        "📊 /cachetop — 预热热歌榜前100首缓存\n"
        "📋 /cacheplaylist 歌单ID — 缓存指定歌单全部歌曲\n"
        "♻️ /autocache — 开关闲时自动缓存\n"
        "📊 /cachestatus — 查看缓存状态\n\n"
        "👑 <b>管理员管理</b>（仅主管理员）\n"
        "➕ /addadmin 用户ID — 添加管理员\n"
        "➖ /removeadmin 用户ID — 移除管理员\n"
        "📋 /admins — 查看管理员列表"
    )
    await update.message.reply_text(text, parse_mode="HTML")


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
        f"🎵 当前音质：{config.MUSIC_QUALITY}"
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
            "发送 /music 歌曲名 开始听歌"
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
            logger.info("Cookie 已自动刷新")
            try:
                await context.bot.send_message(
                    chat_id=config.ADMIN_ID,
                    text="🔄 网易云 Cookie 已自动刷新成功"
                )
            except Exception:
                pass
        else:
            logger.info("Cookie 刷新未返回新值，保持当前")
    except Exception as e:
        logger.error(f"Cookie 自动刷新失败: {e}")
        try:
            await context.bot.send_message(
                chat_id=config.ADMIN_ID,
                text=f"⚠️ Cookie 自动刷新失败: {e}\n请使用 /setcookie 手动更新"
            )
        except Exception:
            pass


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
                # 最低优先级：最近5秒有用户活动则暂停
                while time.time() - last_user_activity < 5:
                    await asyncio.sleep(2)

                try:
                    # 获取播放地址
                    url = await asyncio.to_thread(api.get_first_song_url, song["id"], config.MUSIC_QUALITY)
                    if not url:
                        failed += 1
                        continue
                    # 下载
                    resp = await asyncio.to_thread(requests_get, url, 45)
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
        except Exception as e:
            logger.error(f"缓存预热任务失败: {e}")
            await context.bot.send_message(config.ADMIN_ID, f"❌ 缓存预热失败: {e}")

    asyncio.create_task(_do_cache())


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
    # 统计已缓存数量
    try:
        keys = db._exec("KEYS", "cache:file_id:*")
        cached_count = len(keys) if keys else 0
    except Exception:
        cached_count = "未知"
    idle_time = int(time.time() - last_user_activity) if last_user_activity else "从未"
    running = "🔄 正在缓存中" if auto_cache_running else "⏸️ 未在缓存"
    enabled = "✅ 已开启" if auto_cache_enabled else "❌ 已关闭"
    await update.message.reply_text(
        f"📊 缓存状态\n\n"
        f"♻️ 自动缓存：{enabled}\n"
        f"🔄 当前状态：{running}\n"
        f"📚 曲库榜单：{len(AUTO_CACHE_PLAYLISTS)} 个\n"
        f"💾 已缓存歌曲：{cached_count} 首\n"
        f"⏱️ 距上次用户活动：{idle_time}秒\n"
        f"📋 闲时阈值：{AUTO_CACHE_IDLE_THRESHOLD}秒（5分钟）"
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
                # 最低优先级：最近5秒有用户活动则暂停
                while time.time() - last_user_activity < 5:
                    await asyncio.sleep(2)

                try:
                    url = await asyncio.to_thread(api.get_first_song_url, song["id"], config.MUSIC_QUALITY)
                    if not url:
                        failed += 1
                        continue
                    resp = await asyncio.to_thread(requests_get, url, 45)
                    if resp.status_code != 200 or not resp.content or len(resp.content) < 1000:
                        failed += 1
                        continue
                    audio_bytes = io.BytesIO(resp.content)
                    audio_bytes = _tag_mp3(audio_bytes, song)
                    filename = f"{song['name']} - {config.MUSIC_QUALITY}.mp3"
                    msg = await context.bot.send_audio(
                        chat_id=config.ADMIN_ID,
                        audio=audio_bytes,
                        filename=filename,
                        title=song["name"],
                        performer=song["artist"],
                        caption=f"歌单缓存 {idx}/{len(to_cache)}",
                        duration=song["duration"] // 1000 if song.get("duration") else None,
                    )
                    if msg and msg.audio and msg.audio.file_id:
                        db.set_file_id(song["id"], msg.audio.file_id)
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
    print("=" * 50)
    print("  🎵 网易云音乐 Telegram Bot 启动中...")
    print("=" * 50)

    # 构建 Application，设置长超时（上传音频需要较长的 write_timeout）
    builder = ApplicationBuilder().token(config.BOT_TOKEN)
    request_kwargs = dict(
        connect_timeout=30.0,
        read_timeout=60.0,
        write_timeout=120.0,
        pool_timeout=30.0,
    )
    if config.PROXY_URL:
        print(f"🌐 使用代理: {config.PROXY_URL}")
        request_kwargs["proxy"] = config.PROXY_URL
    request = HTTPXRequest(**request_kwargs)
    builder = builder.request(request)
    application = builder.build()

    # 命令
    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("help", cmd_help))
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
    application.add_handler(CommandHandler("restart", cmd_restart))
    application.add_handler(CommandHandler("cachetop", cmd_cachetop))
    application.add_handler(CommandHandler("autocache", cmd_autocache))
    application.add_handler(CommandHandler("cachestatus", cmd_cachestatus))
    application.add_handler(CommandHandler("cacheplaylist", cmd_cacheplaylist))

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

    # 从 Redis 加载 cookie（优先于环境变量，支持运行时更新）
    saved_cookie = db.get_cookie()
    if saved_cookie:
        api.update_cookie(saved_cookie)
        cookie_source = "数据库"
    else:
        cookie_source = "环境变量"
    print(f"🍪 Cookie 来源: {cookie_source} (长度: {len(api.get_cookie())})")

    print("✅ Bot 已启动")
    print(f"👑 管理员 ID: {config.ADMIN_ID}")
    print(f"🎵 音质等级: {config.MUSIC_QUALITY}")
    print(f"💾 Upstash 数据库: {'已连接' if db.enabled else '未配置（数据仅内存）'}")
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
                last_user_activity = time.time()
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
                            logger.info("Cookie 已自动刷新")
                            try:
                                await application.bot.send_message(
                                    chat_id=config.ADMIN_ID,
                                    text="🔄 网易云 Cookie 已自动刷新成功"
                                )
                            except Exception:
                                pass
                    except Exception as e:
                        logger.error(f"定时刷新失败: {e}")

            asyncio.create_task(_daily_refresh())

            # 定时自动重启（每4小时），Render检测到进程退出后自动重启
            async def _auto_restart():
                while True:
                    await asyncio.sleep(4 * 3600)
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

            # 闲时自动缓存热歌榜（最低优先级，有用户活动时暂停）
            async def _idle_auto_cache():
                global auto_cache_running
                while True:
                    await asyncio.sleep(60)  # 每分钟检查一次
                    if not auto_cache_enabled:
                        continue
                    if auto_cache_running:
                        continue
                    # 检查是否空闲（5分钟无用户活动）
                    if time.time() - last_user_activity < AUTO_CACHE_IDLE_THRESHOLD:
                        continue

                    auto_cache_running = True
                    logger.info("闲时自动缓存：检测到空闲，开始缓存多榜单曲库")
                    try:
                        # 从多个排行榜获取歌曲合集
                        all_songs = []
                        seen_ids = set()
                        for pl_id in AUTO_CACHE_PLAYLISTS:
                            # 有用户活动则立即停止加载排行榜，优先处理用户请求
                            if time.time() - last_user_activity < 10:
                                logger.info(f"闲时缓存：检测到用户活动，停止加载排行榜（已加载{len(all_songs)}首）")
                                break
                            try:
                                songs = await asyncio.to_thread(api.get_toplist_songs, pl_id, 100)
                                if songs:
                                    for s in songs:
                                        if s["id"] not in seen_ids:
                                            seen_ids.add(s["id"])
                                            all_songs.append(s)
                                await asyncio.sleep(0.5)  # 避免请求过快
                            except Exception as e:
                                logger.warning(f"闲时缓存 获取排行榜{pl_id}失败: {e}")

                        if not all_songs:
                            logger.warning("闲时自动缓存：获取曲库失败")
                            continue

                        # 过滤已缓存的
                        to_cache = [s for s in all_songs if not db.get_file_id(s["id"])]
                        if not to_cache:
                            logger.info(f"闲时自动缓存：曲库共{len(all_songs)}首已全部缓存")
                            continue

                        logger.info(f"闲时自动缓存：曲库共{len(all_songs)}首（去重），待缓存{len(to_cache)}首")
                        success = 0
                        failed = 0
                        for idx, song in enumerate(to_cache, 1):
                            # 最低优先级：最近10秒有用户活动则暂停
                            while time.time() - last_user_activity < 10:
                                await asyncio.sleep(5)
                            # 再次检查开关
                            if not auto_cache_enabled:
                                break

                            try:
                                url = await asyncio.to_thread(api.get_first_song_url, song["id"], config.MUSIC_QUALITY)
                                if not url:
                                    failed += 1
                                    logger.info(f"闲时缓存 [{idx}/{len(to_cache)}] ❌ {song['name']} - 无播放地址")
                                    continue
                                resp = await asyncio.to_thread(requests_get, url, 45)
                                if resp.status_code != 200 or not resp.content or len(resp.content) < 1000:
                                    failed += 1
                                    logger.info(f"闲时缓存 [{idx}/{len(to_cache)}] ❌ {song['name']} - 下载失败 status={resp.status_code} size={len(resp.content) if resp.content else 0}")
                                    continue
                                audio_bytes = io.BytesIO(resp.content)
                                # _tag_mp3含同步封面下载，放入线程池避免阻塞事件循环
                                audio_bytes = await asyncio.to_thread(_tag_mp3, audio_bytes, song)
                                filename = f"{song['name']} - {config.MUSIC_QUALITY}.mp3"
                                msg = await application.bot.send_audio(
                                    chat_id=8684066933,  # 内联缓存专用管理员
                                    audio=audio_bytes,
                                    filename=filename,
                                    title=song["name"],
                                    performer=song["artist"],
                                    caption=f"♻️ 闲时缓存 {idx}/{len(to_cache)}",
                                    duration=song["duration"] // 1000 if song.get("duration") else None,
                                )
                                if msg and msg.audio and msg.audio.file_id:
                                    db.set_file_id(song["id"], msg.audio.file_id)
                                    success += 1
                                    logger.info(f"闲时缓存 [{idx}/{len(to_cache)}] ✅ {song['name']} - {song['artist']} ({len(resp.content)//1024}KB)")
                                    # 延迟删除临时消息
                                    async def _del(mid):
                                        await asyncio.sleep(3)
                                        try:
                                            await application.bot.delete_message(chat_id=8684066933, message_id=mid)
                                        except Exception:
                                            pass
                                    asyncio.create_task(_del(msg.message_id))
                                else:
                                    failed += 1
                                    logger.info(f"闲时缓存 [{idx}/{len(to_cache)}] ❌ {song['name']} - 上传无file_id")
                            except Exception as e:
                                failed += 1
                                logger.warning(f"闲时缓存 [{idx}/{len(to_cache)}] ❌ {song['name']} - 异常: {e}")

                            # 最低优先级：每首之间间隔3秒，有用户活动时暂停更久
                            await asyncio.sleep(3)

                        logger.info(f"闲时自动缓存完成：成功{success}首，失败{failed}首")
                    except Exception as e:
                        logger.error(f"闲时自动缓存异常: {e}")
                    finally:
                        auto_cache_running = False

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
