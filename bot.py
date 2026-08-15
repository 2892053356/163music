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
import re
import time
import asyncio
import logging
import hashlib
import requests
from datetime import datetime

from telegram import (
    Update,
    InlineQueryResultArticle,
    InlineQueryResultAudio,
    InputTextMessageContent,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    InlineQueryHandler,
    CallbackQueryHandler,
    ContextTypes,
)
from telegram.request import HTTPXRequest

import config
from netease_api import NeteaseAPI

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

# ============================================================
# 内存数据存储（用户列表、封禁列表、欢迎语、统计）
# Render 等云平台文件系统临时，不持久化到文件
# ============================================================

bot_data = {
    "users": [],
    "banned": [],
    "welcome_message": config.DEFAULT_WELCOME,
    "stats": {"total_searches": 0, "total_plays": 0},
}


def _save_data(data: dict):
    """空操作：云部署不写本地文件"""
    pass


def _register_user(user_id: int):
    """记录用户（去重）"""
    uid = str(user_id)
    if uid not in bot_data["users"]:
        bot_data["users"].append(uid)
        _save_data(bot_data)


def _is_banned(user_id: int) -> bool:
    return str(user_id) in bot_data["banned"]


def _is_admin(user_id: int) -> bool:
    return user_id == config.ADMIN_ID


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


def _tag_mp3(audio_bytes: io.BytesIO, song: dict) -> io.BytesIO:
    """给MP3写入ID3标签（标题、艺术家、专辑），确保Telegram显示正确信息"""
    try:
        from mutagen.mp3 import MP3
        from mutagen.id3 import ID3, TIT2, TPE1, TALB
        audio_bytes.seek(0)
        audio = MP3(audio_bytes)
        if audio.tags is None:
            audio.add_tags()
        audio.tags.add(TIT2(encoding=3, text=[song["name"]]))
        audio.tags.add(TPE1(encoding=3, text=[song["artist"]]))
        audio.tags.add(TALB(encoding=3, text=[song["album"]]))
        audio_bytes.seek(0)
        audio.save(audio_bytes)
        audio_bytes.seek(0)
    except Exception as e:
        logger.warning(f"写入ID3标签失败: {e}")
        audio_bytes.seek(0)
    return audio_bytes


async def _upload_audio(context: ContextTypes.DEFAULT_TYPE, song: dict) -> str:
    """
    下载音频→打ID3标签→上传到Telegram→返回file_id。
    无缓存，每次重新处理。
    """
    # 获取播放地址
    try:
        url = api.get_first_song_url(song["id"], level=config.MUSIC_QUALITY)
    except Exception as e:
        logger.error(f"获取播放地址失败 {song['id']}: {e}")
        return None
    if not url:
        return None

    # 下载（放到线程池避免阻塞）
    try:
        resp = await asyncio.to_thread(requests_get, url, 60)
        audio_bytes = io.BytesIO(resp.content)
    except Exception as e:
        logger.error(f"下载音频失败 {song['id']}: {e}")
        return None

    # 打ID3标签
    audio_bytes = _tag_mp3(audio_bytes, song)
    filename = f"{song['name']} - {config.MUSIC_QUALITY}.mp3"

    # 上传到管理员私聊获取 file_id，然后删除消息
    try:
        msg = await context.bot.send_audio(
            chat_id=config.ADMIN_ID,
            audio=audio_bytes,
            filename=filename,
            title=song["name"],
            performer=song["artist"],
            disable_notification=True,
        )
        file_id = msg.audio.file_id
        try:
            await context.bot.delete_message(
                chat_id=config.ADMIN_ID, message_id=msg.message_id
            )
        except Exception:
            pass
    except Exception as e:
        logger.error(f"上传音频获取file_id失败 {song['id']}: {e}")
        return None

    return file_id


# ============================================================
# 命令处理
# ============================================================

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    _register_user(user.id)

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

    # 自定义欢迎语优先，否则用默认
    custom_welcome = bot_data.get("welcome_message", "")
    if custom_welcome:
        welcome = custom_welcome.replace("{username}", user.first_name or "朋友")
        await update.message.reply_text(welcome, parse_mode="HTML")
        return

    text = (
        f"👋 你好，{user.first_name}！\n\n"
        "我是网易云音乐机器人，可以帮你搜索并播放音乐。\n\n"
        "📖 <b>使用方法：</b>\n"
        "1️⃣ /music 歌曲名 — 搜索并播放歌曲\n"
        "2️⃣ 内联搜索：在任意聊天输入 <code>@本机器人用户名 歌曲名</code>\n\n"
        "💡 试试：/music 周杰伦 晴天"
    )
    await update.message.reply_text(text, parse_mode="HTML")


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "📖 <b>帮助文档</b>\n\n"
        "🎵 <b>搜索与播放</b>\n"
        "• /music 关键词 — 搜索歌曲\n"
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
    await _do_search(update, context, keyword)


