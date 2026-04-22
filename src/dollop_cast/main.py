import os
import sys
import threading
import time
import socket
import requests
import uuid
import json
import xml.etree.ElementTree as ET
from urllib.parse import urlparse, quote
from collections import OrderedDict
from flask import Flask, request, jsonify, send_from_directory, send_file, render_template_string
from flask_cors import CORS
from .stream_proxy import proxy_bp

# --- 配置与存储 ---
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DATA_DIR = os.path.join(PROJECT_ROOT, 'data')
if not os.path.exists(DATA_DIR): os.makedirs(DATA_DIR)

HISTORY_FILE = os.path.join(DATA_DIR, 'history.json')
SHORTCUTS_FILE = os.path.join(DATA_DIR, 'shortcuts.json')
CONFIG_FILE  = os.path.join(DATA_DIR, 'config.json')

def load_json(path, default):
    if os.path.exists(path):
        try:
            with open(path, 'r', encoding='utf-8') as f: return json.load(f)
        except: return default
    return default

def save_json(path, data):
    with open(path, 'w', encoding='utf-8') as f: json.dump(data, f, ensure_ascii=False, indent=2)

# 从持久化配置加载
_cfg = load_json(CONFIG_FILE, {})
MEDIA_ROOT = _cfg.get('media_root', os.path.expanduser('~'))
ENABLE_PROXY = _cfg.get('proxy', True)
LOCAL_DEBUG_MODE = _cfg.get('debug', False)
SKIP_ENDING_SECONDS = _cfg.get('skip_ending', 20)
SKIP_START_SECONDS = _cfg.get('skip_start', 0)

SERVER_PORT = _cfg.get('server_port', 5000)


# --- 全局状态 ---
found_devices = OrderedDict()
selected_device_name = None
current_control_url = None
local_files_map = {}
current_playing_url = None
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

def _trigger_next_episode(current_target):
    if not current_target or current_target.startswith('http'): return
    if os.path.isfile(current_target):
        folder = os.path.dirname(current_target)
        filename = os.path.basename(current_target)
        try:
            files = [f for f in os.listdir(folder) if f.lower().endswith(('.mp4', '.mkv', '.avi', '.ts', '.mov', '.flv'))]
            files.sort()
            idx = files.index(filename)
            if idx + 1 < len(files):
                next_file = files[idx + 1]
                next_path = os.path.join(folder, next_file)
                print(f"[*] 电视播放完毕，自动投屏播下一集: {next_file}")
                requests.post(f"http://127.0.0.1:{SERVER_PORT}/api/cast", json={"url": next_path, "name": next_file}, timeout=5)
        except: pass

def position_polling_loop():
    """后台轮询电视当前的播放进度并检查自动下一集"""
    global current_playing_url, current_control_url
    
    def time_to_sec(t_str):
        try: return sum(x * int(t) for x, t in zip([3600, 60, 1], str(t_str).split(":")))
        except: return 0

    last_url = None
    last_rel_sec = 0
    last_dur_sec = 0

    while True:
        if current_control_url and current_playing_url:
            try:
                headers = {'Content-Type': 'text/xml; charset="utf-8"', 'SOAPACTION': '"urn:schemas-upnp-org:service:AVTransport:1#GetPositionInfo"'}
                body = '<?xml version="1.0"?><s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body><u:GetPositionInfo xmlns:u="urn:schemas-upnp-org:service:AVTransport:1"><InstanceID>0</InstanceID></u:GetPositionInfo></s:Body></s:Envelope>'
                resp = requests.post(current_control_url, data=body, headers=headers, timeout=3)
                
                if resp.status_code == 200:
                    content = resp.text
                    start = content.find("<RelTime>") + 9
                    end = content.find("</RelTime>")
                    dur_start = content.find("<TrackDuration>") + 15
                    dur_end = content.find("</TrackDuration>")
                    
                    if start > 8 and end > start:
                        rel_time = content[start:end]
                        if dur_start > 14 and dur_end > dur_start:
                            dur_time = content[dur_start:dur_end]
                            dur_sec = time_to_sec(dur_time)
                            rel_sec = time_to_sec(rel_time)

                            if rel_time != "00:00:00" and "NOT_IMPLEMENTED" not in rel_time:
                                update_history_pos(current_playing_url, rel_time)

                                # 宽松距离阈值匹配：跳过片尾逻辑，距离末尾小于 SKIP_ENDING_SECONDS 则立刻播下一集
                                if dur_sec > 0 and (dur_sec - rel_sec) <= SKIP_ENDING_SECONDS:
                                    _trigger_next = current_playing_url
                                    current_playing_url = None # 防止重复触发
                                    threading.Thread(target=lambda: _trigger_next_episode(_trigger_next)).start()
                                    time.sleep(2)
                                    continue
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
    return render_template_string(WEB_UI_HTML, local_ip=LOCAL_IP, port=SERVER_PORT, skip_ending=SKIP_ENDING_SECONDS, skip_start=SKIP_START_SECONDS)

