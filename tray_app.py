import webbrowser
import os
import sys
import threading
import time
import socket
import requests
import uuid
import json
import tkinter as tk
from tkinter import filedialog, messagebox
import xml.etree.ElementTree as ET
from urllib.parse import urlparse, quote
from collections import OrderedDict
from flask import Flask, request, jsonify, send_from_directory, send_file, render_template_string
from flask_cors import CORS
import yt_dlp
from stream_proxy import proxy_bp

# --- 配置与存储 ---
SERVER_PORT = 5000
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data')
if not os.path.exists(DATA_DIR): os.makedirs(DATA_DIR)

HISTORY_FILE = os.path.join(DATA_DIR, 'history.json')
SHORTCUTS_FILE = os.path.join(DATA_DIR, 'shortcuts.json')

def load_json(path, default):
    if os.path.exists(path):
        try:
            with open(path, 'r', encoding='utf-8') as f: return json.load(f)
        except: return default
    return default

def save_json(path, data):
    with open(path, 'w', encoding='utf-8') as f: json.dump(data, f, ensure_ascii=False, indent=2)

# --- 全局状态 ---
found_devices = OrderedDict()
selected_device_name = None
current_control_url = None
ENABLE_PROXY = True
LOCAL_DEBUG_MODE = False
local_files_map = {} 
current_playing_url = None # 当前正在电视上播放的原始 URL (用于匹配历史)
current_playing_name = "未知视频"

# --- DLNA 逻辑 ---

def get_control_url_from_desc(desc_url):
    try:
        response = requests.get(desc_url, timeout=3)
        if response.status_code != 200: return None
        root = ET.fromstring(response.content)
        friendly_name = root.findtext(".//{urn:schemas-upnp-org:device-1-0}friendlyName") or root.findtext(".//friendlyName") or "Unknown Device"
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
    except: pass
    return None, None

def scan_devices_loop():
    global found_devices, selected_device_name
    MCAST_GRP, MCAST_PORT = '239.255.255.250', 1900
    msg = ('M-SEARCH * HTTP/1.1\r\nHOST: 239.255.255.250:1900\r\nMAN: "ssdp:discover"\r\nMX: 3\r\nST: urn:schemas-upnp-org:device:MediaRenderer:1\r\n\r\n').encode('utf-8')
    while True:
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
            sock.settimeout(5)
            sock.sendto(msg, (MCAST_GRP, MCAST_PORT))
            start_time = time.time()
            while time.time() - start_time < 5:
                try:
                    data, addr = sock.recvfrom(65507)
                    response = data.decode('utf-8', errors='ignore')
                    headers = {l.split(':', 1)[0].strip().upper(): l.split(':', 1)[1].strip() for l in response.split('\r\n') if ':' in l}
                    location = headers.get('LOCATION')
                    if location:
                        name, ctrl_url = get_control_url_from_desc(location)
                        if name and ctrl_url:
                            unique_name = f"{name} ({addr[0]})"
                            if unique_name not in found_devices:
                                found_devices[unique_name] = ctrl_url
                                if not selected_device_name: select_device(unique_name)
                except socket.timeout: break
                except: pass
            sock.close()
            time.sleep(30)
        except: time.sleep(10)

def select_device(name):
    global selected_device_name, current_control_url
    selected_device_name = name
    current_control_url = found_devices.get(name)
    print(f"[*] 选中设备: {name}")

