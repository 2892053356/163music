# 🎵 网易云音乐 Telegram Bot

支持搜索、播放网易云音乐的 Telegram 机器人，带内联搜索、管理员功能、音质设置、Cookie自动刷新和闲时自动缓存。

作者：https://t.me/xioudi_a

bot:https://t.me/xioudi163_bot

## ✨ 功能

### 基础功能
- `/music 歌曲名` — 搜索并播放歌曲（支持下一页）
- 内联搜索：`@XiOuDi163_bot 歌曲名` — 任意对话中直接分享音频（返回8首结果）
- 内联结果"点击在bot中播放"按钮 — 点击自动跳转bot私聊播放
- `/playlist 歌单ID/链接` — 播放网易云歌单
- 音频带 ID3 标签（正确显示标题/艺术家/专辑封面）
- 歌词获取（音频下方按钮）
- MP3格式，标准音质（可切换较高音质）

### 管理员功能
- `/admin` — 管理员面板
- `/stats` — 查看机器人统计
- `/users` — 查看用户列表（点击ID访问主页）
- `/broadcast 消息` — 向所有用户广播消息
- `/ban 用户ID` / `/unban 用户ID` — 封禁/解封用户
- `/setwelcome 文本` — 设置欢迎语（支持HTML、`{username}`）
- `/addadmin 用户ID` / `/removeadmin 用户ID` — 管理员管理（仅主管理员）
- `/restart` — 重启Render服务（每8小时自动重启）

### 音质设置
- `/quality` — 查看当前音质
- `/setquality standard|higher` — 设置音质（普通VIP支持标准/较高）
- 音质设置持久化到Redis，重启后保持

### Cookie管理
- `/cookie` — 查看Cookie状态
- `/refreshcookie` — 手动刷新Cookie
- `/setcookie 值` — 手动设置Cookie
- 支持上传.txt文件或粘贴长文本自动设置
- 每24小时自动刷新Cookie
- 每小时自动检测Cookie有效性，过期/异常时通知所有管理员

### 缓存功能
- Telegram file_id 缓存（避免重复上传音频）
- `/cachetop` — 预热热歌榜前100首缓存
- `/cacheplaylist 歌单ID` — 缓存指定歌单全部歌曲
- `/autocache` — 开关闲时自动缓存
- `/cachestatus` — 查看缓存状态
- 闲时自动缓存排行榜歌曲（24个排行榜分7天缓存）
- 缓存优先级最低，有用户使用时立即暂停

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
| `MUSIC_QUALITY` | ❌ | 音质，默认 `standard`（可运行时通过 /setquality 修改） |
| `DEFAULT_WELCOME` | ❌ | 默认欢迎语，支持 `{username}` 变量 |
| `PROXY_URL` | ❌ | 代理地址，Render 不需要 |
| `UPSTASH_REDIS_REST_URL` | ✅ | Upstash Redis REST URL |
| `UPSTASH_REDIS_REST_TOKEN` | ✅ | Upstash Redis REST Token |

### Upstash 配置（免费数据库）

1. 注册 [upstash.com](https://upstash.com)
2. 创建 Redis 数据库（选免费版，地区选就近的）
3. 进入数据库详情页，复制 **REST API** 下的 `UPSTASH_REDIS_REST_URL` 和 `UPSTASH_REDIS_REST_TOKEN`
4. 填入 Render 环境变量或本地 `.env`

Upstash 免费版：每日 10000 次命令，256MB 存储，足够个人使用。

## 🔗 配套项目

- [cf-music-proxy](https://github.com/XiOuDi/cf-music-proxy) — Cloudflare Worker 音乐代理，备用音频下载方案

## ⚙️ BotFather 设置

1. 给 @BotFather 发送 `/setinline`，选择你的机器人，开启内联模式
2. 可选：发送 `/setcommands` 设置命令菜单

## 📖 使用

- `/music 歌曲名` — 搜索播放
- `@机器人用户名 歌曲名` — 内联搜索分享
- `/playlist 歌单ID` — 播放歌单
- `/admin` — 管理员面板
- `/quality` — 查看音质
- `/setquality higher` — 设置较高音质
- `/setwelcome 文本` — 设置欢迎语
- `/help` — 帮助

## ⚠️ 注意

- **Webhook 模式**：Render 部署自动使用 webhook，本地未设置 `WEBHOOK_URL` 时自动降级为 long polling
- Render 免费 Web Service 15分钟无请求会休眠，webhook 消息会自动唤醒实例（首次响应可能有几秒延迟）
- 数据持久化到 Upstash Redis，重启不丢失
- 网易云 cookie 会自动刷新和检测，过期时通知管理员
- 内联搜索返回8首结果，未缓存歌曲使用代理端点下载
- 每8小时自动重启服务，防止内存泄漏
- 闲时自动缓存排行榜歌曲，有用户使用时立即暂停
