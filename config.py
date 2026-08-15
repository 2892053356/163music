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

# 默认欢迎语（可通过管理员 /setwelcome 运行时修改，重启后恢复此默认值）
DEFAULT_WELCOME = os.environ.get("DEFAULT_WELCOME", "")