def dlna_play(video_url, seek_to=None):
    if not current_control_url: return False, "未选择设备"
    headers = {'Content-Type': 'text/xml; charset="utf-8"', 'SOAPACTION': '"urn:schemas-upnp-org:service:AVTransport:1#SetAVTransportURI"'}
    body = f'<?xml version="1.0"?><s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body><u:SetAVTransportURI xmlns:u="urn:schemas-upnp-org:service:AVTransport:1"><InstanceID>0</InstanceID><CurrentURI>{video_url}</CurrentURI><CurrentURIMetaData></CurrentURIMetaData></u:SetAVTransportURI></s:Body></s:Envelope>'
    try:
        requests.post(current_control_url, data=body, headers=headers, timeout=5)
        headers['SOAPACTION'] = '"urn:schemas-upnp-org:service:AVTransport:1#Play"'
        body_play = '<?xml version="1.0"?><s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body><u:Play xmlns:u="urn:schemas-upnp-org:service:AVTransport:1"><InstanceID>0</InstanceID><Speed>1</Speed></u:Play></s:Body></s:Envelope>'
        requests.post(current_control_url, data=body_play, headers=headers, timeout=5)
        
        # 跳转逻辑优化：多次尝试跳转
        if seek_to and seek_to != "00:00:00":
            def delayed_seek():
                print(f"[*] 尝试跳转到进度: {seek_to}")
                # 电视加载视频流需要时间，我们尝试 3 次跳转
                for i in range(3):
                    time.sleep(3 + i * 2) 
                    try:
                        headers_seek = {'Content-Type': 'text/xml; charset="utf-8"', 'SOAPACTION': '"urn:schemas-upnp-org:service:AVTransport:1#Seek"'}
                        body_seek = f'<?xml version="1.0"?><s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body><u:Seek xmlns:u="urn:schemas-upnp-org:service:AVTransport:1"><InstanceID>0</InstanceID><Unit>REL_TIME</Unit><Target>{seek_to}</Target></u:Seek></s:Body></s:Envelope>'
                        resp = requests.post(current_control_url, data=body_seek, headers=headers_seek, timeout=5)
                        if resp.status_code == 200:
                            print(f"[*] 第 {i+1} 次跳转成功")
                            break
                    except: pass
            threading.Thread(target=delayed_seek).start()
        return True, "投屏成功"
    except Exception as e: return False, str(e)

def update_history_pos(url, pos):
    """保存进度到文件"""
    if not url: return
    history = load_json(HISTORY_FILE, {})
    if url in history:
        history[url]['pos'] = pos
        history[url]['time'] = time.time()
        save_json(HISTORY_FILE, history)

def position_polling_loop():
    """后台轮询电视当前的播放进度"""
    global current_playing_url, current_control_url
    while True:
        if current_control_url and current_playing_url:
            try:
                headers = {'Content-Type': 'text/xml; charset="utf-8"', 'SOAPACTION': '"urn:schemas-upnp-org:service:AVTransport:1#GetPositionInfo"'}
                body = '<?xml version="1.0"?><s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body><u:GetPositionInfo xmlns:u="urn:schemas-upnp-org:service:AVTransport:1"><InstanceID>0</InstanceID></u:GetPositionInfo></s:Body></s:Envelope>'
                resp = requests.post(current_control_url, data=body, headers=headers, timeout=3)
                if resp.status_code == 200:
                    # 使用正则或简单解析获取 RelTime
                    content = resp.text
                    start = content.find("<RelTime>") + 9
                    end = content.find("</RelTime>")
                    if start > 8 and end > start:
                        rel_time = content[start:end]
                        # 只有大于 00:00:00 才记录
                        if rel_time != "00:00:00" and "NOT_IMPLEMENTED" not in rel_time:
                            update_history_pos(current_playing_url, rel_time)
            except: pass
        time.sleep(5)

# --- Flask Server ---
app = Flask(__name__)
CORS(app)
app.register_blueprint(proxy_bp)

def get_local_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try: s.connect(('10.255.255.255', 1)); IP = s.getsockname()[0]
    except: IP = '127.0.0.1'
    finally: s.close()
    return IP

LOCAL_IP = get_local_ip()

@app.route('/')
def index():
    return render_template_string(WEB_UI_HTML, local_ip=LOCAL_IP, port=SERVER_PORT)

