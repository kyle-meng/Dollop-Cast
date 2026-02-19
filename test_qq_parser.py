
import requests
import re
import json

def get_qq_video_info(url):
    print(f"Testing URL: {url}")
    
    # 模拟 iPhone
    headers = {
        'User-Agent': 'Mozilla/5.0 (iPhone; CPU iPhone OS 13_2_3 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/13.0.3 Mobile/15E148 Safari/604.1',
        'Referer': url
    }
    
    try:
        resp = requests.get(url, headers=headers)
        if resp.status_code == 200:
            # 尝试在页面源码中找 m3u8 或 mp4
            # 腾讯 H5 页面通常会有 VIDEO_INFO 或者类似的 JSON
            content = resp.text
            
            # 1. 简单粗暴找 .m3u8
            m3u8_matches = re.findall(r'http[s]?://[^"]+\.m3u8[^"]*', content)
            if m3u8_matches:
                print("Found m3u8 directly in HTML:")
                for m in m3u8_matches:
                    # 过滤掉转义符
                    clean_url = m.replace('\\/', '/')
                    print(f"  - {clean_url}")
                return

            # 2. 找 window.__PLAYER_INFO__ 或者类似变量
            # 腾讯现在的 H5 播放器隐藏得比较深，通常是用 JS 动态加载的
            # 但有时会有 get_video_info 接口的线索
            
            print("No direct m3u8 found in HTML. Trying yt-dlp with cookie/user-agent...")
            
    except Exception as e:
        print(f"Error: {e}")

if __name__ == '__main__':
    # 找一个腾讯视频的热门链接测试
    test_url = 'https://v.qq.com/x/cover/mzc002008li5q0z/t00473lt7r3.html' 
    get_qq_video_info(test_url)