@app.route('/api/status')
def get_status():
    return jsonify({
        "devices": list(found_devices.keys()),
        "selected_device": selected_device_name,
        "proxy": ENABLE_PROXY,
        "debug": LOCAL_DEBUG_MODE,
        "skip_ending": SKIP_ENDING_SECONDS,
        "skip_start": SKIP_START_SECONDS,
        "media_root": MEDIA_ROOT,
        "history": load_json(HISTORY_FILE, {}),
        "shortcuts": load_json(SHORTCUTS_FILE, [])
    })

@app.route('/api/stream_local')
def stream_local():
    path = request.args.get('path', '')
    if not path or not os.path.exists(path):
        return "Not found", 404
    # Simple security scope check
    global MEDIA_ROOT
    if not os.path.normpath(path).startswith(os.path.normpath(MEDIA_ROOT)):
        return "Access denied", 403
    return send_file(path, conditional=True)

@app.route('/api/history', methods=['POST'])
def update_history_api():
    data = request.json
    url = data.get('url')
    name = data.get('name')
    sec = data.get('pos_seconds', 0)
    hh = int(sec) // 3600
    mm = (int(sec) % 3600) // 60
    ss = int(sec) % 60
    pos_str = f"{hh:02}:{mm:02}:{ss:02}"
    hist = load_json(HISTORY_FILE, {})
    hist[url] = {
        "name": name,
        "pos": pos_str,
        "time": time.time()
    }
    save_json(HISTORY_FILE, hist)
    return jsonify({"status": "ok"})

@app.route('/api/next_episode', methods=['POST'])
def get_next_episode():
    target = request.json.get('url')
    if not target or target.startswith('http'):
        return jsonify({"status": "error", "msg": "不支持的网络流"})
    if os.path.isfile(target):
        folder = os.path.dirname(target)
        filename = os.path.basename(target)
        try:
            files = [f for f in os.listdir(folder) if f.lower().endswith(('.mp4', '.mkv', '.avi', '.ts', '.mov', '.flv'))]
            files.sort()
            idx = files.index(filename)
            if idx + 1 < len(files):
                next_file = files[idx + 1]
                next_path = os.path.join(folder, next_file)
                return jsonify({"status": "ok", "url": next_path, "name": next_file})
        except: pass
    return jsonify({"status": "error", "msg": "已经是最后一集或无本地文件"})

@app.route('/api/resolve', methods=['POST'])
def resolve_playable():
    """将给定的目标（文件或文件夹）解析为确切的视频文件信息（供本地/投屏通用）"""
    data = request.json
    target = data.get('url')
    name = data.get('name', target)
    if os.path.exists(target) and os.path.isdir(target):
        files = [f for f in os.listdir(target) if f.lower().endswith(('.mp4', '.mkv', '.avi', '.ts', '.mov', '.flv'))]
        files.sort()
        if not files:
            return jsonify({"status": "error", "msg": "文件夹内无视频"}), 404
        target_file = files[0]
        history = load_json(HISTORY_FILE, {})
        latest_time = 0
        for f in files:
            full_p = os.path.join(target, f)
            if full_p in history and history[full_p]['time'] > latest_time:
                latest_time = history[full_p]['time']
                target_file = f
        target = os.path.join(target, target_file)
        name = target_file
    return jsonify({"status": "ok", "url": target, "name": name})