@app.route('/api/status')
def get_status():
    return jsonify({
        "devices": list(found_devices.keys()),
        "selected_device": selected_device_name,
        "proxy": ENABLE_PROXY,
        "debug": LOCAL_DEBUG_MODE,
        "history": load_json(HISTORY_FILE, {}),
        "shortcuts": load_json(SHORTCUTS_FILE, [])
    })

@app.route('/api/browse/<type>')
def browse_local(type):
    root = tk.Tk(); root.withdraw(); root.attributes('-topmost', True)
    path = filedialog.askopenfilename() if type == 'file' else filedialog.askdirectory()
    root.destroy()
    return jsonify({"path": path})

@app.route('/api/config', methods=['POST'])
def update_config():
    global ENABLE_PROXY, LOCAL_DEBUG_MODE
    data = request.json
    if 'proxy' in data: ENABLE_PROXY = data['proxy']
    if 'debug' in data: LOCAL_DEBUG_MODE = data['debug']
    if 'selected_device' in data: select_device(data['selected_device'])
    return jsonify({"status": "ok"})

@app.route('/api/shortcuts', methods=['POST'])
def add_shortcut():
    shortcuts = load_json(SHORTCUTS_FILE, [])
    shortcuts.append(request.json)
    save_json(SHORTCUTS_FILE, shortcuts)
    return jsonify({"status": "ok"})

@app.route('/api/shortcuts/delete', methods=['POST'])
def delete_shortcut():
    idx = request.json.get('index')
    shortcuts = load_json(SHORTCUTS_FILE, [])
    if 0 <= idx < len(shortcuts): shortcuts.pop(idx)
    save_json(SHORTCUTS_FILE, shortcuts)
    return jsonify({"status": "ok"})

@app.route('/api/cast', methods=['POST'])
def cast_endpoint():
    global current_playing_url, current_playing_name
    data = request.json
    target = data.get('url')
    name = data.get('name', target)

    # 如果是文件夹，自动转到文件夹续播逻辑
    if os.path.isdir(target):
        return _cast_folder_impl(target)

    # 记录/读取历史：每次播放都更新时间戳，保留已有进度
    history = load_json(HISTORY_FILE, {})
    existing_pos = history.get(target, {}).get('pos', '00:00:00')
    history[target] = {"name": name, "pos": existing_pos, "time": time.time()}
    save_json(HISTORY_FILE, history)

    current_playing_url = target
    current_playing_name = name
    last_pos = existing_pos

    def process():
        real = target
        if os.path.isfile(target):
            fid = str(uuid.uuid4())
            local_files_map[fid] = target
            real = f"http://{LOCAL_IP}:{SERVER_PORT}/local/{fid}"
        elif not any(target.startswith(p) for p in ['http', 'https']):
            return

        if ENABLE_PROXY and any(d in real for d in ['bilibili', 'iqiyi', 'youku']):
            real = f"http://{LOCAL_IP}:{SERVER_PORT}/segment?url={quote(real)}"

        dlna_play(real, seek_to=last_pos)
    threading.Thread(target=process).start()
    return jsonify({"status": "ok"})

def _cast_folder_impl(folder_path):
    """文件夹续播核心逻辑，供 /api/cast 和 /api/cast_folder 共用"""
    global current_playing_url, current_playing_name
    if not os.path.isdir(folder_path):
        return jsonify({"status": "error", "msg": "路径无效"}), 400
    files = [f for f in os.listdir(folder_path) if f.lower().endswith(('.mp4', '.mkv', '.avi', '.ts'))]
    files.sort()
    if not files:
        return jsonify({"status": "error", "msg": "无视频"}), 404

    # 找到该文件夹内最近播放的一集（按历史时间戳）
    target_file = files[0]
    history = load_json(HISTORY_FILE, {})
    latest_time = 0
    for f in files:
        full_p = os.path.join(folder_path, f)
        if full_p in history and history[full_p]['time'] > latest_time:
            latest_time = history[full_p]['time']
            target_file = f

    full_path = os.path.join(folder_path, target_file)
    current_playing_url = full_path
    current_playing_name = target_file
    last_pos = history.get(full_path, {}).get('pos', '00:00:00')

    # 写入/更新历史记录
    history[full_path] = {"name": target_file, "pos": last_pos, "time": time.time()}
    save_json(HISTORY_FILE, history)

    fid = str(uuid.uuid4())
    local_files_map[fid] = full_path
    dlna_play(f"http://{LOCAL_IP}:{SERVER_PORT}/local/{fid}", seek_to=last_pos)
    return jsonify({"status": "ok", "count": len(files), "now": target_file})

