/**
 * 网易云音乐音频代理 - Cloudflare Workers
 * 作用：代理网易云音频URL，让Telegram直接从Cloudflare下载，减少Render出站流量
 * 部署：Cloudflare Workers → 创建服务 → 粘贴此代码 → 部署
 * 使用：https://your-worker.workers.dev/audio/{song_id}?quality=standard
 */

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    
    // 健康检查
    if (url.pathname === '/health') {
      return new Response('OK', { status: 200 });
    }
    
    // 音频代理路由：/audio/{song_id}
    const match = url.pathname.match(/^\/audio\/(\d+)/);
    if (!match) {
      return new Response('Not Found', { status: 404 });
    }
    
    const songId = match[1];
    const quality = url.searchParams.get('quality') || 'standard';
    
    // CORS 头
    const corsHeaders = {
      'Access-Control-Allow-Origin': '*',
      'Access-Control-Allow-Methods': 'GET, OPTIONS',
      'Access-Control-Allow-Headers': '*',
    };
    
    // 预检请求
    if (request.method === 'OPTIONS') {
      return new Response(null, { headers: corsHeaders });
    }
    
    try {
      // 调用网易云 API 获取音频直链
      // 使用 weapi 加密
      const apiUrl = 'https://music.163.com/weapi/song/enhance/player/url/v1';
      
      // weapi 加密（简化版，实际需要完整加密）
      // 这里使用网易云公开的 API 端点
      const params = new URLSearchParams();
      params.append('ids', `[${songId}]`);
      params.append('level', quality);
      params.append('encodeType', 'mp3');
      
      const apiResponse = await fetch(apiUrl, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/x-www-form-urlencoded',
          'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
          'Referer': 'https://music.163.com/',
        },
        body: params.toString(),
      });
      
      const data = await apiResponse.json();
      const songData = data.data?.[0];
      
      if (!songData || !songData.url) {
        return new Response(JSON.stringify({ error: '无法获取音频地址', code: 404 }), {
          status: 404,
          headers: { ...corsHeaders, 'Content-Type': 'application/json' },
        });
      }
      
      // 代理音频流
      const audioResponse = await fetch(songData.url, {
        headers: {
          'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
          'Referer': 'https://music.163.com/',
        },
      });
      
      // 获取歌曲信息用于文件名
      const songName = url.searchParams.get('name') || `song_${songId}`;
      const artist = url.searchParams.get('artist') || '';
      const filename = `${songName}${artist ? ' - ' + artist : ''}.mp3`;
      
      // 返回音频流，添加缓存头
      return new Response(audioResponse.body, {
        status: audioResponse.status,
        headers: {
          ...corsHeaders,
          'Content-Type': audioResponse.headers.get('Content-Type') || 'audio/mpeg',
          'Content-Length': audioResponse.headers.get('Content-Length') || '',
          'Content-Disposition': `inline; filename="${encodeURIComponent(filename)}"`,
          'Cache-Control': 'public, max-age=86400', // 缓存24小时
          'Accept-Ranges': 'bytes',
        },
      });
      
    } catch (error) {
      return new Response(JSON.stringify({ error: error.message, code: 500 }), {
        status: 500,
        headers: { ...corsHeaders, 'Content-Type': 'application/json' },
      });
    }
  },
};
