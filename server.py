import os
import sys
import threading
import requests
import yt_dlp
import xml.etree.ElementTree as ET
from urllib.parse import urlparse
from flask import Flask, request, jsonify
from flask_cors import CORS

# --- 配置 ---
TARGET_DEVICE_IP = "192.168.1.224"
TARGET_DEVICE_DESC_URL = "http://192.168.1.224:49152/description.xml"
SERVER_PORT = 5000

app = Flask(__name__)
CORS(app) # 允许跨域，方便浏览器插件调用

# --- DLNA 功能模块 ---

def get_av_transport_url(desc_url):
    try:
        print(f"正在连接设备: {desc_url}")
        response = requests.get(desc_url, timeout=3)
        response.raise_for_status()
        root = ET.fromstring(response.content)
        for service in root.iter():
            s_type = service.findtext("{urn:schemas-upnp-org:device-1-0}serviceType") or service.findtext("serviceType")
            if s_type and 'AVTransport' in s_type:
                c_url = service.findtext("{urn:schemas-upnp-org:device-1-0}controlURL") or service.findtext("controlURL")
                if c_url:
                    if not c_url.startswith('http'):
                        parsed = urlparse(desc_url)
                        base = f"{parsed.scheme}://{parsed.netloc}"
                        c_url = base + c_url if c_url.startswith('/') else base + "/" + c_url
                    return c_url
    except Exception as e:
        print(f"获取控制 URL 失败: {e}")
    return None

def dlna_play(video_url):
    control_url = get_av_transport_url(TARGET_DEVICE_DESC_URL)
    if not control_url:
        return False, "无法连接到电视"

    print(f"投送地址: {video_url}")
    headers = {
        'Content-Type': 'text/xml; charset="utf-8"',
        'SOAPACTION': '"urn:schemas-upnp-org:service:AVTransport:1#SetAVTransportURI"'
    }
    
    # SetAVTransportURI
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
        requests.post(control_url, data=body, headers=headers, timeout=5)
    except Exception as e:
        return False, f"发送地址失败: {e}"

    # Play
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
        requests.post(control_url, data=body, headers=headers, timeout=5)
        return True, "投屏指令已发送"
    except Exception as e:
        return False, f"发送播放指令失败: {e}"

# --- 视频解析模块 ---

def extract_video_url(web_url):
    # 如果本身就是流媒体链接，直接返回
    if any(web_url.endswith(ext) for ext in ['.m3u8', '.mp4', '.mkv', '.ts']):
        return web_url

    print(f"正在解析网页: {web_url}")
    ydl_opts = {
        'format': 'best', # 选择最佳画质
        'quiet': True,
        'no_warnings': True,
    }
    
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(web_url, download=False)
            if 'url' in info:
                return info['url']
            elif 'formats' in info:
                return info['formats'][-1]['url'] # 尝试获取最后一个format的url
    except Exception as e:
        print(f"解析失败: {e}")
        return None
    return None

# --- Flask 路由 ---

@app.route('/cast', methods=['POST'])
def cast_endpoint():
    data = request.json
    url = data.get('url')
    if not url:
        return jsonify({"status": "error", "message": "No URL provided"}), 400

    print(f"\n收到投屏请求: {url}")
    
    # 异步处理，避免阻塞 HTTP 响应
    def process_and_cast(target_url):
        real_url = extract_video_url(target_url)
        if real_url:
            print(f"解析得到真实地址: {real_url}")
            success, msg = dlna_play(real_url)
            print(f"投屏结果: {msg}")
        else:
            print("抱歉，无法解析该网址的视频流。")
            # 这里如果解析失败，也许可以尝试直接投原始 URL (有些电视浏览器也许能识别)
            dlna_play(target_url)

    threading.Thread(target=process_and_cast, args=(url,)).start()
    
    return jsonify({"status": "ok", "message": "正在处理投屏请求..."})

if __name__ == '__main__':
    print(f"Dollop Cast 服务端启动在端口 {SERVER_PORT}...")
    app.run(host='127.0.0.1', port=SERVER_PORT)