async def _do_search(update: Update, context: ContextTypes.DEFAULT_TYPE, keyword: str):
    """执行搜索并展示结果按钮"""
    status_msg = await update.message.reply_text(f"🔍 正在搜索「{keyword}」...")

    try:
        songs = api.search_songs_simple(keyword, limit=config.SEARCH_RESULTS_LIMIT)
    except Exception as e:
        logger.error(f"搜索失败: {e}")
        await status_msg.edit_text("❌ 搜索失败，请稍后重试。")
        return

    bot_data["stats"]["total_searches"] += 1
    _save_data(bot_data)

    if not songs:
        await status_msg.edit_text(f"😢 没有找到与「{keyword}」相关的歌曲。")
        return

    # 构建按钮列表（每行一首）
    keyboard = []
    for i, song in enumerate(songs):
        label = f"{i+1}. {song['name']} - {song['artist']} ({_fmt_duration(song['duration'])})"
        # callback_data 格式: play:<song_id>
        keyboard.append([
            InlineKeyboardButton(label, callback_data=f"play:{song['id']}")
        ])
    reply_markup = InlineKeyboardMarkup(keyboard)

    await status_msg.edit_text(
        f"✅ 找到以下歌曲（点击播放）：",
        reply_markup=reply_markup,
    )


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
    elif data.startswith("lyric:"):
        song_id = int(data.split(":", 1)[1])
        await _send_lyrics(update, context, song_id)


