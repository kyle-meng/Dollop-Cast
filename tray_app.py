import webbrowser
import os
import sys
import threading
import time
import socket
import requests
import uuid
import tkinter as tk
from tkinter import filedialog
import xml.etree.ElementTree as ET
from urllib.parse import urlparse, quote
from collections import OrderedDict

# GUI / System Tray
import pystray
from pystray import MenuItem as item
from PIL import Image, ImageDraw

# Web Server & DLNA Logic
from flask import Flask, request, jsonify, send_from_directory, send_file
from flask_cors import CORS
import yt_dlp
from stream_proxy import proxy_bp

# --- 全局变量 ---
SERVER_PORT = 5000
found_devices = OrderedDict() # { "Friend Name": "Control URL" }
selected_device_name = None
current_control_url = None
tray_icon = None
ENABLE_PROXY = True # 全局代理开关
LOCAL_DEBUG_MODE = False # 本地调试模式开关
local_files_map = {} # { "uuid": "absolute_file_path" }



def get_control_url_from_desc(desc_url):
    try:
        response = requests.get(desc_url, timeout=3)
        if response.status_code != 200: return None
        
        root = ET.fromstring(response.content)
        # 获取 Friendly Name
        friendly_name = root.findtext(".//{urn:schemas-upnp-org:device-1-0}friendlyName")
        if not friendly_name:
             friendly_name = root.findtext(".//friendlyName")
        if not friendly_name:
            friendly_name = "Unknown Device"

        # 获取 AVTransport Control URL
        for service in root.iter():
            s_type = service.findtext("{urn:schemas-upnp-org:device-1-0}serviceType") or service.findtext("serviceType")
            if s_type and 'AVTransport' in s_type:
                c_url = service.findtext("{urn:schemas-upnp-org:device-1-0}controlURL") or service.findtext("controlURL")
                if c_url:
                    if not c_url.startswith('http'):
                        parsed = urlparse(desc_url)
                        base = f"{parsed.scheme}://{parsed.netloc}"
                        c_url = base + c_url if c_url.startswith('/') else base + "/" + c_url
                    return friendly_name, c_url
    except:
        pass
    return None, None

def scan_devices_loop():
    global found_devices, selected_device_name, current_control_url
    
    MCAST_GRP = '239.255.255.250'
    MCAST_PORT = 1900
    msg = (
        'M-SEARCH * HTTP/1.1\r\n'
        'HOST: 239.255.255.250:1900\r\n'
        'MAN: "ssdp:discover"\r\n'
        'MX: 3\r\n'
        'ST: urn:schemas-upnp-org:device:MediaRenderer:1\r\n'
        '\r\n'
    ).encode('utf-8')

    while True:
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
            sock.settimeout(5)
            sock.sendto(msg, (MCAST_GRP, MCAST_PORT))
            
            start_time = time.time()
            while time.time() - start_time < 5: # 每次扫描 5 秒
                try:
                    data, addr = sock.recvfrom(65507)
                    response = data.decode('utf-8', errors='ignore')
                    headers = {}
                    for line in response.split('\r\n'):
                        if ':' in line:
                            k, v = line.split(':', 1)
                            headers[k.strip().upper()] = v.strip()
                    
                    location = headers.get('LOCATION')
                    usn = headers.get('USN', '')
                    
                    if location:
                        # 这是一个新发现的设备 或者 已知设备更新
                        # 获取详细信息
                        name, ctrl_url = get_control_url_from_desc(location)
                        if name and ctrl_url:
                            # 加上 IP 区分同名设备
                            unique_name = f"{name} ({addr[0]})"
                            
                            if unique_name not in found_devices:
                                found_devices[unique_name] = ctrl_url
                                print(f"发现设备: {unique_name}")
                                # 如果还没选设备，默认选第一个
                                if not selected_device_name:
                                    select_device(unique_name)
                                    update_menu() # 通知 UI 更新
                                    
                except socket.timeout:
                    break
                except Exception as e:
                    pass
            sock.close()
            
            # 每 30 秒重新扫描一次
            time.sleep(30)
            
        except Exception as e:
            print(f"扫描线程出错: {e}")
            time.sleep(10)

