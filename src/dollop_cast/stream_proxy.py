import requests
from flask import Blueprint, request, Response, stream_with_context
from urllib.parse import urlparse, urljoin, quote

# 创建 Blueprint
proxy_bp = Blueprint('proxy', __name__)

# 常用 headers
DEFAULT_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
    'Accept': '*/*'
}

def get_headers_for_m3u8(url):
    """
    专门为 m3u8 索引文件生成的 Headers (通常需要防盗链 Referer)
    """
    headers = DEFAULT_HEADERS.copy()
    parsed = urlparse(url)
    if 'youku.com' in parsed.netloc:
        headers['Referer'] = 'https://www.youku.com/'
    elif 'iqiyi.com' in parsed.netloc:
        headers['Referer'] = 'https://www.iqiyi.com/'
    elif 'bilibili.com' in parsed.netloc:
        headers['Referer'] = 'https://www.bilibili.com/'
    elif 'qq.com' in parsed.netloc:
        headers['Referer'] = 'https://v.qq.com/'
    elif 'googlevideo.com' in parsed.netloc:
        headers['Referer'] = 'https://www.youtube.com/'
    else:
        headers['Referer'] = f"{parsed.scheme}://{parsed.netloc}/"
    return headers

@proxy_bp.route('/playlist')
def handle_playlist():
    """
    处理 m3u8 播放列表：下载 -> 改写切片链接 -> 返回
    """
    target_url = request.args.get('url')
    if not target_url: return "Missing URL", 400

    try:
        # 1. 请求原始 m3u8 (带 Referer)
        headers = get_headers_for_m3u8(target_url)
        # 移除 Host 以免出错
        if 'Host' in headers: del headers['Host']
        
        resp = requests.get(target_url, headers=headers, timeout=10)
        if resp.status_code != 200:
            return f"Fetch Error: {resp.status_code}", resp.status_code

        # 2. 解析并改写内容
        content = resp.content.decode('utf-8', errors='ignore')
        new_lines = []
        base_url = target_url.rsplit('/', 1)[0] + '/'
        
        # 获取本机 host (用于构造 segment 接口地址)
        # 注意：这里假设 tray_app 运行在 5000 端口
        # request.host_url 类似 http://192.168.1.5:5000/
        host_url = request.host_url.rstrip('/') 

        for line in content.splitlines():
            line = line.strip()
            if line and not line.startswith('#'):
                # 这是一个切片链接
                # 步骤 A: 补全为绝对路径 (解决腾讯等相对路径问题)
                if not line.startswith('http'):
                    abs_url = urljoin(base_url, line)
                else:
                    abs_url = line
                
                # 步骤 B: 包装成本机 /segment 接口地址
                # 最终变成: http://本机:5000/segment?url=http://...
                encoded_url = quote(abs_url)
                # 使用 segment 路由
                new_line = f"{host_url}/segment?url={abs_url}"
                new_lines.append(new_line)
            else:
                # 其它行 (比如 #EXTINF) 原样保留
                new_lines.append(line)
        
        new_content = '\n'.join(new_lines)
        
        # 3. 返回改写后的 m3u8
        return Response(new_content, mimetype='application/vnd.apple.mpegurl')

    except Exception as e:
        print(f"Playlist Error: {e}")
        return str(e), 500

# 全局 Session (复用连接，大幅提升下载速度)
session = requests.Session()
adapter = requests.adapters.HTTPAdapter(pool_connections=20, pool_maxsize=100)
session.mount('https://', adapter)
session.mount('http://', adapter)

import hashlib
import os
from flask import send_file

# 分片缓存目录
SEGMENT_CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'cache', 'segments')
if not os.path.exists(SEGMENT_CACHE_DIR):
    os.makedirs(SEGMENT_CACHE_DIR)

