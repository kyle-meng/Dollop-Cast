import socket
import struct

def scan_dlna_devices():
    print("正在扫描局域网内的 DLNA 设备...")
    
    # SSDP 组播地址和端口
    MCAST_GRP = '239.255.255.250'
    MCAST_PORT = 1900
    
    # 构建 SSDP M-SEARCH 请求包
    # 搜索所有设备 (ssdp:all) 或者特定媒体渲染器 (urn:schemas-upnp-org:device:MediaRenderer:1)
    msg = (
        'M-SEARCH * HTTP/1.1\r\n'
        'HOST: 239.255.255.250:1900\r\n'
        'MAN: "ssdp:discover"\r\n'
        'MX: 3\r\n'
        'ST: urn:schemas-upnp-org:device:MediaRenderer:1\r\n' # 只搜索媒体渲染设备(电视、音箱等)
        '\r\n'
    )

    # 创建 UDP 套接字
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.settimeout(5) # 扫描 5 秒
    
    try:
        # 发送组播请求
        sock.sendto(msg.encode('utf-8'), (MCAST_GRP, MCAST_PORT))
        
        found_devices = []
        
        while True:
            try:
                data, addr = sock.recvfrom(65507)
                print(data)
                response = data.decode('utf-8', errors='ignore')
                
                # 简单解析响应头
                headers = {}
                for line in response.split('\r\n'):
                    if ':' in line:
                        key, value = line.split(':', 1)
                        headers[key.strip().upper()] = value.strip()
                
                # 获取设备名称 (通常在 LOCATION 指向的 XML 中，这里先只显示 IP)
                print(headers)
                server = headers.get('SERVER', 'Unknown')
                location = headers.get('LOCATION', '')
                usn = headers.get('USN', '')
                
                # 避免重复
                if usn and usn not in [d['usn'] for d in found_devices]:
                    device_info = {
                        'ip': addr[0],
                        'server': server,
                        'location': location,
                        'usn': usn
                    }
                    found_devices.append(device_info)
                    print(f"[发现设备] IP: {addr[0]} | Server: {server}")
                    print(f"  Location: {location}")
            except socket.timeout:
                break
            except Exception as e:
                print(f"解析错误: {e}")
                
        if not found_devices:
            print("\n未找到任何 DLNA 媒体渲染器。请确保：")
            print("1. 电视/盒子已开机并连接到同一局域网")
            print("2. 设备的 DLNA/投屏功能已开启")
        else:
            print(f"\n共找到 {len(found_devices)} 个设备。")
            
    except Exception as e:
        print(f"扫描出错: {e}")
    finally:
        sock.close()

if __name__ == '__main__':
    scan_dlna_devices()
