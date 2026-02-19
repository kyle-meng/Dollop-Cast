import requests
import re
import json

def get_bilibili_stream(url):
    """
    专门解析 Bilibili 视频流 (优先尝试获取 MP4 直链)
    """
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
        'Referer': 'https://www.bilibili.com/'
    }

    try:
        # 1. 提取 BV 号
        bv_match = re.search(r'(BV\w+)', url)
        if not bv_match:
            print("未找到 BV 号")
            return None
        bvid = bv_match.group(1)

        # 2. 获取 CID (视频 ID)
        # API: https://api.bilibili.com/x/web-interface/view?bvid=...
        info_api = f"https://api.bilibili.com/x/web-interface/view?bvid={bvid}"
        resp = requests.get(info_api, headers=headers)
        data = resp.json()
        
        if data['code'] != 0:
            print(f"获取视频信息失败: {data['message']}")
            return None
            
        cid = data['data']['cid']
        title = data['data']['title']
        print(f"正在解析: {title} (CID: {cid})")

        # 3. 获取播放地址 (playurl)
        # fnval=1 表示优先获取 mp4/flv 格式 (非 DASH)
        # platform=html5 也是关键
        play_api = f"https://api.bilibili.com/x/player/playurl?bvid={bvid}&cid={cid}&qn=64&fnval=1&fnver=0&fourk=1&platform=html5"
        
        # 注意：如果不带 Cookie，通常只能拿 360p/480p 的试看或低清流
        # 如果您有 B站 Cookie，可以在这里加上 headers['Cookie'] = 'SESSDATA=...'
        
        play_resp = requests.get(play_api, headers=headers)
        play_data = play_resp.json()

        if play_data['code'] != 0:
            print("获取播放地址失败")
            return None

        d = play_data['data']
        
        # 优先找 durl (flv/mp4 直链)
        if 'durl' in d and len(d['durl']) > 0:
            real_url = d['durl'][0]['url']
            print("✅ 成功获取 MP4/FLV 直链")
            return real_url
        
        # 如果只有 dash，且我们没法处理，那就没办法了
        # (此时通常是因为 B站 强制推 DASH，可以尝试伪装 User-Agent 为 Android)
        print("❌ 未获取到 MP4 直链，B站返回了 DASH 格式 (需要进一步合成，不适合DLNA直投)")
        return None

    except Exception as e:
        print(f"B站解析出错: {e}")
        return None

if __name__ == "__main__":
    test_url = "https://www.bilibili.com/video/BV1kXZ2B1EWr/"
    url = get_bilibili_stream(test_url)
    if url:
        print(f"最终地址: {url}")
    else:
        print("解析失败")
