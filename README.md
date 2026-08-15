# 🎵 网易云音乐 Telegram Bot

支持搜索、播放网易云音乐的 Telegram 机器人，带内联搜索和管理员功能。

## ✨ 功能

- `/music 歌曲名` — 搜索并播放歌曲
- 内联搜索：`@机器人用户名 歌曲名` — 任意对话中直接分享音频
- 音频带 ID3 标签（正确显示标题/艺术家）
- 歌词获取
- 管理员：广播、统计、封禁、自定义欢迎语

## 🚀 Render 部署

### 方式一：render.yaml 一键部署

1. Fork 本仓库到你的 GitHub
2. 进入 [Render Dashboard](https://dashboard.render.com)
3. 点击 **New +** → **Blueprint** → 连接你的仓库
4. Render 会自动读取 `render.yaml`，填入以下环境变量：
   - `BOT_TOKEN`：Telegram Bot Token
   - `NETEASE_COOKIE`：网易云 MUSIC_U cookie
   - `ADMIN_ID`：管理员用户数字 ID
   - `DEFAULT_WELCOME`（可选）：默认欢迎语
5. 点击 **Apply** 完成部署

### 方式二：手动创建 Worker

1. Fork 本仓库
2. Render → **New +** → **Background Worker**
3. 连接仓库，配置：
   - **Build Command**: `pip install -r requirements.txt`
   - **Start Command**: `python bot.py`
4. 在 **Environment** 中添加上述环境变量
5. 部署完成

### 环境变量说明

| 变量 | 必填 | 说明 |
|------|------|------|
| `BOT_TOKEN` | ✅ | Telegram Bot Token（@BotFather 获取） |
| `NETEASE_COOKIE` | ✅ | 网易云 MUSIC_U cookie 值 |
| `ADMIN_ID` | ✅ | 管理员用户数字 ID |
| `MUSIC_QUALITY` | ❌ | 音质，默认 `standard` |
| `DEFAULT_WELCOME` | ❌ | 默认欢迎语，支持 `{username}` 变量 |
| `PROXY_URL` | ❌ | 代理地址，Render 不需要 |

## ⚙️ BotFather 设置

1. 给 @BotFather 发送 `/setinline`，选择你的机器人，开启内联模式
2. 可选：发送 `/setcommands` 设置命令菜单

## 📖 使用

- `/music 歌曲名` — 搜索播放
- `@机器人用户名 歌曲名` — 内联搜索分享
- `/admin` — 管理员面板
- `/setwelcome 文本` — 设置欢迎语
- `/help` — 帮助

## ⚠️ 注意

- Render 免费 Worker 不会休眠，适合 long polling 机器人
- 数据存储在内存中，重启后用户列表/统计/运行时设置的欢迎语会重置
- 网易云 cookie 会过期，需定期更新环境变量
- 内联搜索首次需要下载上传音频，响应稍慢
