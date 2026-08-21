# Render 部署指南（v7.5 优化版）

## 出站流量优化说明

本版本针对 Render 出站流量进行了专项优化：

| 优化项 | 效果 |
|--------|------|
| Cloudflare Workers 音频代理 | 减少 90%+ 出站流量 |
| file_id 缓存 | 已缓存歌曲零流量 |
| 群组搜索限制15条 | 减少API请求 |
| 5秒去重 | 避免重复发送 |

### 工作原理

```
优化前：Telegram ← Render(下载3-10MB + 上传3-10MB) ← 网易云CDN
优化后：Telegram ← Cloudflare Workers(代理) ← 网易云CDN
         Render 只处理 API 请求（<10KB）
```

## 部署步骤

### 1. 部署 Cloudflare Workers 音频代理（必须）

1. 登录 [Cloudflare Dashboard](https://dash.cloudflare.com/)
2. 进入 Workers & Pages → Create → Create Worker
3. 命名为 `netease-audio-proxy`，点击 Deploy
4. 点击 Edit Code，将 `cloudflare-worker-audio-proxy.js` 的内容粘贴进去
5. 点击 Deploy
6. 记录 Worker URL，例如：`https://netease-audio-proxy.yourname.workers.dev`

### 2. 部署到 Render

1. 登录 [Render Dashboard](https://dashboard.render.com/)
2. 点击 New → Web Service
3. 连接你的 GitHub 仓库 `XiOuDi/163music`
4. 配置：
   - **Name**: `netease-telegram-bot`
   - **Runtime**: Python 3
   - **Build Command**: `pip install --no-cache-dir -r requirements_render.txt`
   - **Start Command**: `python bot_v6.2.py`
   - **Plan**: Free

5. 添加环境变量（Advanced → Add Environment Variables）：

| Key | Value | 说明 |
|-----|-------|------|
| `BOT_TOKEN` | 你的Bot Token | 从 @BotFather 获取 |
| `NETEASE_COOKIE` | 网易云 MUSIC_U | 浏览器开发者工具获取 |
| `ADMIN_ID` | 你的Telegram ID | 从 @userinfobot 获取 |
| `MUSIC_QUALITY` | `standard` | 音质（standard/higher/exhigh） |
| `DB_TYPE` | `upstash` | 数据库类型 |
| `UPSTASH_REDIS_REST_URL` | Upstash URL | 从 upstash.com 获取 |
| `UPSTASH_REDIS_REST_TOKEN` | Upstash Token | 从 upstash.com 获取 |
| `AUDIO_PROXY_URL` | Cloudflare Worker URL | 第1步部署的Worker地址 |
| `PORT` | `10000` | Render默认端口 |

6. 点击 Create Web Service

### 3. 设置 Telegram Webhook

部署完成后，Render 会给你一个 URL，例如：
`https://netease-telegram-bot.onrender.com`

在浏览器中访问：
```
https://api.telegram.org/bot<YOUR_BOT_TOKEN>/setWebhook?url=https://netease-telegram-bot.onrender.com/webhook
```

返回 `{"ok":true,"result":true,"description":"Webhook was set"}` 即成功。

## 验证部署

1. 给 Bot 发送 `/start`，应该收到欢迎消息
2. 发送 `/play 邓紫棋 泡沫`，应该收到搜索结果
3. 点击歌曲，应该收到音频（通过 Cloudflare 代理）
4. 发送 `/admin`，查看管理员面板

## 出站流量监控

在 Render Dashboard 中查看：
- **Metrics** → **Network Egress**：出站流量
- 优化后，每首歌只消耗约 10KB API 请求流量
- 未优化时，每首歌消耗 6-20MB 流量

## 常见问题

### Q: Cloudflare Worker 部署后音频无法播放？
A: 检查 Worker 日志，确认网易云 API 调用是否成功。可能需要更新 Worker 中的 weapi 加密逻辑。

### Q: Render 免费版休眠怎么办？
A: Telegram Webhook 会唤醒服务。首次请求可能需要 10-30 秒冷启动。

### Q: 如何切换回 Render 本地代理？
A: 删除 `AUDIO_PROXY_URL` 环境变量即可，会自动回退到下载+上传模式。

### Q: Upstash 免费额度够吗？
A: Upstash 免费版每月 10000 次命令，100MB 存储，足够个人 Bot 使用。

## 文件说明

| 文件 | 用途 |
|------|------|
| `bot_v6.2.py` | 主程序（v7.5） |
| `config.py` | 配置文件 |
| `database.py` | Upstash 数据库封装 |
| `netease_api.py` | 网易云 API 封装 |
| `render.yaml` | Render 部署配置 |
| `requirements_render.txt` | Render 依赖（精简版） |
| `cloudflare-worker-audio-proxy.js` | Cloudflare Workers 音频代理 |
| `RENDER_DEPLOY.md` | 本文档 |
