import os
import sys
import threading
import time
import socket
import requests
import xml.etree.ElementTree as ET
from urllib.parse import urlparse, quote
from collections import OrderedDict

# GUI / System Tray
import pystray
from pystray import MenuItem as item
from PIL import Image, ImageDraw

# Web Server & DLNA Logic
from flask import Flask, request, jsonify
from flask_cors import CORS
import yt_dlp
from stream_proxy import proxy_bp

# --- 全局变量 ---
SERVER_PORT = 5000
found_devices = OrderedDict() # { "Friend Name": "Control URL" }
selected_device_name = None
current_control_url = None
tray_icon = None

# --- 1. DLNA 扫描与控制 ---

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
    except:
        pass
    return None

@app.route('/cast', methods=['POST'])
def cast_endpoint():
    data = request.json
    url = data.get('url')
    if not url: return jsonify({"status": "error"}), 400

    def process(target):
        # 如果不是直链，先提示正在解析
        if not any(target.endswith(ext) for ext in ['.m3u8', '.mp4', '.mkv', '.ts']):
            tray_icon.notify("正在尝试解析视频地址，请稍候...", "Dollop Cast")
            
        real = extract_video_url(target)
        if real:
            # 判断是否需要代理 (针对防盗链站点)
            need_proxy = False
            for domain in ['youku.com', 'iqiyi.com', 'bilibili.com', 'bilivideo.com', 'qq.com']:
                if domain in real:
                    need_proxy = True
                    break
            
            if need_proxy:
                # 构造本机代理地址
                # 注意：必须对原 URL 进行编码，防止参数混淆
                proxy_url = f"http://{LOCAL_IP}:{SERVER_PORT}/proxy?url={quote(real)}"
                print(f"启用本地代理转发: {proxy_url}")
                final_url = proxy_url
            else:
                final_url = real

            success, msg = dlna_play(final_url)
            if success: tray_icon.notify(f"投屏成功: {target[:30]}...", "Dollop Cast")
            else: tray_icon.notify(f"投屏失败: {msg}", "Dollop Cast")
        else:
            # 尝试盲投
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