@proxy_bp.route('/segment')
def handle_segment():
    """
    处理 TS/MP4 切片 (优先读本地缓存，没有则下载并缓存)
    """
    target_url = request.args.get('url')
    if not target_url: return "Missing URL", 400

    # 判断文件类型
    is_large_file = any(target_url.split('?')[0].endswith(ext) for ext in ['.mp4', '.flv', '.mkv', '.mov'])
    
    # MD5 哈希 (仅用于 TS 缓存)
    if not is_large_file:
        file_hash = hashlib.md5(target_url.encode('utf-8')).hexdigest()
        local_path = os.path.join(SEGMENT_CACHE_DIR, f"{file_hash}.ts")
        if os.path.exists(local_path):
            print(f"缓存命中: {local_path}")
            return send_file(local_path)

    try:
        # 基础 UA
        headers = {
            'User-Agent': DEFAULT_HEADERS['User-Agent'],
            'Accept': '*/*'
        }

        # --- 智能 Referer 注入 ---
        parsed = urlparse(target_url)
        domain = parsed.netloc

        if 'qq.com' in domain:
            headers['Referer'] = 'https://v.qq.com/'
        elif 'iqiyi.com' in domain:
            headers['Referer'] = 'https://www.iqiyi.com/'
        elif 'bilibili.com' in domain or 'bilivideo.com' in domain:
            headers['Referer'] = 'https://www.bilibili.com/'
        elif 'youku.com' in domain or 'cibntv.net' in domain: 
             headers['Referer'] = 'https://www.youku.com/'
        elif 'googlevideo.com' in domain:
            headers['Referer'] = 'https://www.youtube.com/'
        elif 'baidupcs.com' in domain or 'pan.baidu.com' in domain:
             # 百度网盘必须使用特定客户端 UA 才能下载，否则报错 errno -6
             headers['User-Agent'] = 'netdisk;P2SP;3.0'
             headers['Referer'] = 'https://pan.baidu.com/'
        if is_large_file:
            # --- 大文件模式：流式透传 + Range 支持 ---
            
            # 1. 透传客户端的 Range 头
            client_range = request.headers.get('Range')
            if client_range:
                headers['Range'] = client_range
            
            # 2. 发起流式请求
            resp = session.get(target_url, headers=headers, stream=True, timeout=15)
            
            # 3. 构造响应头 (透传关键 headers)
            excluded_headers = ['content-encoding', 'transfer-encoding', 'connection', 'host']
            resp_headers = [(name, value) for (name, value) in resp.headers.items()
                            if name.lower() not in excluded_headers]

            # 4. 返回流式响应 (自定义生成器以容忍中断)
            def generate():
                try:
                    for chunk in resp.iter_content(chunk_size=128 * 1024):
                        if chunk:
                            yield chunk
                except (requests.exceptions.ChunkedEncodingError, requests.exceptions.ConnectionError) as e:
                    print(f"流传输中断 (客户端可重试): {e}")
                    # 不抛出异常，让流正常结束，客户端会检测到 Content-Length 不符而重试
                except Exception as e:
                    # 尝试捕获 IncompleteRead (它有时被封装在 ProtocolError 中)
                    import http.client
                    if isinstance(e, http.client.IncompleteRead) or "IncompleteRead" in str(e):
                         print(f"流读取未完成 (IncompleteRead): {e}")
                    else:
                         print(f"流传输未知错误: {e}")

            return Response(
                stream_with_context(generate()),
                status=resp.status_code,
                headers=resp_headers,
                direct_passthrough=True
            )
            
        else:
            # --- TS 分片模式：下载缓存 + Serve ---
            
            # 注意：不带 Stream=True，为了快速下载完
            resp = session.get(target_url, headers=headers, timeout=20)
            
            if resp.status_code == 200:
                with open(local_path, 'wb') as f:
                    f.write(resp.content)
                return send_file(local_path)
            else:
                return f"Remote Error: {resp.status_code}", resp.status_code

    except Exception as e:
        print(f"Segment Error: {e}")
        return str(e), 500

# 兼容旧逻辑的入口 (可选，防止 tray_app 调用出错)
@proxy_bp.route('/proxy')
def handle_proxy_legacy():
    # 简单的分流逻辑，为了兼容之前的 tray_app 代码
    u = request.args.get('url')
    if '.m3u8' in u.split('?')[0]:
        return handle_playlist()
    else:
        return handle_segment()
