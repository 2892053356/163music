# 🎵 网易云音乐 Telegram Bot

支持搜索、播放网易云音乐的 Telegram 机器人，带内联搜索、歌单播放和管理员功能。

[![Latest Release](https://img.shields.io/github/v/release/XiOuDi/163music?label=Latest%20Release&style=for-the-badge)](https://github.com/XiOuDi/163music/releases)
[![Releases](https://img.shields.io/badge/📦_查看所有版本-Releases-blue?style=for-the-badge)](https://github.com/XiOuDi/163music/releases)

https://t.me/xioudi_A       https://t.me/xioudi163_bot

> 🔗 **配套项目**：[cf-music-proxy](https://github.com/XiOuDi/cf-music-proxy) — Cloudflare Workers 音频代理（备用方案，需绑定自定义域名）

## ✨ 功能

### 搜索与播放
- `/music 歌曲名` — 搜索并播放歌曲，带歌词按钮
- `/playlist 歌单ID/链接` — 播放网易云歌单（列表选择 / 全部播放）
- 内联搜索：`@机器人用户名 歌曲名` — 任意对话中直接分享音频
- 音频带 ID3 标签（正确显示标题/艺术家/专辑），MP3 格式

### 缓存与性能
- Telegram file_id 缓存：播放过的歌曲秒发，零带宽
- MGET 批量查询缓存，减少 Redis 网络往返
- 排行榜预热：`/cachetop` 后台缓存热歌榜前100首
- 三级任务优先级：用户单曲 > 歌单播放 > 缓存预热

### 管理员功能
- `/admin` — 管理员面板
- 广播、统计、封禁用户
- 自定义欢迎语（`/setwelcome`、`/viewwelcome`、`/resetwelcome`）
- Cookie 管理（`/cookie`、`/refreshcookie`、`/setcookie`，上传.txt自动识别）
- 用户列表（`/users`，点击ID访问主页）
- 管理员添加/移除（`/addadmin`、`/deladmin`）
- 清除缓存（`/clearcache`）
- 手动重启（`/restart`）+ 每4小时自动重启

## 🚀 Render 部署（Webhook 模式）

### 方式一：render.yaml 一键部署

1. Fork 本仓库到你的 GitHub
2. 进入 [Render Dashboard](https://dashboard.render.com)
3. 点击 **New +** → **Blueprint** → 连接你的仓库
4. Render 会自动读取 `render.yaml`，创建 **Web Service**
5. 填入环境变量（见下表），点击 **Apply**
6. 部署完成后，Render 会自动分配公网 URL（如 `https://your-app.onrender.com`）
7. Bot 会自动设置 webhook，无需手动配置

> Render 会自动设置 `RENDER_EXTERNAL_URL` 和 `PORT` 环境变量，bot 用它们构建 webhook 地址。

### 方式二：手动创建 Web Service

1. Fork 本仓库
2. Render → **New +** → **Web Service**
3. 连接仓库，配置：
   - **Build Command**: `pip install -r requirements.txt`
   - **Start Command**: `python bot.py`
4. 在 **Environment** 中添加环境变量
5. 部署完成

### 环境变量说明

| 变量 | 必填 | 说明 |
|------|------|------|
| `BOT_TOKEN` | ✅ | Telegram Bot Token（@BotFather 获取） |
| `NETEASE_COOKIE` | ✅ | 网易云 MUSIC_U cookie 值 |
| `ADMIN_ID` | ✅ | 管理员用户数字 ID |
| `MUSIC_QUALITY` | ❌ | 音质，默认 `standard` |
| `INLINE_RESULTS_LIMIT` | ❌ | 内联搜索结果数，默认 12 |
| `DEFAULT_WELCOME` | ❌ | 默认欢迎语，支持 `{username}` 变量 |
| `CF_PROXY_URL` | ❌ | Cloudflare Workers 代理地址（备用方案，需绑定自定义域名，见下方说明） |
| `UPSTASH_REDIS_REST_URL` | ✅ | Upstash Redis REST URL |
| `UPSTASH_REDIS_REST_TOKEN` | ✅ | Upstash Redis REST Token |

### Upstash 配置（免费数据库）

1. 注册 [upstash.com](https://upstash.com)
2. 创建 Redis 数据库（选免费版，地区选就近的）
3. 进入数据库详情页，复制 **REST API** 下的 `UPSTASH_REDIS_REST_URL` 和 `UPSTASH_REDIS_REST_TOKEN`
4. 填入 Render 环境变量或本地 `.env`

Upstash 免费版：每日 10000 次命令，256MB 存储，足够个人使用。

### 内联搜索音频代理说明

内联搜索未缓存的歌曲需要提供音频 URL 给 Telegram 下载。当前默认使用 **Render 自带代理端点**（`/audio/{song_id}`），Telegram 可稳定访问 `onrender.com` 域名。

**Cloudflare Workers 代理（可选备用）**：

由于 Telegram 服务器端对 `workers.dev` 域名存在访问限制（`webpage curl failed`），CF 代理默认不启用。如需使用：

1. 部署 [cf-music-proxy](https://github.com/XiOuDi/cf-music-proxy)
2. **必须绑定自定义域名**（如 `proxy.yourdomain.com`），不能直接用 `workers.dev`
3. 在 Render 环境变量中设置 `CF_PROXY_URL=https://proxy.yourdomain.com`
4. 修改 bot.py 内联搜索逻辑启用 CF 代理

## ⚙️ BotFather 设置

1. 给 @BotFather 发送 `/setinline`，选择你的机器人，开启内联模式
2. 可选：发送 `/setcommands` 设置命令菜单

## 📖 使用

- `/music 歌曲名` — 搜索播放
- `/playlist 歌单ID/链接` — 播放歌单
- `@机器人用户名 歌曲名` — 内联搜索分享
- `/admin` — 管理员面板
- `/setwelcome 文本` — 设置欢迎语
- `/help` — 帮助

## ⚠️ 注意

- **Webhook 模式**：Render 部署自动使用 webhook，本地未设置 `WEBHOOK_URL` 时自动降级为 long polling
- Render 免费 Web Service 15分钟无请求会休眠，webhook 消息会自动唤醒实例（首次响应可能有几秒延迟）
- 数据持久化到 Upstash Redis，重启不丢失
- 网易云 cookie 会过期，管理员可通过 `/refreshcookie` 或 `/setcookie` 更新
- 内联搜索首次播放需通过 Render 代理下载，已缓存的歌曲秒发
- Render 不存储音频文件，全部在内存中处理，请求结束即释放
