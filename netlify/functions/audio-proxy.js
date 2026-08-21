/**
 * 网易云音乐音频代理 - Netlify Functions 版本
 * 
 * 部署：将此文件放在 netlify/functions/ 目录
 * 访问：https://your-site.netlify.app/.netlify/functions/audio-proxy?song_id=xxx&quality=standard
 * 或配置重写后：https://your-site.netlify.app/audio/xxx?quality=standard
 * 
 * 作用：代理网易云音频URL，让Telegram直接从Netlify下载，减少Render出站流量
 * Netlify免费版：10秒超时，12.5万请求/月，100GB流量/月
 */

const crypto = require('crypto');

// ============================================================
// weapi 加密（网易云API加密）
// ============================================================

const AES_KEY = '0CoJUm6Qyw8W8jud';
const AES_IV = Buffer.from('0102030405060708', 'utf8');
const RSA_PUB_KEY = BigInt(
  '00e0b509f6259df8642dbc35662901477df22677ec152b5ff68ace615bb7b725' +
  '152b3ab17a876aea8a5aa76d2e417629ec4ee341f56135fccf695280104e0312' +
  'ecbda92557c93870114af6c9d05c4f7f0c3685b7a46bee255932575cce10b424' +
  'd813cfe4875d3e82047b97ddef52741d546b8e289dc6935b3ece0462db0a22b8e7'
);
const RSA_EXP = 65537n;

function randStr(length = 16) {
  const chars = 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789';
  let result = '';
  for (let i = 0; i < length; i++) {
    result += chars.charAt(Math.floor(Math.random() * chars.length));
  }
  return result;
}

function aesEncrypt(text, key) {
  const cipher = crypto.createCipheriv('aes-128-cbc', Buffer.from(key, 'utf8'), AES_IV);
  let encrypted = cipher.update(text, 'utf8', 'base64');
  encrypted += cipher.final('base64');
  return encrypted;
}

function rsaEncrypt(text) {
  // 反转文本
  const reversed = text.split('').reverse().join('');
  // 转十六进制
  const hex = Buffer.from(reversed, 'utf8').toString('hex');
  // 模幂运算
  const num = BigInt('0x' + hex);
  const result = num ** RSA_EXP % RSA_PUB_KEY;
  // 转256位十六进制字符串
  return result.toString(16).padStart(256, '0');
}

function weapi(data) {
  const text = JSON.stringify(data);
  const secret = randStr(16);
  const params = aesEncrypt(aesEncrypt(text, AES_KEY), secret);
  const encSecKey = rsaEncrypt(secret);
  return { params, encSecKey };
}

// ============================================================
// 网易云 API 调用
// ============================================================

async function getSongUrl(songId, quality = 'standard') {
  const url = 'https://music.163.com/weapi/song/enhance/player/url/v1';
  const data = weapi({
    ids: JSON.stringify([songId]),
    level: quality,
    encodeType: 'mp3'
  });
  
  const params = new URLSearchParams();
  params.append('params', data.params);
  params.append('encSecKey', data.encSecKey);
  
  const response = await fetch(url, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/x-www-form-urlencoded',
      'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
      'Referer': 'https://music.163.com/'
    },
    body: params.toString()
  });
  
  const result = await response.json();
  if (result.data && result.data[0] && result.data[0].url) {
    return result.data[0].url;
  }
  return null;
}

// ============================================================
// Netlify Functions Handler
// ============================================================

exports.handler = async (event, context) => {
  // CORS 头
  const corsHeaders = {
    'Access-Control-Allow-Origin': '*',
    'Access-Control-Allow-Methods': 'GET, OPTIONS',
    'Access-Control-Allow-Headers': '*'
  };
  
  // 预检请求
  if (event.httpMethod === 'OPTIONS') {
    return {
      statusCode: 200,
      headers: corsHeaders
    };
  }
  
  // 健康检查
  if (event.path === '/.netlify/functions/audio-proxy' && !event.queryStringParameters.song_id) {
    // 从路径提取 song_id（如果配置了重写）
    const pathMatch = event.path.match(/\/audio\/(\d+)/);
    if (pathMatch) {
      event.queryStringParameters.song_id = pathMatch[1];
    } else {
      return {
        statusCode: 200,
        headers: { ...corsHeaders, 'Content-Type': 'application/json' },
        body: JSON.stringify({ status: 'ok', message: '网易云音乐音频代理服务运行中' })
      };
    }
  }
  
  const songId = event.queryStringParameters.song_id;
  const quality = event.queryStringParameters.quality || 'standard';
  const name = event.queryStringParameters.name || `song_${songId}`;
  const artist = event.queryStringParameters.artist || '';
  
  if (!songId) {
    return {
      statusCode: 400,
      headers: { ...corsHeaders, 'Content-Type': 'application/json' },
      body: JSON.stringify({ error: '缺少 song_id 参数', code: 400 })
    };
  }
  
  try {
    // 获取音频直链
    const audioUrl = await getSongUrl(songId, quality);
    
    if (!audioUrl) {
      return {
        statusCode: 404,
        headers: { ...corsHeaders, 'Content-Type': 'application/json' },
        body: JSON.stringify({ error: '无法获取音频地址，歌曲可能需要VIP或已下架', code: 404 })
      };
    }
    
    // 下载音频并流式返回
    const audioResponse = await fetch(audioUrl, {
      headers: {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
        'Referer': 'https://music.163.com/'
      }
    });
    
    if (!audioResponse.ok) {
      return {
        statusCode: audioResponse.status,
        headers: { ...corsHeaders, 'Content-Type': 'application/json' },
        body: JSON.stringify({ error: '音频下载失败', code: audioResponse.status })
      };
    }
    
    // 获取音频数据
    const arrayBuffer = await audioResponse.arrayBuffer();
    const buffer = Buffer.from(arrayBuffer);
    
    const filename = `${name}${artist ? ' - ' + artist : ''}.mp3`;
    
    return {
      statusCode: 200,
      headers: {
        ...corsHeaders,
        'Content-Type': 'audio/mpeg',
        'Content-Length': buffer.length.toString(),
        'Content-Disposition': `inline; filename="${encodeURIComponent(filename)}"`,
        'Cache-Control': 'public, max-age=86400', // 缓存24小时
        'Accept-Ranges': 'bytes'
      },
      body: buffer.toString('base64'),
      isBase64Encoded: true
    };
    
  } catch (error) {
    console.error('音频代理错误:', error);
    return {
      statusCode: 500,
      headers: { ...corsHeaders, 'Content-Type': 'application/json' },
      body: JSON.stringify({ error: '服务器内部错误', message: error.message, code: 500 })
    };
  }
};