async def _play_song(update: Update, context: ContextTypes.DEFAULT_TYPE, song_id: int, edit: bool = False):
    """获取歌曲信息，下载音频并发送（带歌词按钮）"""
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

    bot_data["stats"]["total_plays"] += 1
    _save_data(bot_data)

    # 下载音频到内存，自定义文件名
    try:
        if edit:
            await update.callback_query.edit_message_text("📥 正在下载并发送音频...")
        resp = requests_get(url, timeout=60)
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

    caption = _song_caption(song)

    # 仅在私聊中显示歌词按钮
    chat = update.effective_chat
    reply_markup = None
    if chat and chat.type == "private":
        reply_markup = InlineKeyboardMarkup([[
            InlineKeyboardButton("📝 获取歌词", callback_data=f"lyric:{song_id}")
        ]])

    try:
        if edit:
            await context.bot.send_audio(
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
            await context.bot.send_audio(
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


def requests_get(url: str, timeout: int = 60):
    """同步 GET 请求（用于在异步函数中下载文件）"""
    return requests.get(url, timeout=timeout)


async def _send_lyrics(update: Update, context: ContextTypes.DEFAULT_TYPE, song_id: int):
    """获取并发送歌词"""
    query = update.callback_query
    await query.answer("正在获取歌词...")

    try:
        result = api.get_lyric(song_id)
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

    if _is_banned(user.id):
        return

    keyword = query.query.strip()
    if not keyword:
        results = [
            InlineQueryResultArticle(
                id="tip",
                title="输入歌曲名或歌手名开始搜索",
                description="例如：周杰伦 晴天",
                input_message_content=InputTextMessageContent(
                    "🎵 在输入框中继续输入歌曲名即可搜索~"
                ),
            )
        ]
        await query.answer(results, cache_time=1)
        return

    try:
        songs = api.search_songs_simple(keyword, limit=config.INLINE_RESULTS_LIMIT)
    except Exception as e:
        logger.error(f"内联搜索失败: {e}")
        songs = []

    bot_data["stats"]["total_searches"] += 1
    _save_data(bot_data)

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

    # 批量获取所有歌曲的播放地址
    song_ids = [s["id"] for s in songs]
    url_map = {}
    try:
        url_result = api.get_song_url(song_ids, level=config.MUSIC_QUALITY)
        for item in url_result.get("data", []):
            sid = item.get("id")
            u = item.get("url")
            if sid and u:
                url_map[sid] = u
    except Exception as e:
        logger.error(f"批量获取播放地址失败: {e}")

    # 过滤有播放地址的歌曲
    results = []
    valid_songs = [s for s in songs if url_map.get(s["id"])]
    if not valid_songs:
        results.append(
            InlineQueryResultArticle(
                id="no_url",
                title=f"「{keyword}」的歌曲均无法播放",
                description="可能需要VIP或已下架",
                input_message_content=InputTextMessageContent(
                    f"😢 「{keyword}」相关歌曲均无法获取播放地址。"
                ),
            )
        )
        await query.answer(results, cache_time=0, is_personal=True)
        return

    # 限制处理数量，避免超时（最多8首）
    valid_songs = valid_songs[:8]

    # 并发下载+打标签+上传所有歌曲（无缓存，每次重新处理）
    file_id_tasks = [_upload_audio(context, song) for song in valid_songs]
    file_ids = await asyncio.gather(*file_id_tasks, return_exceptions=True)

    bot_username = context.bot.username or ""
    via_line = f"\n\n🤖 via @{bot_username}" if bot_username else ""

    results = []
    for song, file_id in zip(valid_songs, file_ids):
        if isinstance(file_id, Exception) or not file_id:
            continue

        caption = (
            f"🎵 <b>{song['name']}</b>\n"
            f"👤 {song['artist']}\n"
            f"💿 {song['album']}"
            f"{via_line}"
        )

        results.append(
            InlineQueryResultAudio(
                id=str(song["id"]),
                audio_url=file_id,
                title=song["name"],
                performer=song["artist"],
                audio_duration=song["duration"] // 1000 if song["duration"] else None,
                caption=caption,
                parse_mode="HTML",
            )
        )

    if not results:
        results.append(
            InlineQueryResultArticle(
                id="upload_fail",
                title=f"「{keyword}」音频处理失败",
                description="请稍后重试",
                input_message_content=InputTextMessageContent(
                    f"😢 「{keyword}」音频处理失败，请稍后重试。"
                ),
            )
        )

    await query.answer(results, cache_time=0, is_personal=True)


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
        "📢 /broadcast 消息 — 向所有用户广播消息\n"
        "🚫 /ban 用户ID — 封禁用户\n"
        "✅ /unban 用户ID — 解封用户\n"
        "📋 /banned — 查看封禁列表\n\n"
        "📝 <b>欢迎语设置</b>\n"
        "✏️ /setwelcome 文本 — 设置欢迎语（支持HTML、{username}）\n"
        "👁 /viewwelcome — 查看当前欢迎语\n"
        "🔄 /resetwelcome — 恢复默认欢迎语"
    )
    await update.message.reply_text(text, parse_mode="HTML")


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not _is_admin(user.id):
        await update.message.reply_text("⛔ 权限不足。")
        return

    text = (
        "📊 <b>机器人统计</b>\n\n"
        f"👥 注册用户数：{len(bot_data['users'])}\n"
        f"🚫 封禁用户数：{len(bot_data['banned'])}\n"
        f"🔍 总搜索次数：{bot_data['stats']['total_searches']}\n"
        f"▶️ 总播放次数：{bot_data['stats']['total_plays']}\n"
        f"🎵 当前音质：{config.MUSIC_QUALITY}"
    )
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

    for uid in bot_data["users"]:
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
    if target_id not in bot_data["banned"]:
        bot_data["banned"].append(target_id)
        _save_data(bot_data)
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
    if target_id in bot_data["banned"]:
        bot_data["banned"].remove(target_id)
        _save_data(bot_data)
    await update.message.reply_text(f"✅ 已解封用户 {target_id}")


async def cmd_banned(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not _is_admin(user.id):
        await update.message.reply_text("⛔ 权限不足。")
        return

    if not bot_data["banned"]:
        await update.message.reply_text("📋 当前没有封禁用户。")
        return

    text = "📋 <b>封禁列表</b>\n\n" + "\n".join(f"• {uid}" for uid in bot_data["banned"])
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

    bot_data["welcome_message"] = new_welcome
    _save_data(bot_data)
    await update.message.reply_text("✅ 欢迎语已更新！预览：", parse_mode="HTML")
    preview = new_welcome.replace("{username}", user.first_name or "朋友")
    await update.message.reply_text(preview, parse_mode="HTML")


async def cmd_viewwelcome(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """管理员查看当前欢迎语"""
    user = update.effective_user
    if not _is_admin(user.id):
        await update.message.reply_text("⛔ 权限不足。")
        return

    current = bot_data.get("welcome_message", "")
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

    bot_data["welcome_message"] = ""
    _save_data(bot_data)
    await update.message.reply_text("✅ 已恢复默认欢迎语。")


# ============================================================
# 错误处理
# ============================================================

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.error(f"更新 {update} 引发错误: {context.error}")


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
    application.add_handler(CommandHandler("admin", cmd_admin))
    application.add_handler(CommandHandler("stats", cmd_stats))
    application.add_handler(CommandHandler("broadcast", cmd_broadcast))
    application.add_handler(CommandHandler("ban", cmd_ban))
    application.add_handler(CommandHandler("unban", cmd_unban))
    application.add_handler(CommandHandler("banned", cmd_banned))
    application.add_handler(CommandHandler("setwelcome", cmd_setwelcome))
    application.add_handler(CommandHandler("viewwelcome", cmd_viewwelcome))
    application.add_handler(CommandHandler("resetwelcome", cmd_resetwelcome))

    # 内联搜索
    application.add_handler(InlineQueryHandler(handle_inline_query))

    # 按钮回调
    application.add_handler(CallbackQueryHandler(handle_callback))

    # 错误
    application.add_error_handler(error_handler)

    print("✅ Bot 已启动，按 Ctrl+C 停止")
    print(f"👑 管理员 ID: {config.ADMIN_ID}")
    print(f"🎵 音质等级: {config.MUSIC_QUALITY}")
    print("=" * 50)

    application.run_polling()


if __name__ == "__main__":
    main()
