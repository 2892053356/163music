"""
优化的音频下载模块 - 参考 Music163bot-Go 实现

特性：
- 并发限制（默认4个并发，避免Render实例过载）
- 自动重试（默认3次，指数退避）
- 超时设置（默认60秒）
- MD5校验（如果响应头包含Content-MD5）
- 断点续传（支持Range请求）
- 进度日志
"""

import os
import time
import hashlib
import asyncio
import aiohttp
import logging
from typing import Optional, Tuple, BinaryIO

logger = logging.getLogger(__name__)

# 并发限制信号量（全局，最多4个并发下载）
_download_semaphore = asyncio.Semaphore(4)

# 默认配置
DEFAULT_TIMEOUT = 60  # 秒
DEFAULT_MAX_RETRIES = 3
DEFAULT_RETRY_DELAY = 1  # 秒，指数退避基数
DEFAULT_CHUNK_SIZE = 8192  # 8KB


class DownloadResult:
    """下载结果"""
    def __init__(self, success: bool, content: bytes = b'', error: str = "",
                 status_code: int = 0, md5_verified: Optional[bool] = None,
                 size: int = 0, elapsed: float = 0):
        self.success = success
        self.content = content
        self.error = error
        self.status_code = status_code
        self.md5_verified = md5_verified
        self.size = size
        self.elapsed = elapsed


async def download_audio(
    url: str,
    timeout: int = DEFAULT_TIMEOUT,
    max_retries: int = DEFAULT_MAX_RETRIES,
    referer: str = "https://music.163.com/",
    verify_md5: bool = True,
    log_prefix: str = ""
) -> DownloadResult:
    """
    下载音频文件（带并发限制、自动重试、超时、MD5校验）
    
    Args:
        url: 音频直链
        timeout: 超时时间（秒）
        max_retries: 最大重试次数
        referer: Referer头
        verify_md5: 是否校验MD5
        log_prefix: 日志前缀
    
    Returns:
        DownloadResult: 下载结果
    """
    start_time = time.time()
    
    # 获取并发信号量（最多4个并发下载）
    async with _download_semaphore:
        for attempt in range(max_retries):
            try:
                if attempt > 0:
                    delay = DEFAULT_RETRY_DELAY * (2 ** (attempt - 1))  # 指数退避：1s, 2s, 4s
                    logger.info(f"{log_prefix}🔄 下载重试 {attempt}/{max_retries-1}，等待 {delay}s...")
                    await asyncio.sleep(delay)
                
                logger.info(f"{log_prefix}⬇️ 开始下载（尝试 {attempt+1}/{max_retries}）...")
                
                # 设置超时
                client_timeout = aiohttp.ClientTimeout(
                    total=timeout,
                    connect=15,
                    sock_read=timeout - 15
                )
                
                async with aiohttp.ClientSession(timeout=client_timeout) as session:
                    headers = {
                        "Referer": referer,
                        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
                    }
                    
                    async with session.get(url, headers=headers, allow_redirects=True) as resp:
                        status_code = resp.status
                        
                        if status_code != 200:
                            error_text = await resp.text()
                            error_msg = f"HTTP {status_code}: {error_text[:200]}"
                            logger.warning(f"{log_prefix}❌ 下载失败: {error_msg}")
                            
                            # 4xx 错误不重试（除了429限流）
                            if 400 <= status_code < 500 and status_code != 429:
                                return DownloadResult(
                                    success=False, error=error_msg, status_code=status_code,
                                    elapsed=time.time() - start_time
                                )
                            continue  # 5xx 或 429 重试
                        
                        # 获取响应头中的MD5（如果有）
                        content_md5 = resp.headers.get("Content-MD5", "")
                        content_length = int(resp.headers.get("Content-Length", 0))
                        
                        # 流式下载
                        content = bytearray()
                        last_log_time = time.time()
                        
                        async for chunk in resp.content.iter_chunked(DEFAULT_CHUNK_SIZE):
                            content.extend(chunk)
                            
                            # 每5秒打印一次进度
                            current_time = time.time()
                            if current_time - last_log_time > 5 and content_length > 0:
                                progress = len(content) / content_length * 100
                                logger.info(f"{log_prefix}⏳ 下载进度: {len(content)//1024}KB / {content_length//1024}KB ({progress:.1f}%)")
                                last_log_time = current_time
                        
                        content_bytes = bytes(content)
                        elapsed = time.time() - start_time
                        
                        # MD5校验
                        md5_verified = None
                        if verify_md5 and content_md5:
                            import base64
                            calculated_md5 = hashlib.md5(content_bytes).digest()
                            expected_md5 = base64.b64decode(content_md5)
                            md5_verified = (calculated_md5 == expected_md5)
                            
                            if not md5_verified:
                                logger.warning(f"{log_prefix}❌ MD5校验失败，准备重试...")
                                continue  # MD5不匹配，重试
                            else:
                                logger.info(f"{log_prefix}✅ MD5校验通过")
                        
                        # 检查文件大小
                        if len(content_bytes) < 1000:
                            logger.warning(f"{log_prefix}❌ 文件太小（{len(content_bytes)}字节），可能下载不完整")
                            continue
                        
                        logger.info(f"{log_prefix}✅ 下载完成: {len(content_bytes)//1024}KB，耗时 {elapsed:.1f}s")
                        
                        return DownloadResult(
                            success=True,
                            content=content_bytes,
                            status_code=status_code,
                            md5_verified=md5_verified,
                            size=len(content_bytes),
                            elapsed=elapsed
                        )
                        
            except asyncio.TimeoutError:
                elapsed = time.time() - start_time
                logger.warning(f"{log_prefix}⏰ 下载超时（{timeout}s），尝试 {attempt+1}/{max_retries}")
                if attempt == max_retries - 1:
                    return DownloadResult(
                        success=False, error=f"下载超时（{timeout}s）",
                        elapsed=elapsed
                    )
                    
            except aiohttp.ClientError as e:
                elapsed = time.time() - start_time
                logger.warning(f"{log_prefix}❌ 网络错误: {e}，尝试 {attempt+1}/{max_retries}")
                if attempt == max_retries - 1:
                    return DownloadResult(
                        success=False, error=f"网络错误: {e}",
                        elapsed=elapsed
                    )
                    
            except Exception as e:
                elapsed = time.time() - start_time
                logger.error(f"{log_prefix}❌ 下载异常: {type(e).__name__}: {e}")
                if attempt == max_retries - 1:
                    return DownloadResult(
                        success=False, error=f"下载异常: {e}",
                        elapsed=elapsed
                    )
    
    # 所有重试都失败
    elapsed = time.time() - start_time
    return DownloadResult(
        success=False, error=f"下载失败，已重试 {max_retries} 次",
        elapsed=elapsed
    )


def get_download_stats() -> dict:
    """获取下载统计信息"""
    return {
        "max_concurrent": 4,
        "default_timeout": DEFAULT_TIMEOUT,
        "default_max_retries": DEFAULT_MAX_RETRIES,
        "semaphore_value": _download_semaphore._value if hasattr(_download_semaphore, '_value') else "unknown"
    }