def dlna_play(video_url):
    global current_control_url
    if not current_control_url:
        return False, "未选择设备或未发现设备"

    print(f"投送地址: {video_url} -> {current_control_url}")
    headers = {
        'Content-Type': 'text/xml; charset="utf-8"',
        'SOAPACTION': '"urn:schemas-upnp-org:service:AVTransport:1#SetAVTransportURI"'
    }
    
    body = f"""<?xml version="1.0"?>
    <s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">
        <s:Body>
            <u:SetAVTransportURI xmlns:u="urn:schemas-upnp-org:service:AVTransport:1">
                <InstanceID>0</InstanceID>
                <CurrentURI>{video_url}</CurrentURI>
                <CurrentURIMetaData></CurrentURIMetaData>
            </u:SetAVTransportURI>
        </s:Body>
    </s:Envelope>"""
    
    try:
        requests.post(current_control_url, data=body, headers=headers, timeout=5)
    except Exception as e:
        return False, f"发送地址失败: {e}"

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
    try:
        requests.post(current_control_url, data=body, headers=headers, timeout=5)
        return True, "投屏指令已发送"
    except Exception as e:
        return False, f"发送播放指令失败: {e}"

# --- 2. Flask Server (后台服务) ---

app = Flask(__name__)
CORS(app)
app.register_blueprint(proxy_bp)

def get_local_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        # 不需要真的连接，只是为了获取路由出口IP
        s.connect(('10.255.255.255', 1))
        IP = s.getsockname()[0]
    except Exception:
        IP = '127.0.0.1'
    finally:
        s.close()
    return IP

LOCAL_IP = get_local_ip()

def extract_video_url(web_url):
    if any(web_url.endswith(ext) for ext in ['.m3u8', '.mp4', '.mkv', '.ts']):
        return web_url
    
    # 针对 Bilibili 使用专用解析器 (更快、更准)
    if 'bilibili.com' in web_url or 'BV' in web_url:
        try:
            import bili_parser
            bili_url = bili_parser.get_bilibili_stream(web_url)
            if bili_url: return bili_url
        except Exception:
            pass

    try:
        # 优化 yt-dlp 配置：优先 MP4，避免 DASH (除非电视支持)
        ydl_opts = {
            'format': 'best[ext=mp4]/best', 
            'quiet': True, 
            'no_warnings': True,
            # 某些网站需要模拟浏览器 User-Agent
            'user_agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(web_url, download=False)
            if 'url' in info: return info['url']
            elif 'formats' in info: return info['formats'][-1]['url']
    except Exception as e:
        print(f"解析失败: {e}")
    return None

# --- 路由：静态文件服务 (cache) ---
import os

CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'cache')
if not os.path.exists(CACHE_DIR):
    os.makedirs(CACHE_DIR)

@app.route('/local/<file_id>')
def serve_local_file(file_id):
    """Serve selected local file by ID"""
    file_path = local_files_map.get(file_id)
    if not file_path or not os.path.exists(file_path):
        return "File not found or expired", 404
    return send_file(file_path)

@app.route('/cache/<path:filename>')
def serve_cache(filename):
    return send_from_directory(CACHE_DIR, filename)

def prefetch_segments(content, base_url, proxy_endpoint):
    """
    后台预加载线程：解析 m3u8 中的 TS 链接并请求本机代理缓存
    """
    import time
    print("后台预加载开始...")
    urls = []
    for line in content.splitlines():
        line = line.strip()
        if line and not line.startswith('#'):
            if not line.startswith('http'):
                abs_url = requests.compat.urljoin(base_url, line)
            else:
                abs_url = line
            urls.append(abs_url)
    
    print(f"预加载列表: 共 {len(urls)} 个分片")
    
    BATCH_SIZE = 10
    BATCH_INTERVAL = 60

    for i, u in enumerate(urls):
        try:
            # 请求本机 proxy 接口
            target = f"{proxy_endpoint}?url={requests.utils.quote(u)}"
            requests.get(target, timeout=20) 
            
            # --- 批次控制 ---
            # 每下载完一批，休息一段时间
            if (i + 1) % BATCH_SIZE == 0:
                print(f"预加载进度: {i+1}/{len(urls)}。暂停 {BATCH_INTERVAL} 秒...")
                time.sleep(BATCH_INTERVAL)
            else:
                 time.sleep(0.05)
            
        except Exception as e:
            pass
    print("后台预加载结束")

