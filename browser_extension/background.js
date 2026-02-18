// 存储每个标签页捕获到的视频流
let videoStreams = {};

// 监听 webRequest
chrome.webRequest.onBeforeRequest.addListener(
    function (details) {
        const url = details.url;
        const tabId = details.tabId;

        // 排除扩展自身的请求和无效请求
        if (tabId === -1 || !url) return;

        // 简单的后缀匹配 (即使有参数也能匹配)
        // 许多 HLS 流是 .m3u8?token=xxx
        if (url.includes('.m3u8') || url.includes('.mp4')) {

            console.log("捕获到视频流:", url);

            if (!videoStreams[tabId]) {
                videoStreams[tabId] = [];
            }

            // 避免重复添加完全相同的 URL
            if (!videoStreams[tabId].includes(url)) {
                videoStreams[tabId].push(url);

                // 更新 Badge 提示用户发现视频
                chrome.action.setBadgeText({ text: String(videoStreams[tabId].length), tabId: tabId });
                chrome.action.setBadgeBackgroundColor({ color: "#4CAF50", tabId: tabId });
            }
        }
    },
    { urls: ["<all_urls>"] }
);

// 监听标签页关闭，清理缓存
chrome.tabs.onRemoved.addListener(function (tabId) {
    if (videoStreams[tabId]) {
        delete videoStreams[tabId];
    }
});

// 监听 Popup 发来的消息，获取当前标签页的视频列表
chrome.runtime.onMessage.addListener(
    function (request, sender, sendResponse) {
        if (request.action === "getStreams") {
            const tabId = request.tabId;
            sendResponse({ streams: videoStreams[tabId] || [] });
        }
    }
);