@app.route('/api/files')
def list_files():
    """列出目录内容，路径被沙盒锁定在 MEDIA_ROOT 内"""
    global MEDIA_ROOT
    req_path = request.args.get('path', '')
    if req_path:
        if os.path.isabs(req_path):
            target = os.path.normpath(req_path)
        else:
            target = os.path.normpath(os.path.join(MEDIA_ROOT, req_path))
    else:
        target = os.path.normpath(MEDIA_ROOT)
    # 安全检查：不允许跳出 MEDIA_ROOT
    if not target.startswith(os.path.normpath(MEDIA_ROOT)):
        return jsonify({'error': 'access denied'}), 403
    try:
        entries = []
        for name in sorted(os.listdir(target), key=lambda x: (not os.path.isdir(os.path.join(target, x)), x.lower())):
            full = os.path.join(target, name)
            is_dir = os.path.isdir(full)
            if name.startswith('.'): continue   # 隐藏文件
            if is_dir or name.lower().endswith(('.mp4', '.mkv', '.avi', '.ts', '.mov', '.m4v', '.wmv', '.flv')):
                entries.append({'name': name, 'is_dir': is_dir, 'path': full})
        parent = os.path.dirname(target) if os.path.normpath(target) != os.path.normpath(MEDIA_ROOT) else None
        return jsonify({'current': target, 'parent': parent, 'root': MEDIA_ROOT, 'entries': entries})
    except PermissionError:
        return jsonify({'error': '无权限访问此目录'}), 403
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/config', methods=['POST'])
def update_config():
    global ENABLE_PROXY, LOCAL_DEBUG_MODE, MEDIA_ROOT, SKIP_ENDING_SECONDS, SKIP_START_SECONDS
    data = request.json
    cfg = load_json(CONFIG_FILE, {})
    dirty = False

    if 'proxy' in data: 
        ENABLE_PROXY = data['proxy']
        cfg['proxy'] = ENABLE_PROXY
        dirty = True
    if 'debug' in data: 
        LOCAL_DEBUG_MODE = data['debug']
        cfg['debug'] = LOCAL_DEBUG_MODE
        dirty = True
    if 'skip_ending' in data:
        try:
            val = int(data['skip_ending'])
            SKIP_ENDING_SECONDS = val
            cfg['skip_ending'] = val
            dirty = True
        except: pass
    if 'skip_start' in data:
        try:
            val = int(data['skip_start'])
            SKIP_START_SECONDS = val
            cfg['skip_start'] = val
            dirty = True
        except: pass

    if 'selected_device' in data: 
        select_device(data['selected_device'])

    if 'media_root' in data:
        new_root = data['media_root'].strip()
        if os.path.isdir(new_root):
            MEDIA_ROOT = new_root
            cfg['media_root'] = MEDIA_ROOT
            dirty = True
        else:
            return jsonify({'status': 'error', 'msg': '路径不存在'}), 400

    if dirty:
        save_json(CONFIG_FILE, cfg)
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
    
    if existing_pos == '00:00:00' and SKIP_START_SECONDS > 0:
        existing_pos = f"{SKIP_START_SECONDS//3600:02d}:{(SKIP_START_SECONDS%3600)//60:02d}:{SKIP_START_SECONDS%60:02d}"

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
    
    if last_pos == '00:00:00' and SKIP_START_SECONDS > 0:
        last_pos = f"{SKIP_START_SECONDS//3600:02d}:{(SKIP_START_SECONDS%3600)//60:02d}:{SKIP_START_SECONDS%60:02d}"

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
        .btn-browse { 
            flex-shrink: 0; width: 42px; height: 42px;
            border: 1px solid #ddd; border-radius: 8px;
            background: #f5f5f5; cursor: pointer; font-size: 18px;
            display: flex; align-items: center; justify-content: center;
            transition: background 0.15s;
        }
        .btn-browse:hover { background: #e3f2fd; border-color: #90caf9; }
        .btn-browse:disabled { opacity: 0.5; cursor: not-allowed; }

        /* ===== 开关组件 ===== */
        .switch-box { display: flex; align-items: center; justify-content: space-between; margin-bottom: 12px; }
        .switch-label { font-size: 1.05rem; font-weight: bold; }
        .switch { position: relative; display: inline-block; width: 50px; height: 28px; }
        .switch input { opacity: 0; width: 0; height: 0; }
        .slider { position: absolute; cursor: pointer; top: 0; left: 0; right: 0; bottom: 0; background-color: #ccc; transition: .4s; border-radius: 28px; }
        .slider:before { position: absolute; content: ""; height: 20px; width: 20px; left: 4px; bottom: 4px; background-color: white; transition: .4s; border-radius: 50%; }
        input:checked + .slider { background-color: #2196F3; }
        input:checked + .slider:before { transform: translateX(22px); }

        /* ===== 文件选择器弹窗 ===== */
        .picker-overlay {
            display: none; position: fixed; inset: 0; z-index: 500;
            background: rgba(0,0,0,0.45); align-items: flex-end; justify-content: center;
        }
        .picker-overlay.open { display: flex; }
        .picker-sheet {
            background: white; border-radius: 20px 20px 0 0;
            width: 100%; max-width: 600px; max-height: 75vh;
            display: flex; flex-direction: column;
            box-shadow: 0 -8px 30px rgba(0,0,0,0.2);
            animation: slideUp 0.25s ease;
        }
        @keyframes slideUp { from { transform: translateY(100%); } to { transform: translateY(0); } }
        .picker-header {
            padding: 14px 16px 10px; border-bottom: 1px solid #eee;
            display: flex; align-items: center; gap: 8px; flex-shrink: 0;
        }
        .picker-header button { border: none; background: none; font-size: 20px; cursor: pointer; padding: 4px; border-radius: 6px; }
        .picker-header button:hover { background: #f0f0f0; }
        .picker-breadcrumb { flex: 1; font-size: 0.8rem; color: #666; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; direction: rtl; text-align: left; }
        .picker-select-btn { border: none; background: #e3f2fd; color: #1565c0; border-radius: 8px; padding: 6px 12px; font-weight: bold; cursor: pointer; font-size: 0.82rem; white-space: nowrap; }
        .picker-body { overflow-y: auto; flex: 1; }
        .picker-entry {
            display: flex; align-items: center; gap: 12px;
            padding: 13px 16px; cursor: pointer; border-bottom: 1px solid #f5f5f5;
            transition: background 0.1s;
        }
        .picker-entry:hover { background: #f0f7ff; }
        .picker-entry-icon { font-size: 22px; flex-shrink: 0; width: 28px; text-align: center; }
        .picker-entry-name { flex: 1; font-size: 0.95rem; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
        .picker-entry-arrow { color: #bbb; flex-shrink: 0; }
        .picker-empty { text-align: center; padding: 30px; color: #aaa; font-size: 0.9rem; }
        .picker-loading { text-align: center; padding: 30px; color: #888; }

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

    <!-- 设置模式专属：系统设定 -->
    <div class="card settings-only" id="system-settings-card">
        <h2>⚙️ 系统设定</h2>
        <div style="display:flex; flex-direction:column; gap:10px; margin-top:10px;">
            <div class="switch-box" style="margin-bottom:0px;">
                <div class="switch-label" style="font-size: 1rem; font-weight: normal;">启用流媒体代理解析 (推荐)</div>
                <label class="switch">
                    <input type="checkbox" id="setting-proxy" onchange="updateSettings()">
                    <span class="slider"></span>
                </label>
            </div>
            <div class="switch-box" style="margin-bottom:0px;">
                <div class="switch-label" style="font-size: 1rem; font-weight: normal;">启用本地调试模式</div>
                <label class="switch">
                    <input type="checkbox" id="setting-debug" onchange="updateSettings()">
                    <span class="slider"></span>
                </label>
            </div>
            <label style="display:flex; align-items:center; margin-top:5px;">
                <span style="width: 130px;">跳过片头秒数:</span>
                <input type="number" id="setting-skip-start" onchange="updateSettings()" style="width:60px; padding:4px;" min="0">
            </label>
            <label style="display:flex; align-items:center;">
                <span style="width: 130px;">跳过片尾秒数:</span>
                <input type="number" id="setting-skip" onchange="updateSettings()" style="width:60px; padding:4px;" min="0">
            </label>
        </div>
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
            <input type="text" id="sc-name" placeholder="起个名字" style="margin-bottom:10px;">
            <div style="display:flex; gap:8px; align-items:center; margin-bottom:10px;">
                <input type="text" id="sc-path" placeholder="URL 或 本地路径" style="flex:1; margin:0;">
                <button class="btn-browse" onclick="openPicker()" title="浏览">📂</button>
            </div>
            <button class="btn" onclick="addSC()">添加</button>
        </div>
    </div>

    <!-- 檒体根目录设置 -->
    <div class="card settings-only" id="media-root-card">
        <h2>🗂️ 媒体目录</h2>
        <div style="display:flex; gap:8px; align-items:center;">
            <input type="text" id="media-root-input" placeholder="/path/to/media" style="flex:1; margin:0;">
            <button class="btn" style="width:auto; margin:0; padding:10px 16px;" onclick="saveMediaRoot()">保存</button>
        </div>
        <small id="media-root-hint" style="color:#888; font-size:0.78rem; display:block; margin-top:6px;"></small>
    </div>

    <!-- 播放记录 -->
    <div class="history-panel" id="history-panel">
        <div class="switch-box use-only" style="margin-bottom:15px; border-bottom:1px solid #eee; padding-bottom:8px;">
            <div class="switch-label">📺 设备投放 <small style="color:#888; font-weight:normal; display:block; font-size:0.8rem; margin-top:2px;">开启投屏，关闭则在网页本地播放</small></div>
            <label class="switch">
                <input type="checkbox" id="cast-toggle" onchange="toggleCastMode()">
                <span class="slider"></span>
            </label>
        </div>
        <h2 style="margin-bottom:10px;">📜 播放记录 <small style="font-size:0.72rem;font-weight:normal;color:#888;margin-left:4px;">自动续播</small></h2>
        <div id="history-list"></div>
    </div>

    <!-- WEB 播放器弹窗 -->
    <div class="picker-overlay" id="player-overlay" style="z-index: 999; flex-direction: column; background: black;">
        <div style="width:100%; display:flex; justify-content:space-between; align-items:center; padding:15px; color:white; flex-shrink:0;">
            <div id="player-title" style="font-weight:bold; font-size:1.1rem; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; max-width: 80%;">Video Title</div>
            <button onclick="closePlayer()" style="background:none; border:none; color:white; font-size:28px; cursor:pointer; padding: 0 10px;">✕</button>
        </div>
        <div style="flex:1; width:100%; display:flex; justify-content:center; align-items:center;">
            <video id="web-video-player" controls autoplay onended="playNextWebVideo()" style="max-width:100%; max-height:100%; width:100%; outline: none;"></video>
        </div>
    </div>

    <!-- 文件选择器弹窗 -->
    <div class="picker-overlay" id="picker-overlay" onclick="if(event.target===this)closePicker()">
        <div class="picker-sheet">
            <div class="picker-header">
                <button onclick="pickerGoUp()" id="picker-up-btn" title="返回上级">⬆️</button>
                <span class="picker-breadcrumb" id="picker-breadcrumb"></span>
                <button class="picker-select-btn" id="picker-select-folder-btn" onclick="pickerSelectFolder()">选此文件夹</button>
                <button onclick="closePicker()" title="关闭">✕</button>
            </div>
            <div class="picker-body" id="picker-body"></div>
        </div>
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

        let appHistory = {};

        // ===== 数据刷新 =====
        async function fetchStatus() {
            const res = await fetch('/api/status');
            const data = await res.json();
            
            appHistory = data.history;

            if (document.getElementById('setting-proxy')) {
                document.getElementById('setting-proxy').checked = data.proxy;
                document.getElementById('setting-debug').checked = data.debug;
                document.getElementById('setting-skip-start').value = data.skip_start;
                document.getElementById('setting-skip').value = data.skip_ending;
            }

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

        // ===== 文件选择器 =====
        let pickerCurrentPath = '';
        let pickerRoot = '';

        async function openPicker() {
            document.getElementById('picker-overlay').classList.add('open');
            await navigatePicker('');
        }
        function closePicker() {
            document.getElementById('picker-overlay').classList.remove('open');
        }
        async function navigatePicker(path) {
            pickerCurrentPath = path;
            document.getElementById('picker-body').innerHTML = '<div class="picker-loading">加载中…</div>';
            try {
                const res = await fetch('/api/files' + (path ? '?path=' + encodeURIComponent(path) : ''));
                const data = await res.json();
                if (data.error) { document.getElementById('picker-body').innerHTML = `<div class="picker-empty">${data.error}</div>`; return; }
                pickerRoot = data.root;
                // 面包屑
                const rel = data.current.startsWith(data.root) ? data.current.slice(data.root.length) : data.current;
                document.getElementById('picker-breadcrumb').textContent = rel || '/';
                // 上级按钮
                document.getElementById('picker-up-btn').disabled = !data.parent;
                // 选文件夹按钮（总是允许选择当前目录）
                document.getElementById('picker-select-folder-btn').style.display = '';
                // 条目列表
                if (!data.entries.length) {
                    document.getElementById('picker-body').innerHTML = '<div class="picker-empty">此目录为空</div>';
                    return;
                }
                document.getElementById('picker-body').innerHTML = data.entries.map(e => {
                    const safeP = encodeURIComponent(e.path);
                    if (e.is_dir) {
                        return `<div class="picker-entry" onclick="navigatePicker(decodeURIComponent('${safeP}'))">
                            <span class="picker-entry-icon">📁</span>
                            <span class="picker-entry-name">${e.name}</span>
                            <span class="picker-entry-arrow">›</span>
                        </div>`;
                    } else {
                        return `<div class="picker-entry" onclick="pickerSelectFile('${safeP}','${encodeURIComponent(e.name)}')">
                            <span class="picker-entry-icon">🎬</span>
                            <span class="picker-entry-name">${e.name}</span>
                            <span class="picker-entry-arrow" style="color:#2196F3;font-size:0.8rem;">选择</span>
                        </div>`;
                    }
                }).join('');
                // 重定向路径存储到真实路径
                pickerCurrentPath = data.current;
            } catch(err) {
                document.getElementById('picker-body').innerHTML = `<div class="picker-empty">加载失败: ${err.message}</div>`;
            }
        }
        async function pickerGoUp() {
            const res = await fetch('/api/files' + (pickerCurrentPath ? '?path=' + encodeURIComponent(pickerCurrentPath) : ''));
            const data = await res.json();
            if (data.parent) await navigatePicker(data.parent);
        }
        function pickerSelectFile(safeP, safeName) {
            const path = decodeURIComponent(safeP);
            const name = decodeURIComponent(safeName);
            document.getElementById('sc-path').value = path;
            const nameEl = document.getElementById('sc-name');
            if (!nameEl.value) nameEl.value = name.replace(/\\.[^.]+$/, '');
            closePicker();
        }
        function pickerSelectFolder() {
            document.getElementById('sc-path').value = pickerCurrentPath;
            const nameEl = document.getElementById('sc-name');
            if (!nameEl.value) nameEl.value = pickerCurrentPath.split('/').filter(Boolean).pop() || '';
            closePicker();
        }

        // ===== 媒体目录设置 =====
        async function saveMediaRoot() {
            const val = document.getElementById('media-root-input').value.trim();
            if (!val) return;
            const res = await fetch('/api/config', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({media_root: val}) });
            const data = await res.json();
            const hint = document.getElementById('media-root-hint');
            if (data.status === 'ok') { hint.style.color='#388e3c'; hint.textContent='✅ 已保存: ' + val; }
            else { hint.style.color='#d32f2f'; hint.textContent='❌ ' + (data.msg||'失败'); }
        }

        // ===== 本地播放/投放逻辑 =====
        let isCastEnabled = localStorage.getItem('cast_enabled') !== 'false';
        document.getElementById('cast-toggle').checked = isCastEnabled;

        function toggleCastMode() {
            isCastEnabled = document.getElementById('cast-toggle').checked;
            localStorage.setItem('cast_enabled', isCastEnabled);
        }

        async function playVideo(path, name) {
            try {
                const rRes = await fetch('/api/resolve', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({url: path, name: name}) });
                const rData = await rRes.json();
                if (rData.status !== 'ok') { alert(rData.msg || "解析失败"); return; }
                path = rData.url;
                name = rData.name;
            } catch(e) { }

            if (isCastEnabled) {
                await fetch('/api/cast', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({url: path, name}) });
                fetchStatus();
            } else {
                openWebPlayer(path, name);
            }
        }

        let webPlayerInterval = null;
        let activePlayerUrl = '';
        let activePlayerName = '';

        function openWebPlayer(path, name) {
            activePlayerUrl = path;
            activePlayerName = name;
            const overlay = document.getElementById('player-overlay');
            const video = document.getElementById('web-video-player');
            document.getElementById('player-title').textContent = name;
            
            let videoSrc = path;
            if (!path.startsWith('http')) {
                videoSrc = '/api/stream_local?path=' + encodeURIComponent(path);
            }
            video.src = videoSrc;
            
            // 恢复进度
            if (appHistory[path] && appHistory[path].pos && appHistory[path].pos !== '00:00:00') {
                const parts = appHistory[path].pos.split(':').reverse();
                let seconds = 0;
                for (let i = 0; i < parts.length; i++) seconds += parseInt(parts[i] || 0) * Math.pow(60, i);
                if (seconds > 0) video.currentTime = seconds;
            } else if ({{skip_start}} > 0) {
                video.currentTime = {{skip_start}};
            }
            overlay.classList.add('open');
            video.play();

            video.dataset.nextTriggered = "0";
            video.ontimeupdate = () => {
                if (video.duration && (video.duration - video.currentTime <= {{skip_ending}})) {
                    if (video.dataset.nextTriggered !== "1") {
                        video.dataset.nextTriggered = "1";
                        playNextWebVideo();
                    }
                }
            };

            // 定时记录历史
            if(webPlayerInterval) clearInterval(webPlayerInterval);
            webPlayerInterval = setInterval(() => {
                if(!video.paused) {
                    fetch('/api/history', { method:'POST', headers:{'Content-Type':'application/json'}, 
                        body: JSON.stringify({url: activePlayerUrl, name: activePlayerName, pos_seconds: video.currentTime}) 
                    });
                }
            }, 5000);
        }

        function closePlayer() {
            const video = document.getElementById('web-video-player');
            video.pause();
            if(webPlayerInterval) clearInterval(webPlayerInterval);
            fetch('/api/history', { method:'POST', headers:{'Content-Type':'application/json'}, 
                body: JSON.stringify({url: activePlayerUrl, name: activePlayerName, pos_seconds: video.currentTime}) 
            }).then(() => fetchStatus()); // 最后更新一次
            video.src = '';
            document.getElementById('player-overlay').classList.remove('open');
        }

        async function playNextWebVideo() {
            if (!activePlayerUrl) return;
            const res = await fetch('/api/next_episode', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({url: activePlayerUrl}) });
            const data = await res.json();
            if (data.status === 'ok') {
                playVideo(data.url, data.name);
            } else {
                closePlayer();
            }
        }

        async function castShortcut(el) { playVideo(decodeURIComponent(el.dataset.path), decodeURIComponent(el.dataset.name)); }
        async function castFromHistory(el) { playVideo(decodeURIComponent(el.dataset.url), decodeURIComponent(el.dataset.name)); }

        async function addSC() { const name = document.getElementById('sc-name').value; const path = document.getElementById('sc-path').value; if(!name||!path) return; await fetch('/api/shortcuts', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({name, path, icon: selectedIcon}) }); document.getElementById('sc-name').value=''; document.getElementById('sc-path').value=''; fetchStatus(); }
        async function delSC(i) { await fetch('/api/shortcuts/delete', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({index: i}) }); fetchStatus(); }
        async function selectDevice(n) { await fetch('/api/config', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({selected_device: n}) }); fetchStatus(); }
        
        async function updateSettings() {
            const proxy = document.getElementById('setting-proxy').checked;
            const debug = document.getElementById('setting-debug').checked;
            const skip_start = parseInt(document.getElementById('setting-skip-start').value) || 0;
            const skip_end = parseInt(document.getElementById('setting-skip').value) || 0;
            await fetch('/api/config', { 
                method: 'POST', 
                headers: {'Content-Type': 'application/json'}, 
                body: JSON.stringify({proxy: proxy, debug: debug, skip_start: skip_start, skip_ending: skip_end}) 
            });
            fetchStatus();
        }

        fetchStatus(); setInterval(fetchStatus, 5000);
    </script>
</body>
</html>
"""

def run_server():
    print("=======================================")
    print("📺 Dollop Cast 影音控制中心已启动")
    print(f"🔗 管理地址: http://{LOCAL_IP}:{SERVER_PORT}")
    print("=======================================")
    threading.Thread(target=scan_devices_loop, daemon=True).start()
    threading.Thread(target=position_polling_loop, daemon=True).start()
    from waitress import serve
    # app.run(host='0.0.0.0', port=SERVER_PORT, debug=False, use_reloader=False)
    serve(app, host='0.0.0.0', port=SERVER_PORT)

if __name__ == '__main__':
    run_server()
