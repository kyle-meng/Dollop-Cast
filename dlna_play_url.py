import sys
import socket
import requests
import xml.etree.ElementTree as ET
from urllib.parse import urlparse

# --- 配置 ---
# 请替换为您电视的实际 IP 和 Description URL
TARGET_DEVICE_IP = "192.168.1.224"
TARGET_DEVICE_DESC_URL = "http://192.168.1.224:49152/description.xml"

def get_av_transport_url(desc_url):
    try:
        print(f"正在连接设备: {desc_url}")
        response = requests.get(desc_url, timeout=5)
        response.raise_for_status()
        
        root = ET.fromstring(response.content)
        
        # 命名空间处理
        namespaces = {
            'tns': 'urn:schemas-upnp-org:device-1-0',
            's': 'urn:schemas-upnp-org:service:AVTransport:1'
        }
        
        for service in root.iter():
            # 简单查找服务类型
            s_type = service.findtext("{urn:schemas-upnp-org:device-1-0}serviceType") or \
                     service.findtext("serviceType")
            
            if s_type and 'AVTransport' in s_type:
                # 获取控制 URL
                c_url = service.findtext("{urn:schemas-upnp-org:device-1-0}controlURL") or \
                        service.findtext("controlURL")
                
                if c_url:
                    if not c_url.startswith('http'):
                        parsed = urlparse(desc_url)
                        base = f"{parsed.scheme}://{parsed.netloc}"
                        if c_url.startswith('/'):
                            c_url = base + c_url
                        else:
                            c_url = base + "/" + c_url
                    return c_url
    except Exception as e:
        print(f"获取控制 URL 失败: {e}")
    return None

def play_video(control_url, video_url):
    print(f"正在投送视频: {video_url}")
    
    headers = {
        'Content-Type': 'text/xml; charset="utf-8"',
        'SOAPACTION': '"urn:schemas-upnp-org:service:AVTransport:1#SetAVTransportURI"'
    }
    
    # 1. 设置播放地址 (SetAVTransportURI)
    body_set_uri = f"""<?xml version="1.0"?>
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
        requests.post(control_url, data=body_set_uri, headers=headers)
        print("视频地址已发送。")
    except Exception as e:
        print(f"发送视频地址失败: {e}")
        return

    # 2. 发送播放指令 (Play)
    headers['SOAPACTION'] = '"urn:schemas-upnp-org:service:AVTransport:1#Play"'
    body_play = """<?xml version="1.0"?>
    <s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">
        <s:Body>
            <u:Play xmlns:u="urn:schemas-upnp-org:service:AVTransport:1">
                <InstanceID>0</InstanceID>
                <Speed>1</Speed>
            </u:Play>
        </s:Body>
    </s:Envelope>"""
    
    try:
        requests.post(control_url, data=body_play, headers=headers)
        print("播放指令已发送！请查看电视。")
    except Exception as e:
        print(f"发送播放指令失败: {e}")

if __name__ == "__main__":
    # 获取参数中的 URL
    if len(sys.argv) < 2:
        print("用法: python dlna_play_url.py <视频URL>")
        print("示例: python dlna_play_url.py http://example.com/video.mp4")
        
        # 交互式输入
        video_url = input("\n请输入要投屏的视频地址 (URL): ").strip()
        if not video_url:
            print("未输入地址，退出。")
            exit(1)
    else:
        video_url = sys.argv[1]

    # 获取控制 URL (复用之前的逻辑)
    control_url = get_av_transport_url(TARGET_DEVICE_DESC_URL)
    
    if control_url:
        play_video(control_url, video_url)
    else:
        print("无法连接到电视，请检查 IP 设置。")


# https://hn.bfvvs.com/play/hls/b82q0wje/index.m3u8