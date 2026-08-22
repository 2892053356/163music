/**
 * 网易云音乐音频代理 - Cloudflare Workers（稳定版）
 * 作用：代理网易云音频URL，让Telegram直接从Cloudflare下载，减少Render出站流量
 * 
 * 部署：
 * 1. Cloudflare Dashboard → Workers & Pages → Create → Create Worker
 * 2. 粘贴此代码 → Deploy
 * 3. 在 Settings → Variables 添加环境变量：
 *    - NETEASE_COOKIE: 网易云 MUSIC_U cookie（可选，用于获取VIP歌曲）
 *    - UPSTASH_REDIS_REST_URL: Upstash REST URL（可选，用于缓存音频直链）
 *    - UPSTASH_REDIS_REST_TOKEN: Upstash REST Token（可选）
 * 
 * 使用：https://your-worker.workers.dev/audio/{song_id}?quality=standard&name=歌曲名&artist=艺术家
 */

// weapi 加密所需
const CRYPTO = {
  // RSA 公钥指数（十六进制）= 65537
  RSA_PUB_KEY: "010001",
  // RSA 公钥模数（网易云固定）
  RSA_MOD: "00e0b509f6259df8642dbc35662901477df22677ec152b5ff68ace615bb7b725152b3ab17a876aea8a5aa76d2e417629ec4ee341f56135fccf695280104e0312ecbda92557c93870114af6c9d05c4f7f0c3685b7a46bee255932575cce10b424d813cfe4875d3e82047b97ddef52741d546b8e289dc6935b3ece0462db0a22b8e7",
  // 固定第一个AES密钥
  NONCE: "0CoJUm6Qyw8W8jud",
  // 固定IV
  IV: "0102030405060708",
};

/**
 * 生成随机字符串（字母+数字）
 */
function randStr(length = 16) {
  const chars = 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789';
  let result = '';
  const array = new Uint8Array(length);
  crypto.getRandomValues(array);
  for (let i = 0; i < length; i++) {
    result += chars[array[i] % chars.length];
  }
  return result;
}

// 内存缓存（CF Workers 每个实例独立，存活时间不定）
const memoryCache = new Map();
const CACHE_TTL = 180000; // 3分钟

/**
 * 字符串转 Base64
 */
function bufToBase64(buf) {
  let binary = '';
  const bytes = new Uint8Array(buf);
  for (let i = 0; i < bytes.byteLength; i++) {
    binary += String.fromCharCode(bytes[i]);
  }
  return btoa(binary);
}

/**
 * Base64 转字符串
 */
function base64ToBuf(b64) {
  const binary = atob(b64);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i++) {
    bytes[i] = binary.charCodeAt(i);
  }
  return bytes.buffer;
}

/**
 * AES-CBC 加密
 */
async function aesEncrypt(text, key, iv) {
  const keyBuf = await crypto.subtle.importKey(
    "raw",
    new TextEncoder().encode(key),
    { name: "AES-CBC" },
    false,
    ["encrypt"]
  );
  const encrypted = await crypto.subtle.encrypt(
    { name: "AES-CBC", iv: new TextEncoder().encode(iv) },
    keyBuf,
    new TextEncoder().encode(text)
  );
  return bufToBase64(encrypted);
}

/**
 * 模幂运算（快速幂）- 避免 base^exp 产生巨大数
 */
function modPow(base, exp, mod) {
  let result = 1n;
  base = base % mod;
  while (exp > 0n) {
    if (exp % 2n === 1n) {
      result = (result * base) % mod;
    }
    exp = exp / 2n;
    base = (base * base) % mod;
  }
  return result;
}

/**
 * RSA 加密（使用 BigInt 模幂运算）
 */
