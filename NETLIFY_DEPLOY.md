# Netlify Functions 音频代理部署指南

## 简介

使用 Netlify Functions 作为网易云音乐音频代理，替代 Cloudflare Workers，解决音频加载慢的问题。

### 为什么选择 Netlify？

| 对比项 | Cloudflare Workers | Netlify Functions |
|--------|-------------------|-------------------|
| 超时限制 | 无明确超时 | **10秒** |
| CPU限制 | **10ms/请求** ⚠️ | 无明确限制 |
| 冷启动 | ~5ms | ~200ms |
| 免费请求 | 10万/天 | 12.5万/月 |
| 免费流量 | 无限（受CPU限制） | **100GB/月** |
| 大文件处理 | 差（CPU限制） | **好**（10秒超时） |

> Netlify Functions 没有 CPU 时间限制，10秒超时足够下载和转发大部分音频文件（标准音质3-5MB）。

## 部署步骤

### 方式一：Git 集成部署（推荐）

1. **Fork 仓库**
   - Fork https://github.com/XiOuDi/163music 到你的 GitHub

2. **连接 Netlify**
   - 登录 https://app.netlify.com
   - 点击 **Add new site** → **Import an existing project**
   - 选择 GitHub，授权后选择你 Fork 的仓库

3. **配置部署**
   - **Build command**: `echo 'Deploy ready'`（或留空）
   - **Publish directory**: `.`
   - 点击 **Deploy site**

4. **等待部署完成**
   - 首次部署约 1-2 分钟
   - 部署完成后会分配一个域名，如 `https://your-site-name.netlify.app`

5. **测试代理**
   - 访问：`https://your-site-name.netlify.app/audio/1824010970?quality=standard`
   - 应该能听到邓紫棋《泡沫》的音频

### 方式二：Netlify CLI 部署

1. **安装 Netlify CLI**
```bash
npm install -g netlify-cli
```

2. **登录**
```bash
netlify login
```

3. **初始化项目**
```bash
cd 163music_webhook
netlify init
```

4. **部署**
```bash
netlify deploy --prod
```

## 配置 Telegram Bot

部署完成后，将 Netlify 代理 URL 配置到 Bot 的环境变量中。

### Render 部署

在 Render → Environment 中添加或修改：

```
AUDIO_PROXY_URL=https://your-site-name.netlify.app
```

### 本地部署

在 `.env` 文件中添加或修改：

```
AUDIO_PROXY_URL=https://your-site-name.netlify.app
```

> 注意：URL 末尾不要加斜杠 `/`

## API 使用说明

### 请求格式

```
GET https://your-site-name.netlify.app/audio/{song_id}?quality={quality}&name={name}&artist={artist}
```

### 参数说明

| 参数 | 必填 | 说明 | 默认值 |
|------|------|------|--------|
| `song_id` | ✅ | 网易云歌曲ID | - |
| `quality` | ❌ | 音质：standard/higher/exhigh/lossless | standard |
| `name` | ❌ | 歌曲名（用于文件名） | song_{id} |
| `artist` | ❌ | 艺术家（用于文件名） | 空 |

### 示例

```
# 标准音质
https://your-site-name.netlify.app/audio/1824010970

# 较高音质
https://your-site-name.netlify.app/audio/1824010970?quality=higher

# 带文件名
https://your-site-name.netlify.app/audio/1824010970?name=泡沫&artist=邓紫棋
```

## 免费额度说明

| 项目 | 免费额度 | 超出后 |
|------|----------|--------|
| 请求次数 | 12.5万/月 | 计费 $25/百万请求 |
| 运行时间 | 100小时/月 | 计费 $0.02/GB-秒 |
| 出站流量 | 100GB/月 | 计费 $0.05/GB |
| 函数超时 | 10秒 | 付费版可提升到26秒 |
| 并发数 | 无明确限制 | - |

> 对于个人 Telegram Bot，免费额度完全够用。按每天播放100首歌计算，每月约 3000 次请求，3GB 流量。

## 自定义域名（可选）

1. 在 Netlify 站点设置 → Domain management
2. 点击 **Add a domain**
3. 输入你的域名，按提示配置 DNS
4. 配置完成后，使用自定义域名作为代理 URL：
   ```
   AUDIO_PROXY_URL=https://audio.yourdomain.com
   ```

## 常见问题

### Q: 音频还是加载慢？
A: 
1. 检查 Netlify Functions 日志，看是否有超时
2. 尝试降低音质（standard 比 higher 小）
3. 检查网络连接，Netlify 节点主要在海外

### Q: 提示 404 无法获取音频地址？
A: 
1. 歌曲可能需要 VIP 或已下架
2. 检查 song_id 是否正确
3. 查看 Netlify Functions 日志获取详细错误

### Q: 如何查看函数日志？
A: 
- Netlify Dashboard → 你的站点 → Functions → audio-proxy → Logs
- 或使用 CLI：`netlify functions:logs`

### Q: 可以和 Cloudflare Workers 同时使用吗？
A: 可以，但 Bot 只能配置一个 `AUDIO_PROXY_URL`。建议选择速度更快的那个。

### Q: 部署后多久生效？
A: 
- 首次部署：1-2 分钟
- 后续更新：30秒-1分钟
- 函数缓存：24小时（通过 Cache-Control 头控制）

## 文件结构

```
163music_webhook/
├── netlify/
│   └── functions/
│       └── audio-proxy.js    # Netlify Functions 音频代理
├── netlify.toml               # Netlify 部署配置
└── NETLIFY_DEPLOY.md          # 本文档
```

## 技术细节

### weapi 加密
代理函数实现了网易云 weapi 加密协议：
- AES-128-CBC 双重加密
- RSA 加密随机密钥
- 与官方客户端加密方式一致

### 缓存策略
- 响应头设置 `Cache-Control: public, max-age=86400`
- Netlify CDN 会缓存音频 24 小时
- 相同歌曲第二次请求直接从 CDN 返回，速度更快

### 流式传输
- 使用 `isBase64Encoded: true` 返回二进制数据
- Netlify 自动处理 Base64 解码
- 支持 Range 请求（`Accept-Ranges: bytes`）
