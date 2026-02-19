import requests

# 目标 URL (您提供的那个 .ts 分片)
url = "http://valipl.cp31.ott.cibntv.net/65729268B734171E087D95250/03000700006972EC785E9477D9F5AB8C696BAE-078A-4B78-A21F-E38D186AB81F-00007.ts?ccode=0564&duration=2831&expire=18000&psid=c042b1f89585e6056336712580710f9041346&ups_client_netip=75b7bcf7&ups_ts=1771416766&ups_userid=&apscid=&mnid=&rid=200000005C8D1199B352088FB618D73F75EA927102000000&operate_type=1&umt=1&type=mp4hd2v3&utid=vpocInh1j1ICAXW3vPfm7o2N&vid=XNDI0NDQ0ODEwNA%3D%3D&s=efbfbd78efbfbd5cefbf&t=6f72c0c75aa4908&cug=2&bc=2&si=774&eo=1&ykfs=885856&ckt=3&m_onoff=0&vkey=B15a1d7733d0cb2816a01aae70bd5264c&fms=7ea6b9b57cf05bc8&tr=2831&le=dba64e9bee3a47a58a2ae59259b2b38b"

def test_request(name, headers):
    print(f"--- 测试: {name} ---")
    try:
        # 使用 stream=True 只读取 Header，不下载大文件
        resp = requests.get(url, headers=headers, stream=True, timeout=5)
        print(f"状态码: {resp.status_code}")
        if resp.status_code == 200:
            print("✅ 成功")
            # 尝试读取一点数据确保连接正常
            chunk = next(resp.iter_content(1024))
            print(f"读取数据成功: {len(chunk)} bytes")
        else:
            print(f"❌ 失败: {resp.text[:100]}")
    except Exception as e:
        print(f"❌ 异常: {e}")
    print()

# 1. 模拟当前代理逻辑 (带 Referer)
headers_proxy = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
    'Referer': 'https://www.youku.com/',
    'Accept': '*/*'
}
test_request("带 Youku Referer", headers_proxy)

# 2. 模拟浏览器直接访问 (通常不带 Referer，或者 Referer 是 m3u8 地址，甚至 None)
headers_browser = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
    'Accept': '*/*'
}
test_request("不带 Referer (纯 UA)", headers_browser)

# 3. 极简模式
headers_simple = {}
test_request("无 Headers (裸奔)", headers_simple)

# 4. 模拟您刚才修改的带 Host 只有
headers_custom = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
    'Accept': '*/*',
    'connection':'keep-alive',
    'host':'valipl.cp31.ott.cibntv.net',
    'range':'bytes=0-'
}
test_request("自定义 Headers (带 Host, Range)", headers_custom)