function rsaEncrypt(text, pubKey, modulus) {
  // 反转文本
  const textReversed = text.split('').reverse().join('');
  // 转换为十六进制
  let hex = '';
  for (let i = 0; i < textReversed.length; i++) {
    hex += textReversed.charCodeAt(i).toString(16).padStart(2, '0');
  }
  // BigInt 模幂加密（使用快速幂，避免巨大数）
  const base = BigInt('0x' + hex);
  const exp = BigInt('0x' + pubKey);
  const mod = BigInt('0x' + modulus);
  const result = modPow(base, exp, mod);
  // 转换为十六进制字符串，补零到256位
  return result.toString(16).padStart(256, '0');
}

/**
 * weapi 加密（与网易云官方实现一致）
 * 1. JSON序列化
 * 2. 第一次AES加密（固定密钥 NONCE）
 * 3. 第二次AES加密（随机16位密钥）
 * 4. RSA加密随机密钥
 */
async function weapiEncrypt(params) {
  const jsonStr = JSON.stringify(params);
  // 生成随机16位密钥
  const secret = randStr(16);
  // 第一次 AES 加密（固定密钥）
  const encText = await aesEncrypt(jsonStr, CRYPTO.NONCE, CRYPTO.IV);
  // 第二次 AES 加密（随机密钥）
  const encText2 = await aesEncrypt(encText, secret, CRYPTO.IV);
  // RSA 加密随机密钥
  const encSecKey = rsaEncrypt(secret, CRYPTO.RSA_PUB_KEY, CRYPTO.RSA_MOD);
  return {
    params: encText2,
    encSecKey: encSecKey
  };
}

/**
 * 从 Upstash 获取缓存的音频直链
 */
async function getCachedAudioUrl(songId, quality, env) {
  if (!env.UPSTASH_REDIS_REST_URL || !env.UPSTASH_REDIS_REST_TOKEN) return null;
  
  const cacheKey = `audio_url:${songId}:${quality}`;
  try {
    const resp = await fetch(`${env.UPSTASH_REDIS_REST_URL}/GET/${encodeURIComponent(cacheKey)}`, {
      headers: { 'Authorization': `Bearer ${env.UPSTASH_REDIS_REST_TOKEN}` }
    });
    const data = await resp.json();
    return data.result || null;
  } catch (e) {
    console.warn('Upstash GET failed:', e.message);
    return null;
  }
}

/**
 * 保存音频直链到 Upstash（10分钟过期）
 */
async function setCachedAudioUrl(songId, quality, url, env) {
  if (!env.UPSTASH_REDIS_REST_URL || !env.UPSTASH_REDIS_REST_TOKEN) return;
  
  const cacheKey = `audio_url:${songId}:${quality}`;
  try {
    await fetch(`${env.UPSTASH_REDIS_REST_URL}/SETEX/${encodeURIComponent(cacheKey)}/600/${encodeURIComponent(url)}`, {
      headers: { 'Authorization': `Bearer ${env.UPSTASH_REDIS_REST_TOKEN}` }
    });
  } catch (e) {
    console.warn('Upstash SET failed:', e.message);
  }
}

/**
 * 从 Upstash 获取 Cookie
 * 注意：Upstash 中存储的是 MUSIC_U 的值，需要包装成完整的 Cookie 字符串
 */
async function getCookie(env) {
  // 优先使用环境变量中的 Cookie（如果是完整的 Cookie 字符串）
  if (env.NETEASE_COOKIE) {
    // 如果已经包含 MUSIC_U=，直接使用；否则包装
    return env.NETEASE_COOKIE.includes('MUSIC_U=') 
      ? env.NETEASE_COOKIE 
      : `MUSIC_U=${env.NETEASE_COOKIE}`;
  }
  
  // 其次从 Upstash 读取（存储的是 MUSIC_U 的值）
  if (env.UPSTASH_REDIS_REST_URL && env.UPSTASH_REDIS_REST_TOKEN) {
    try {
      const cacheKey = 'bot:cookie';
      const resp = await fetch(`${env.UPSTASH_REDIS_REST_URL}/GET/${encodeURIComponent(cacheKey)}`, {
        headers: { 'Authorization': `Bearer ${env.UPSTASH_REDIS_REST_TOKEN}` }
      });
      const data = await resp.json();
      const musicU = data.result;
      if (musicU) {
        // 包装成完整的 Cookie 字符串
        return `MUSIC_U=${musicU}`;
      }
      return null;
    } catch (e) {
      console.warn('Upstash cookie GET failed:', e.message);
    }
  }
  return null;
}