@app.route('/api/cast_folder', methods=['POST'])
def cast_folder():
    folder_path = request.json.get('path')
    return _cast_folder_impl(folder_path)

@app.route('/local/<file_id>')
def serve_local_file(file_id):
    path = local_files_map.get(file_id)
    return send_file(path) if path and os.path.exists(path) else ("Not Found", 404)

# --- Web UI HTML ---
WEB_UI_HTML = """
<!DOCTYPE html>
<html>
<head>
    <title>Dollop Cast 中心</title>
    <meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, user-scalable=no">
    <style>
        * { box-sizing: border-box; margin: 0; padding: 0; }
        html, body { height: 100%; }

        /* ===== 基础 ===== */
        body {
            font-family: -apple-system, system-ui, sans-serif;
            background: #f0f2f5;
            color: #1c1e21;
            display: flex;
            flex-direction: column;
        }
        .card { background: white; border-radius: 12px; padding: 15px; box-shadow: 0 4px 12px rgba(0,0,0,0.08); }
        h2 { font-size: 1rem; display: flex; align-items: center; margin-bottom: 12px; }
        h2::before { content: '•'; color: #2196F3; font-size: 1.8rem; margin-right: 8px; }
        .btn { padding: 10px 18px; border: none; border-radius: 8px; background: #2196F3; color: white; font-weight: bold; width: 100%; margin-top: 10px; cursor: pointer; }
        .btn-small { padding: 6px 10px; font-size: 0.8rem; background: #607d8b; width: auto; }
        input[type="text"] { width: 100%; padding: 12px; border: 1px solid #ddd; border-radius: 8px; margin-bottom: 10px; }
        .device-item { display: flex; justify-content: space-between; align-items: center; padding: 12px; border-radius: 8px; background: #f8f9fa; margin-bottom: 8px; cursor: pointer; }
        .active { border-left: 5px solid #4CAF50; background: #e8f5e9; }

        /* ===== 顶部栏（固定）===== */
        .top-bar {
            position: fixed; top: 0; left: 0; right: 0; z-index: 200;
            height: 50px;
            display: flex; justify-content: space-between; align-items: center;
            padding: 0 15px;
            background: white;
            box-shadow: 0 2px 8px rgba(0,0,0,0.1);
        }
        .top-bar-title { font-weight: bold; font-size: 1rem; color: #333; }
        .mode-toggle { border: none; border-radius: 20px; padding: 7px 16px; font-size: 0.82rem; font-weight: bold; cursor: pointer; transition: all 0.2s; }
        .mode-toggle.settings { background: #fff3e0; color: #e65100; }
        .mode-toggle.use     { background: #e8f5e9; color: #2e7d32; }

        /* ===== 快捷方式 tiles ===== */
        .shortcut-grid {
            display: grid;
            grid-template-columns: repeat(auto-fill, minmax(110px, 1fr));
            gap: 10px;
        }
        .tile {
            position: relative;
            background: #e3f2fd;
            border-radius: 10px;
            display: flex; flex-direction: column; align-items: center; justify-content: center;
            text-align: center;
            cursor: pointer;
            border: 1px solid #bbdefb;
            padding: 12px 8px;
            transition: transform 0.13s;
            min-height: 80px;
            container-type: inline-size;
        }
        .tile:active { transform: scale(0.95); }
        .tile-icon { font-size: clamp(28px, 35cqw, 60px); margin-bottom: clamp(6px, 8cqw, 14px); line-height: 1; }
        .tile-name { font-weight: bold; font-size: clamp(0.85rem, 15cqw, 1.4rem); overflow: hidden; white-space: nowrap; text-overflow: ellipsis; width: 100%; }
        .tile-del { position: absolute; top: 4px; right: 4px; color: #ff5252; padding: 4px; font-size: 16px; line-height: 1; z-index: 10; background: rgba(255,255,255,0.7); border-radius: 50%; opacity: 0.8; }
        
        .icon-selector { display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 12px; }
        .icon-option { font-size: 26px; padding: 6px 8px; border-radius: 8px; cursor: pointer; border: 2px solid transparent; transition: 0.2s; user-select: none; }
        .icon-option:hover { background: #f0f0f0; }
        .icon-option.selected { border-color: #2196F3; background: #e3f2fd; }

        /* ===== 播放记录 ===== */
        .history-item { display: flex; justify-content: space-between; align-items: center; padding: 10px 12px; border-radius: 8px; background: #f8f9fa; margin-bottom: 6px; cursor: pointer; }
        .history-item:last-child { margin-bottom: 0; }

        /* ===== 设置模式：正常滚动布局 ===== */
        body.settings-mode {
            min-height: 100vh;
            padding: 65px 15px 15px;
            overflow-y: auto;
            gap: 15px;
        }
        body.settings-mode .shortcuts-card { }
        body.settings-mode .history-panel { }

        /* ===== 使用模式：充满视口布局 ===== */
        body.use-mode {
            height: 100vh;
            overflow: hidden;
            padding-top: 50px;          /* 顶部栏高度 */
            padding-bottom: 0;
        }

        /* 快捷区块：弹性撑满中间 */
        body.use-mode .shortcuts-card {
            flex: 1;
            min-height: 0;              /* 允许 flex 子项收缩 */
            display: flex;
            flex-direction: column;
            overflow: hidden;
            margin: 10px 10px 0;
            border-radius: 12px 12px 0 0;
        }
        body.use-mode .shortcuts-card h2 { flex: 0 0 auto; }
        body.use-mode .shortcut-grid {
            flex: 1;
            min-height: 0;
            overflow-y: auto;
            align-content: start;       /* 避免竖直方向被无限拉伸 */
            grid-template-columns: repeat(auto-fit, minmax(130px, 1fr));
            gap: 15px;
            padding: 5px;
        }
        body.use-mode .tile { 
            height: auto; 
            min-height: unset;
            aspect-ratio: 1 / 1;        /* 保持正方形 */
            justify-content: center;
        }

        /* 使用模式隐藏的元素 */
        body.use-mode .settings-only { display: none !important; }
        body.use-mode .tile-del { display: none; }

        /* 播放记录：固定在底部 */
        body.use-mode .history-panel {
            position: fixed;
            bottom: 0; left: 0; right: 0;
            z-index: 100;
            background: white;
            border-top: 1px solid #e0e0e0;
            box-shadow: 0 -4px 16px rgba(0,0,0,0.1);
            padding: 10px 10px 10px;
            max-height: 38vh;
            overflow-y: auto;
        }
        /* 快捷卡补偿底部历史面板的高度 */
        body.use-mode .shortcuts-card {
            /* JS 动态设置 padding-bottom */
        }
    </style>
</head>
<body class="use-mode">
    <!-- 顶部固定栏 -->
    <div class="top-bar">
        <div class="top-bar-title">📺 Dollop Cast</div>
        <button class="mode-toggle" id="mode-btn" onclick="toggleMode()"></button>
    </div>

    <!-- 设置模式专属：设备选择 -->
    <div class="card settings-only" id="device-card">
        <h2>📡 设备选择</h2>
        <div id="device-list"></div>
    </div>

    <!-- 快捷方式（两种模式都显示） -->
    <div class="card shortcuts-card" id="shortcuts-card">
        <h2>⭐ 快捷控制</h2>
        <div class="shortcut-grid" id="shortcut-grid"></div>
        <!-- 设置模式专属：添加表单 -->
        <div class="settings-only" style="margin-top:15px; border-top:1px solid #eee; padding-top:15px;">
            <div class="icon-selector" id="icon-selector">
                <span class="icon-option selected" onclick="selectIcon(this, '📁')">📁</span>
                <span class="icon-option" onclick="selectIcon(this, '📄')">📄</span>
                <span class="icon-option" onclick="selectIcon(this, '🌐')">🌐</span>
                <span class="icon-option" onclick="selectIcon(this, '👦')">👦</span>
                <span class="icon-option" onclick="selectIcon(this, '🧓')">🧓</span>
                <span class="icon-option" onclick="selectIcon(this, '🎬')">🎬</span>
                <span class="icon-option" onclick="selectIcon(this, '🦄')">🦄</span>
                <span class="icon-option" onclick="selectIcon(this, '🎵')">🎵</span>
                <span class="icon-option" onclick="selectIcon(this, '🎮')">🎮</span>
                <span class="icon-option" onclick="selectIcon(this, '📺')">📺</span>
            </div>
            <input type="text" id="sc-name" placeholder="起个名字">
            <input type="text" id="sc-path" placeholder="URL 或 本地路径">
            <button class="btn btn-small" onclick="browse('file')">📂 浏览文件</button>
            <button class="btn btn-small" onclick="browse('folder')">📁 浏览文件夹</button>
            <button class="btn" onclick="addSC()">添加</button>
        </div>
    </div>

    <!-- 播放记录 -->
    <div class="history-panel" id="history-panel">
        <h2 style="margin-bottom:10px;">📜 播放记录 <small style="font-size:0.72rem;font-weight:normal;color:#888;margin-left:4px;">自动续播</small></h2>
        <div id="history-list"></div>
    </div>

    <script>
        // ===== 模式切换 =====
        let useMode = localStorage.getItem('dollop_mode') !== 'settings';
        function applyMode() {
            if (useMode) {
                document.body.className = 'use-mode';
                document.getElementById('mode-btn').textContent = '⚙️ 设置模式';
                document.getElementById('mode-btn').className = 'mode-toggle settings';
                adjustLayout();
            } else {
                document.body.className = 'settings-mode';
                document.getElementById('mode-btn').textContent = '▶ 使用模式';
                document.getElementById('mode-btn').className = 'mode-toggle use';
                document.getElementById('shortcuts-card').style.paddingBottom = '';
            }
        }
        function toggleMode() {
            useMode = !useMode;
            localStorage.setItem('dollop_mode', useMode ? 'use' : 'settings');
            applyMode();
        }

        // 使用模式下：让快捷卡片底部留出播放记录面板的高度
        function adjustLayout() {
            if (!useMode) return;
            const hp = document.getElementById('history-panel');
            const hh = hp.offsetHeight;
            // 快捷卡的外部容器加 margin-bottom 避免被遮挡
            const sc = document.getElementById('shortcuts-card');
            sc.style.marginBottom = hh + 'px';
        }

        applyMode();
        window.addEventListener('resize', adjustLayout);

        let selectedIcon = '📁';
        function selectIcon(el, icon) {
            document.querySelectorAll('.icon-option').forEach(n => n.classList.remove('selected'));
            el.classList.add('selected');
            selectedIcon = icon;
        }

        // ===== 数据刷新 =====
        async function fetchStatus() {
            const res = await fetch('/api/status');
            const data = await res.json();
            // 设备列表
            document.getElementById('device-list').innerHTML = data.devices.map(d =>
                `<div class="device-item ${d === data.selected_device ? 'active' : ''}" onclick="selectDevice('${d}')"><span>${d}</span><small>${d === data.selected_device ? '✅ 已选' : '选择'}</small></div>`
            ).join('') || '<div style="color:#aaa;text-align:center;padding:10px;">未发现设备，请等待扫描…</div>';
            // 快捷方式
            document.getElementById('shortcut-grid').innerHTML = data.shortcuts.map((s, i) => {
                const safePath = encodeURIComponent(s.path);
                const safeName = encodeURIComponent(s.name);
                const icon = s.icon || (s.path.startsWith('http') ? '🌐' : (s.path.includes('.') ? '📄' : '📁'));
                return `<div class="tile" data-path="${safePath}" data-name="${safeName}" onclick="castShortcut(this)"><span class="tile-del" onclick="event.stopPropagation();delSC(${i})">×</span><div class="tile-icon">${icon}</div><div class="tile-name">${s.name}</div></div>`;
            }).join('') || '<div style="color:#aaa;font-size:0.9rem;padding:10px 0;">暂无快捷方式，切换到设置模式添加</div>';
            // 播放记录
            const hArray = Object.entries(data.history).sort((a,b) => b[1].time - a[1].time).slice(0, 8);
            document.getElementById('history-list').innerHTML = hArray.map(([url, h]) => {
                const safeUrl = encodeURIComponent(url);
                const safeName = encodeURIComponent(h.name);
                return `<div class="history-item" data-url="${safeUrl}" data-name="${safeName}" onclick="castFromHistory(this)"><div><b>${h.name}</b><br><small style="color:#888;">进度: ${h.pos}</small></div><span style="color:#2196F3;font-weight:bold;white-space:nowrap;margin-left:8px;">▶ 续播</span></div>`;
            }).join('') || '<div style="color:#aaa;text-align:center;padding:8px;">暂无记录</div>';
            // 使用模式时重新计算布局（记录条数变化会影响面板高度）
            if (useMode) setTimeout(adjustLayout, 50);
        }

        async function browse(type) { const res = await fetch('/api/browse/' + type); const data = await res.json(); if(data.path) document.getElementById('sc-path').value = data.path; }
        async function castShortcut(el) {
            const path = decodeURIComponent(el.dataset.path);
            const name = decodeURIComponent(el.dataset.name);
            await fetch('/api/cast', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({url: path, name}) });
            fetchStatus();
        }
        async function addSC() { const name = document.getElementById('sc-name').value; const path = document.getElementById('sc-path').value; if(!name||!path) return; await fetch('/api/shortcuts', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({name, path, icon: selectedIcon}) }); document.getElementById('sc-name').value=''; document.getElementById('sc-path').value=''; fetchStatus(); }
        async function delSC(i) { await fetch('/api/shortcuts/delete', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({index: i}) }); fetchStatus(); }
        async function selectDevice(n) { await fetch('/api/config', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({selected_device: n}) }); fetchStatus(); }
        async function cast(u, n) { await fetch('/api/cast', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({url: u, name: n}) }); fetchStatus(); }
        async function castFromHistory(el) { const u = decodeURIComponent(el.dataset.url); const n = decodeURIComponent(el.dataset.name); await cast(u, n); }
        fetchStatus(); setInterval(fetchStatus, 5000);
    </script>
</body>
</html>
"""

def main():
    threading.Thread(target=lambda: app.run(host='0.0.0.0', port=5000, debug=False, use_reloader=False), daemon=True).start()
    threading.Thread(target=scan_devices_loop, daemon=True).start()
    threading.Thread(target=position_polling_loop, daemon=True).start() # 启动进度轮询
    root = tk.Tk(); root.title("Dollop Cast Server"); root.geometry("300x150")
    tk.Label(root, text="影音控制中心已启动", font=("Arial", 10, "bold")).pack(pady=10)
    tk.Label(root, text=f"管理地址: http://{LOCAL_IP}:5000").pack()
    tk.Button(root, text="打开管理面板", command=lambda: webbrowser.open(f"http://localhost:5000")).pack(pady=15)
    root.mainloop()

if __name__ == '__main__':
    main()
