from yt_dlp import YoutubeDL

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
        with YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(web_url, download=False)
            if 'url' in info: return info['url']
            elif 'formats' in info: return info['formats'][-1]['url']
    except Exception as e:
        print(f"解析错误: {e}")
    return None

# 腾讯视频
# https://v.qq.com/x/cover/mzc0020016apvkq/w0046k2cfd4.html
# 优酷视频
# https://v.youku.com/v_show/id_XNDI0NDYyNjk1Mg==.html?spm=a2hkt.13141534.tabsContent_0.d_cast_1_11&s=efbfbd78efbfbd5cefbf&scm=20140719.rcmd.302610.show_efbfbd78efbfbd5cefbf
# 爱奇艺
# https://www.iqiyi.com/v_1xghiumsit0.html
# 哔哩哔哩
# https://www.bilibili.com/video/BV1kXZ2B1EWr/
# 
print(extract_video_url("https://www.iqiyi.com/v_1xghiumsit0.html"))
