import os
import time
import socket
import subprocess
import threading
import http.server
import socketserver
import requests
import xml.etree.ElementTree as ET
from urllib.parse import urlparse

# --- 配置 ---
TARGET_DEVICE_IP = "192.168.1.224"
TARGET_DEVICE_DESC_URL = "http://192.168.1.224:49152/description.xml"
LOCAL_IP = socket.gethostbyname(socket.gethostname())
HTTP_PORT = 8080
HLS_FILENAME = "stream.m3u8"
SEGMENT_FILENAME = "http://192.168.1.85:8080/segment%03d.ts"
HLS_TIME = 2  # 分片时间(秒)，2秒是一个平衡点
LIST_SIZE = 5 # 播放列表保留的分片数

# --- 1. 获取本地 IP ---
def get_local_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        # 不需要真的连接，只是为了获取路由使用的出口IP
        s.connect(('10.255.255.255', 1))
        IP = s.getsockname()[0]
    except Exception:
        IP = '127.0.0.1'
    finally:
        s.close()
    return IP

LOCAL_IP = get_local_ip()
print(f"本机 IP: {LOCAL_IP}")

# --- 2. 简单的 HTTP 服务器 (用于提供流媒体) ---
def start_http_server():
    class CORSRequestHandler(http.server.SimpleHTTPRequestHandler):
        def end_headers(self):
            self.send_header('Access-Control-Allow-Origin', '*')
            super().end_headers()
            
        def log_message(self, format, *args):
            print(f"[{self.log_date_time_string()}] {format % args}")

        def guess_type(self, path):
            if path.endswith(".m3u8"):
                return "application/x-mpegURL"
            elif path.endswith(".ts"):
                return "video/MP2T"
            return super().guess_type(path)

    handler = CORSRequestHandler
    try:
        socketserver.TCPServer.allow_reuse_address = True
        with socketserver.TCPServer(("", HTTP_PORT), handler) as httpd:
            print(f"HTTP 服务器已启动: http://{LOCAL_IP}:{HTTP_PORT}")
            httpd.serve_forever()
    except OSError as e:
        print(f"端口 {HTTP_PORT} 被占用，请尝试更改脚本中的端口号。错误: {e}")

# --- 3. 获取 AVTransport Control URL ---
def get_av_transport_url(desc_url):
    try:
        print(f"正在获取设备描述: {desc_url}")
        response = requests.get(desc_url, timeout=5)
        response.raise_for_status()
        
        root = ET.fromstring(response.content)
        ns = {'tns': 'urn:schemas-upnp-org:device-1-0'}
        
        # 命名空间处理比较麻烦，简单暴力查找
        for service in root.iter():
            if service.tag.endswith('service'):
                service_type = service.findtext("{urn:schemas-upnp-org:device-1-0}serviceType", default="")
                if not service_type: # 尝试无命名空间查找
                     service_type = service.findtext("serviceType", default="")
                
                if 'AVTransport' in service_type:
                    control_url = service.findtext("{urn:schemas-upnp-org:device-1-0}controlURL", default="")
                    if not control_url:
                        control_url = service.findtext("controlURL", default="")
                        
                    if control_url:
                        # 处理相对路径
                        if not control_url.startswith('http'):
                            parsed = urlparse(desc_url)
                            control_url = f"{parsed.scheme}://{parsed.netloc}{control_url}" if control_url.startswith('/') else f"{parsed.scheme}://{parsed.netloc}/{control_url}"
                        return control_url
    except Exception as e:
        print(f"获取控制 URL 失败: {e}")
    return None

# --- 4. 发送 DLNA 播放命令 (SetAVTransportURI + Play) ---
def play_stream(control_url, stream_url):
    headers = {
        'Content-Type': 'text/xml; charset="utf-8"',
        'SOAPACTION': '"urn:schemas-upnp-org:service:AVTransport:1#SetAVTransportURI"'
    }
    
    body = f"""<?xml version="1.0"?>
    <s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">
        <s:Body>
            <u:SetAVTransportURI xmlns:u="urn:schemas-upnp-org:service:AVTransport:1">
                <InstanceID>0</InstanceID>
                <CurrentURI>{stream_url}</CurrentURI>
                <CurrentURIMetaData></CurrentURIMetaData>
            </u:SetAVTransportURI>
        </s:Body>
    </s:Envelope>"""

    try:
        print(f"发送投屏地址: {stream_url}")
        requests.post(control_url, data=body, headers=headers)
        
        # 发送播放命令
        headers['SOAPACTION'] = '"urn:schemas-upnp-org:service:AVTransport:1#Play"'
        body = """<?xml version="1.0"?>
        <s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">
            <s:Body>
                <u:Play xmlns:u="urn:schemas-upnp-org:service:AVTransport:1">
                    <InstanceID>0</InstanceID>
                    <Speed>1</Speed>
                </u:Play>
            </s:Body>
        </s:Envelope>"""
        print("发送播放命令...")
        requests.post(control_url, data=body, headers=headers)
        print("指令已发送，请查看电视。")
    except Exception as e:
        print(f"发送指令失败: {e}")