def download_and_process_m3u8(url, local_ip, use_proxy_segment=True):
    """
    下载 m3u8 改写内容
    use_proxy_segment=True: 切片走本机 /segment 代理
    use_proxy_segment=False: 切片直连 (只补全绝对路径)
    """
    try:
        # ... (下载逻辑不变) ...
        # 1. 下载原始 m3u8 (尽量带防盗链头)
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Referer': 'https://www.youku.com/' 
        }
        if 'Host' in headers: del headers['Host']
        
        resp = requests.get(url, headers=headers, timeout=10)
        if resp.status_code != 200:
            print(f"m3u8下载失败: {resp.status_code}")
            return None

        # 2. 改写内容
        content = resp.content.decode('utf-8', errors='ignore')
        new_lines = []
        base_url = url.rsplit('/', 1)[0] + '/'
        host_url = f"http://{local_ip}:{SERVER_PORT}"

        for line in content.splitlines():
            line = line.strip()
            if line and not line.startswith('#'):
                # 补全绝对路径
                if not line.startswith('http'):
                    abs_url = requests.compat.urljoin(base_url, line)
                else:
                    abs_url = line
                
                if use_proxy_segment:
                    # 改写为本机代理
                    new_line = f"{host_url}/segment?url={requests.utils.quote(abs_url)}"
                else:
                    # 直连模式 (保留绝对路径)
                    new_line = abs_url

                new_lines.append(new_line)
            else:
                new_lines.append(line)
        
        # 3. 保存到本地
        local_filename = 'video.m3u8'
        with open(os.path.join(CACHE_DIR, local_filename), 'w', encoding='utf-8') as f:
            f.write('\n'.join(new_lines))
            
        print(f"m3u8已缓存 (代理切片: {use_proxy_segment}): {local_filename}")

        # --- 启动预加载 (如果启用了代理) ---
        if use_proxy_segment:
            proxy_endpoint = f"{host_url}/segment"
            threading.Thread(target=prefetch_segments, args=(content, base_url, proxy_endpoint), daemon=True).start()

        return f"{host_url}/cache/{local_filename}"

    except Exception as e:
        print(f"处理m3u8出错: {e}")
        return None

@app.route('/cast', methods=['POST'])
def cast_endpoint():
    data = request.json
    url = data.get('url')
    if not url: return jsonify({"status": "error"}), 400

    def process(target):
        # ... (解析逻辑不变) ...
        if not any(target.endswith(ext) for ext in ['.m3u8', '.mp4', '.mkv', '.ts']):
            tray_icon.notify("正在尝试解析视频地址，请稍候...", "Dollop Cast")
            
        real = extract_video_url(target)
        if real:
            print(f"解析得到直链: {real}")
            
            # 判断是否是必须本地缓存的站点 (处理 m3u8 防盗链)
            must_process_locally = False
            for domain in ['youku.com', 'iqiyi.com', 'bilibili.com', 'bilivideo.com', 'qq.com']:
                if domain in real:
                    must_process_locally = True
                    break
            
            final_url = real
            if must_process_locally:
                # 区分 m3u8 还是直链视频 (MP4/FLV)
                # B站通常给的是 flv/mp4 直链，不能用 m3u8 逻辑处理
                is_playlist = '.m3u8' in real or '/m3u8' in real
                
                if is_playlist:
                    # m3u8: 下载清单 -> 改写 -> 预加载 -> 投送本地文件
                    local_m3u8_url = download_and_process_m3u8(real, LOCAL_IP, use_proxy_segment=ENABLE_PROXY)
                    if local_m3u8_url:
                        final_url = local_m3u8_url
                        print(f"投屏本地 m3u8: {final_url} (代理: {ENABLE_PROXY})")
                    else:
                        print("m3u8 处理失败，回退到原始地址")
                else:
                    # MP4/FLV: 
                    if ENABLE_PROXY:
                        # 走流式代理 (解决 Referer 问题)
                        # http://本机:5000/segment?url=...
                        final_url = f"http://{LOCAL_IP}:{SERVER_PORT}/segment?url={requests.utils.quote(real)}"
                        print(f"投屏流式代理 (MP4/FLV): {final_url}")
                    else:
                        # 直连 (可能因 Referer 失败)
                        print("直连投屏 (无代理)")

            if LOCAL_DEBUG_MODE:
                print(f"本地调试模式已开启，尝试在浏览器打开: {final_url}")
                webbrowser.open(final_url)
                tray_icon.notify(f"已在浏览器打开: {final_url[:50]}", "Dollop Cast")
            else:
                success, msg = dlna_play(final_url)
                if success: tray_icon.notify(f"投屏成功: {target[:30]}...", "Dollop Cast")
                else: tray_icon.notify(f"投屏失败: {msg}", "Dollop Cast")
        else:
            # 尝试盲投
            if LOCAL_DEBUG_MODE:
                 print(f"本地调试模式 (盲投): {target[:50]}...")
                 webbrowser.open(target)
                 tray_icon.notify(f"已在浏览器打开: {target[:50]}...", "Dollop Cast")
            else:
                 dlna_play(target)

    threading.Thread(target=process, args=(url,)).start()
    return jsonify({"status": "ok"})

def start_server():
    # 监听 0.0.0.0 以允许局域网访问 (代理需要)
    app.run(host='0.0.0.0', port=SERVER_PORT, debug=False, use_reloader=False)