/**
 * 调试端点：检查配置和连接状态
 */
async function debugInfo(env) {
  const info = {
    upstash_configured: !!(env.UPSTASH_REDIS_REST_URL && env.UPSTASH_REDIS_REST_TOKEN),
    cookie_env_configured: !!env.NETEASE_COOKIE,
    upstash_url: env.UPSTASH_REDIS_REST_URL ? env.UPSTASH_REDIS_REST_URL.replace(/\/\/[^@]*@/, '//***@') : null,
  };
  
  // 测试 Upstash 连接
  if (info.upstash_configured) {
    try {
      const resp = await fetch(`${env.UPSTASH_REDIS_REST_URL}/PING`, {
        headers: { 'Authorization': `Bearer ${env.UPSTASH_REDIS_REST_TOKEN}` }
      });
      const data = await resp.json();
      info.upstash_ping = data.result || 'error';
      
      // 读取 Cookie
      const cookie = await getCookie(env);
      info.cookie_from_upstash = cookie ? `已获取 (${cookie.length} 字符)` : '未找到';
      info.cookie_preview = cookie ? cookie.substring(0, 50) + '...' : null;
      info.cookie_format_correct = cookie ? cookie.startsWith('MUSIC_U=') : false;
      
      // 测试 weapi 加密
      try {
        const testEncrypted = await weapiEncrypt({ ids: '[1]', level: 'standard', encodeType: 'mp3' });
        info.weapi_encrypt_test = '成功';
        info.weapi_params_length = testEncrypted.params.length;
        info.weapi_encseckey_length = testEncrypted.encSecKey.length;
      } catch (e) {
        info.weapi_encrypt_test = '失败';
        info.weapi_encrypt_error = e.message;
      }
    } catch (e) {
      info.upstash_error = e.message;
    }
  }
  
  return info;
}

/**
 * 调用网易云 API 获取音频直链
 */
async function getNeteaseAudioUrl(songId, quality, env) {
  const apiUrl = 'https://music.163.com/weapi/song/enhance/player/url/v1';
  
  const params = {
    ids: `[${songId}]`,
    level: quality,
    encodeType: 'mp3'
  };
  
  const encrypted = await weapiEncrypt(params);
  const body = new URLSearchParams();
  body.append('params', encrypted.params);
  body.append('encSecKey', encrypted.encSecKey);
  
  const cookie = await getCookie(env);
  const headers = {
    'Content-Type': 'application/x-www-form-urlencoded',
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Referer': 'https://music.163.com/',
    'Origin': 'https://music.163.com'
  };
  if (cookie) {
    headers['Cookie'] = cookie;
  }
  
  const resp = await fetch(apiUrl, {
    method: 'POST',
    headers: headers,
    body: body.toString()
  });
  
  const data = await resp.json();
  const songData = data.data?.[0];
  
  if (!songData || !songData.url) {
    console.warn('Netease API returned no url:', JSON.stringify(data).substring(0, 200));
    return null;
  }
  
  return songData.url;
}

/**
 * 带超时的 fetch
 */
