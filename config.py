"""
配置文件 - 全部从环境变量读取，适配 Render 等云平台部署
"""

import os

# Telegram Bot Token
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")

# 网易云 MUSIC_U cookie
NETEASE_COOKIE = os.environ.get("NETEASE_COOKIE", "")

# 管理员用户数字 ID
ADMIN_ID = int(os.environ.get("ADMIN_ID", "0"))

# 歌曲音质等级: standard / higher / exhigh / lossless / hires
MUSIC_QUALITY = os.environ.get("MUSIC_QUALITY", "standard")

# 内联搜索返回结果数量
INLINE_RESULTS_LIMIT = int(os.environ.get("INLINE_RESULTS_LIMIT", "25"))

# 普通搜索返回数量
SEARCH_RESULTS_LIMIT = int(os.environ.get("SEARCH_RESULTS_LIMIT", "10"))

# 代理（留空则直连，Render 不需要）
PROXY_URL = os.environ.get("PROXY_URL", "")

# Cloudflare Workers 代理（用于内联搜索音频代理，解决Render→网易云CDN不稳定）
# 格式: https://your-worker.workers.dev （不要末尾斜杠）
CF_PROXY_URL = os.environ.get("CF_PROXY_URL", "https://cf-music-proxy.l2892053356.workers.dev")

# Webhook 模式配置
# Render 会自动设置 RENDER_EXTERNAL_URL 和 PORT
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", os.environ.get("RENDER_EXTERNAL_URL", ""))
PORT = int(os.environ.get("PORT", "8080"))

# Upstash Redis（数据持久化）
# 在 upstash.com 创建 Redis 后，在 Details 页面获取 REST URL 和 Token
UPSTASH_REDIS_REST_URL = os.environ.get("UPSTASH_REDIS_REST_URL", "")
UPSTASH_REDIS_REST_TOKEN = os.environ.get("UPSTASH_REDIS_REST_TOKEN", "")

# 数据库类型：upstash（云端Redis，默认）或 sqlite（本地）
DB_TYPE = os.environ.get("DB_TYPE", "upstash")

# 默认欢迎语（可通过管理员 /setwelcome 运行时修改，持久化到 Upstash）
DEFAULT_WELCOME = os.environ.get("DEFAULT_WELCOME", """👋 你好，{username}

此bot由西欧帝制作 @XiOuDi_A 
有任何建议可以给我留言

📖 使用方法：
1.   /play 关键词 — 搜索歌曲
2.  内联搜索：在任意聊天输入 @XiOuDi63_bot 歌曲名
3.  /playlist 歌单ID/链接 — 播放网易云歌单（仅限私聊）

例如 
/play 邓紫棋 泡沫
@XiOuDi63_bot 邓紫棋 泡沫""")
