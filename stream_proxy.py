import requests
from flask import Blueprint, request, Response, stream_with_context
from urllib.parse import urlparse, urljoin, quote

# 创建 Blueprint
proxy_bp = Blueprint('proxy', __name__)

# 常用 headers 伪装
DEFAULT_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
    'Accept': '*/*'
}

def get_headers_for_url(url):
    """根据 URL 自动生成防盗链 Headers"""
    headers = DEFAULT_HEADERS.copy()
    parsed = urlparse(url)
    
    # 针对不同站点设置 Referer
    if 'youku.com' in parsed.netloc:
        headers['Referer'] = 'https://www.youku.com/'
    elif 'iqiyi.com' in parsed.netloc:
        headers['Referer'] = 'https://www.iqiyi.com/'
    elif 'bilibili.com' in parsed.netloc or 'bilivideo.com' in parsed.netloc:
        headers['Referer'] = 'https://www.bilibili.com/'
    elif 'qq.com' in parsed.netloc:
        headers['Referer'] = 'https://v.qq.com/'
    else:
        # 默认使用 origin 作为 referer
        headers['Referer'] = f"{parsed.scheme}://{parsed.netloc}/"
        
    return headers

@proxy_bp.route('/proxy')
def proxy_stream():
    """
    代理流媒体接口
    参数: url (需要编码), headers (可选 JSON)
    """
    target_url = request.args.get('url')
    if not target_url:
        return "Missing URL", 400

    # 获取请求头
    req_headers = get_headers_for_url(target_url)
    
    # 如果是 m3u8，需要特殊处理（改写内部 ts 链接）
    is_m3u8 = '.m3u8' in target_url.split('?')[0]

    try:
        # 发起请求 (stream=True 关键)
        resp = requests.get(target_url, headers=req_headers, stream=True, timeout=10)
        
        # 排除一些不必要的 Hop-by-hop headers
        excluded_headers = ['content-encoding', 'content-length', 'transfer-encoding', 'connection']
        headers = [(name, value) for (name, value) in resp.headers.items()
                   if name.lower() not in excluded_headers]

        if is_m3u8:
            # 如果是 m3u8，读取全部内容并改写
            content = resp.content.decode('utf-8', errors='ignore')
            new_lines = []
            base_url = target_url.rsplit('/', 1)[0] + '/'
            
            for line in content.splitlines():
                line = line.strip()
                if line and not line.startswith('#'):
                    # 这是一个分片或子列表 URL
                    # 补全绝对路径
                    if not line.startswith('http'):
                        abs_url = urljoin(base_url, line)
                    else:
                        abs_url = line
                    
                    # 改写为指向本机的代理地址
                    # 注意：我们要把自己当前的 host 拼进去
                    # host_url = request.host_url.rstrip('/')  # http://127.0.0.1:5000
                    # 构造新的 proxy url
                    # 递归代理：让 ts 文件也走 /proxy?url=...
                    encoded_ts_url = quote(abs_url)
                    new_line = f"{request.path}?url={abs_url}" # 使用相对路径更安全
                    new_lines.append(new_line)
                else:
                    new_lines.append(line)
            
            new_content = '\n'.join(new_lines)
            
            # 更新 Content-Length 和 Type
            headers = [h for h in headers if h[0].lower() != 'content-length']
            headers.append(('Content-Length', str(len(new_content))))
            
            return Response(new_content, status=resp.status_code, headers=headers)

        else:
            # 普通文件 (mp4/ts)，直接流式透传
            def generate():
                for chunk in resp.iter_content(chunk_size=8192):
                    yield chunk

            return Response(stream_with_context(generate()), 
                          status=resp.status_code, 
                          headers=headers,
                          direct_passthrough=True)

    except Exception as e:
        print(f"Proxy Error: {e}")
        return f"Proxy Error: {e}", 500