async function fetchWithTimeout(url, options = {}, timeout = 30000) {
  const controller = new AbortController();
  const timeoutId = setTimeout(() => controller.abort(), timeout);
  try {
    const resp = await fetch(url, { ...options, signal: controller.signal });
    return resp;
  } finally {
    clearTimeout(timeoutId);
  }
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    
    // CORS 头
    const corsHeaders = {
      'Access-Control-Allow-Origin': '*',
      'Access-Control-Allow-Methods': 'GET, OPTIONS',
      'Access-Control-Allow-Headers': '*',
    };
    
    // 健康检查
    if (url.pathname === '/health') {
      return new Response('OK', { status: 200, headers: corsHeaders });
    }
    
    // 调试端点
    if (url.pathname === '/debug') {
      const info = await debugInfo(env);
      return new Response(JSON.stringify(info, null, 2), {
        status: 200,
        headers: { ...corsHeaders, 'Content-Type': 'application/json' },
      });
    }
    
    // 预检请求
    if (request.method === 'OPTIONS') {
      return new Response(null, { headers: corsHeaders });
    }
    
    // 音频代理路由：/audio/{song_id}
    const match = url.pathname.match(/^\/audio\/(\d+)/);
    if (!match) {
      return new Response(JSON.stringify({ error: 'Not Found', path: url.pathname }), {
        status: 404,
        headers: { ...corsHeaders, 'Content-Type': 'application/json' },
      });
    }
    
    const songId = match[1];
    const quality = url.searchParams.get('quality') || 'standard';
    const songName = url.searchParams.get('name') || `song_${songId}`;
    const artist = url.searchParams.get('artist') || '';
    const filename = `${songName}${artist ? ' - ' + artist : ''}.mp3`;
    
    try {
      // 1. 检查内存缓存
      const memKey = `${songId}:${quality}`;
      const memCached = memoryCache.get(memKey);
      if (memCached && Date.now() - memCached.time < CACHE_TTL) {
        console.log(`Memory cache hit: ${songId}`);
        return proxyAudio(memCached.url, filename, corsHeaders);
      }
      
      // 2. 检查 Upstash 缓存
      const cachedUrl = await getCachedAudioUrl(songId, quality, env);
      if (cachedUrl) {
        console.log(`Upstash cache hit: ${songId}`);
        memoryCache.set(memKey, { url: cachedUrl, time: Date.now() });
        return proxyAudio(cachedUrl, filename, corsHeaders);
      }
      
      // 3. 调用网易云 API 获取直链
      console.log(`Fetching from Netease API: ${songId}`);
      const audioUrl = await getNeteaseAudioUrl(songId, quality, env);
      
      if (!audioUrl) {
        return new Response(JSON.stringify({ error: '无法获取音频地址，可能需要VIP或已下架', songId }), {
          status: 404,
          headers: { ...corsHeaders, 'Content-Type': 'application/json' },
        });
      }
      
      // 4. 保存到缓存
      memoryCache.set(memKey, { url: audioUrl, time: Date.now() });
      await setCachedAudioUrl(songId, quality, audioUrl, env);
      
      // 5. 代理音频流
      return proxyAudio(audioUrl, filename, corsHeaders);
      
    } catch (error) {
      console.error('Proxy error:', error);
      return new Response(JSON.stringify({ error: error.message, code: 500 }), {
        status: 500,
        headers: { ...corsHeaders, 'Content-Type': 'application/json' },
      });
    }
  },
};

/**
 * 代理音频流（流式转发）
 */
async function proxyAudio(audioUrl, filename, corsHeaders) {
  const audioResponse = await fetchWithTimeout(audioUrl, {
    headers: {
      'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
      'Referer': 'https://music.163.com/',
    },
  }, 30000);
  
  if (!audioResponse.ok) {
    return new Response(JSON.stringify({ error: `音频源返回 ${audioResponse.status}` }), {
      status: 502,
      headers: { ...corsHeaders, 'Content-Type': 'application/json' },
    });
  }
  
  // 流式返回音频
  return new Response(audioResponse.body, {
    status: audioResponse.status,
    headers: {
      ...corsHeaders,
      'Content-Type': audioResponse.headers.get('Content-Type') || 'audio/mpeg',
      'Content-Length': audioResponse.headers.get('Content-Length') || '',
      'Content-Disposition': `inline; filename="${encodeURIComponent(filename)}"`,
      'Cache-Control': 'public, max-age=86400',
      'Accept-Ranges': 'bytes',
    },
  });
}