# --- 5. 启动 FFmpeg 录屏 ---
def start_ffmpeg():
    # 确保没有残留文件
    if os.path.exists(HLS_FILENAME):
        os.remove(HLS_FILENAME)
    for f in os.listdir('.'):
        if f.endswith('.ts'):
            os.remove(f)

    # FFmpeg 命令
    # -f gdigrab: Windows 屏幕捕获
    # -framerate 30: 帧率
    # -i desktop: 捕获全屏
    # -c:v libx264: 视频编码 H.264
    # -preset ultrafast: 极速编码，降低延迟 (牺牲画质)
    # -tune zerolatency: 零延迟调优
    # -vf scale=1280:-2: 缩放到 720p 以降低带宽和性能压力 (可选)
    # -f hls: 输出为 HLS 流
    # 解析 SEGMENT_FILENAME
    hls_segment_filename = SEGMENT_FILENAME
    hls_base_url = None
    if SEGMENT_FILENAME.startswith('http'):
        hls_segment_filename = SEGMENT_FILENAME.split('/')[-1]
        hls_base_url = SEGMENT_FILENAME.replace(hls_segment_filename, '')

    cmd = [
        'ffmpeg',
        '-f', 'gdigrab',
        '-framerate', '30',
        '-offset_x', '0', '-offset_y', '0',
        '-video_size', '1920x1080',
        '-i', 'desktop',
        '-f', 'lavfi', '-i', 'anullsrc=channel_layout=stereo:sample_rate=44100',
        '-vf', 'scale=1920:1080,format=yuv420p',
        '-c:v', 'libx264',
        '-preset', 'veryfast', # 稍微慢一点但质量更好，ultrafast可能产生过大的流
        '-tune', 'zerolatency',
        '-maxrate', '6000k',
        '-bufsize', '12000k',
        '-profile:v', 'high', # 1080p 使用 high profile 更高效
        '-level', '4.1',
        '-c:a', 'aac',
        '-ac', '2',
        '-ar', '44100', # 强制 44.1kHz 采样率
        '-b:a', '128k',
        '-g', '60',     # 关键帧间隔 2秒 (配合 2秒切片)
        '-keyint_min', '60',
        '-sc_threshold', '0',
        '-f', 'hls',
        '-hls_time', str(HLS_TIME), # 增加切片时长到 1 秒
        '-hls_list_size', str(LIST_SIZE),
        '-hls_flags', 'delete_segments+append_list',
    ]

    if hls_base_url:
        cmd.extend(['-hls_base_url', hls_base_url])
    
    cmd.extend(['-hls_segment_filename', hls_segment_filename])
    cmd.append(HLS_FILENAME)
    
    print("正在启动 FFmpeg 录屏推流... (按 Ctrl+C 停止)")
    print("注意：如果报错，请检查屏幕分辨率设置。")
    
    try:
        # 我们不希望 ffmpeg 阻塞主线程，也不希望它输出太多垃圾信息
        process = subprocess.Popen(cmd) # , stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        process.wait()
    except KeyboardInterrupt:
        print("停止推流。")
        process.terminate()

# --- 主程序 ---
if __name__ == "__main__":
    # 1. 启动 HTTP Server (线程)
    t_server = threading.Thread(target=start_http_server, daemon=True)
    t_server.start()
    
    # 2. 获取控制 URL
    control_url = None
    try:
        control_url = get_av_transport_url(TARGET_DEVICE_DESC_URL)
    except:
        pass

    if not control_url:
        print("\n" + "="*50)
        print("注意：无法连接到 DLNA 设备 (或连接超时)。")
        print("已进入【仅服务器模式】，您可以使用本机播放器测试。")
        print(f"播放地址: http://{LOCAL_IP}:{HTTP_PORT}/{HLS_FILENAME}")
        print("推荐测试命令: ffplay " + f"http://{LOCAL_IP}:{HTTP_PORT}/{HLS_FILENAME}")
        print("="*50 + "\n")
    else:
        print(f"设备控制 URL: {control_url}")
        stream_url = f"http://{LOCAL_IP}:{HTTP_PORT}/{HLS_FILENAME}"
        
        # 4. 启动自动投屏线程
        t_play = threading.Thread(target=lambda: (time.sleep(5), play_stream(control_url, stream_url)), daemon=True)
        t_play.start()
    
    # 5. 启动 FFmpeg (主线程阻塞)
    start_ffmpeg()