# --- 3. System Tray Logic ---

def create_image():
    # 生成一个简单的托盘图标
    width = 64
    height = 64
    image = Image.new('RGB', (width, height), (255, 255, 255))
    dc = ImageDraw.Draw(image)
    dc.rectangle((0, 0, width, height), fill=(33, 150, 243))
    dc.rectangle((10, 15, 54, 45), fill=(255, 255, 255)) # TV Screen
    dc.polygon([(20, 45), (44, 45), (32, 55)], fill=(255, 255, 255)) # Stand
    return image

def select_device(name):
    global selected_device_name, current_control_url
    selected_device_name = name
    current_control_url = found_devices[name]
    print(f"当前选中设备: {name}")

def update_menu():
    if tray_icon:
        # 重新生成并赋值整个菜单对象
        tray_icon.menu = build_menu()
        # pystray 没有明确的 refresh 方法，赋值通常即生效，
        # 或者某些后端可能需要 update_menu() 但这里直接赋值是最稳妥的跨平台方式

def build_menu():
    items = []
    
    # 标题项 (不可点)
    # pystray 要求动态文本函数接受一个 item 参数
    items.append(item(f'Dollop Cast 服务运行中 (:5000)', lambda icon, item: None, enabled=False))
    items.append(item(lambda item: '----------------', lambda icon, item: None)) # 分隔符

    # 代理开关
    def toggle_proxy(icon, item):
        global ENABLE_PROXY
        ENABLE_PROXY = not ENABLE_PROXY
        update_menu()

    items.append(item(
        '启用防盗链代理',
        toggle_proxy,
        checked=lambda item: ENABLE_PROXY
    ))
    
    # 调试开关
    def toggle_debug(icon, item):
        global LOCAL_DEBUG_MODE
        LOCAL_DEBUG_MODE = not LOCAL_DEBUG_MODE
        update_menu()

    items.append(item(
        '启用本地调试 (浏览器播放)',
        toggle_debug,
        checked=lambda item: LOCAL_DEBUG_MODE
    ))
    
    # 本地文件投屏
    def cast_local_file(icon, item):
        def _task():
            try:
                # 简单的 tkinter 文件选择
                root = tk.Tk()
                root.withdraw()
                root.attributes('-topmost', True)
                file_path = filedialog.askopenfilename(
                    title="选择视频文件",
                    filetypes=[("视频文件", "*.mp4;*.mkv;*.avi;*.mov;*.flv;*.ts"), ("所有文件", "*.*")]
                )
                root.destroy()
                
                if file_path:
                    fid = str(uuid.uuid4())
                    local_files_map[fid] = file_path
                    # 必须 quote 文件名? 不，这里是 ID
                    play_url = f"http://{LOCAL_IP}:{SERVER_PORT}/local/{fid}"
                    
                    if LOCAL_DEBUG_MODE:
                        webbrowser.open(play_url)
                    else:
                        success, msg = dlna_play(play_url)
                        if success: 
                            tray_icon.notify(f"投屏中: {os.path.basename(file_path)}", "Dollop Cast")
                        else: 
                            tray_icon.notify(f"失败: {msg}", "Dollop Cast")
            except Exception as e:
                print(f"本地文件投屏错误: {e}")

        threading.Thread(target=_task).start()

    items.append(item('投放本地文件...', cast_local_file))
    
    items.append(item(lambda item: '----------------', lambda icon, item: None))

    # 设备列表
    if not found_devices:
        items.append(item('正在扫描设备...', lambda icon, item: None, enabled=False))
    else:
        for name in found_devices:
            # action 必须接受 (icon, item)
            def on_click(icon, item):
                select_device(str(item))
            
            is_checked = (name == selected_device_name)
            items.append(item(name, on_click, checked=lambda item: selected_device_name == str(item), radio=True))

    items.append(item(lambda item: '----------------', lambda icon, item: None))
    items.append(item('手动重扫', lambda icon, item: threading.Thread(target=lambda: scan_devices_loop()).start()))
    items.append(item('退出', lambda icon, item: tray_icon.stop()))
    
    return pystray.Menu(*items)

def main():
    global tray_icon
    
    # 启动 Flask 服务
    t_server = threading.Thread(target=start_server, daemon=True)
    t_server.start()

    # 启动扫描线程
    t_scan = threading.Thread(target=scan_devices_loop, daemon=True)
    t_scan.start()

    # 启动托盘
    # 注意：menu 参数需要传入 Menu 对象，而不是函数
    tray_icon = pystray.Icon("DollopCast", create_image(), "Dollop Cast", menu=build_menu())
    tray_icon.run()

if __name__ == '__main__':
    main()
