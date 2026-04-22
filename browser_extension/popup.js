document.addEventListener('DOMContentLoaded', function () {
    const streamList = document.getElementById('stream-list');
    const emptyMsg = document.getElementById('empty-msg');
    const castPageBtn = document.getElementById('castPageBtn');
    const statusEl = document.getElementById('status');

    let currentTabUrl = "";

    // 1. 获取当前标签页信息
    chrome.tabs.query({ active: true, currentWindow: true }, function (tabs) {
        if (tabs.length === 0) return;
        const tabId = tabs[0].id;
        currentTabUrl = tabs[0].url;

        // 2. 向 background 请求该标签页的流列表
        chrome.runtime.sendMessage({ action: "getStreams", tabId: tabId }, function (response) {
            renderStreams(response ? response.streams : []);
        });
    });

    // 渲染流列表
    function renderStreams(streams) {
        streamList.innerHTML = "";
        if (!streams || streams.length === 0) {
            emptyMsg.style.display = 'block';
            return;
        }

        emptyMsg.style.display = 'none';
        streams.forEach(url => {
            const li = document.createElement('li');
            li.className = 'stream-item';

            const typeLabel = url.includes('.m3u8') ? 'HLS' : 'MP4';
            // 截取 URL 最后一段文件名，更易读
            let filename = url.split('?')[0].split('/').pop();
            if (filename.length > 30) filename = filename.substring(0, 30) + "...";
            if (!filename) filename = "Unknown Video";

            li.innerHTML = `
        <div style="flex:1; overflow:hidden; display:flex; align-items:center;">
            <span class="stream-type" style="background:${typeLabel === 'HLS' ? '#FF9800' : '#2196F3'}">${typeLabel}</span>
            <span class="stream-url" title="${url}">${filename}</span>
        </div>
        <button class="cast-btn">📺 投屏</button>
      `;

            // 点击投屏按钮
            li.querySelector('button').onclick = (e) => {
                e.stopPropagation(); // 防止冒泡
                castVideo(url);
            };
            // 点击整行也能投屏 (为了方便)
            li.onclick = () => castVideo(url);
            streamList.appendChild(li);
        });
    }

    // 投屏逻辑
    function castVideo(url) {
        statusEl.textContent = "⏳ 正在发送投屏请求...";
        statusEl.style.color = "#666";

        // 禁用所有交互
        const allBtns = document.querySelectorAll('button');
        allBtns.forEach(b => b.disabled = true);
        document.body.style.opacity = "0.7";

        fetch('http://127.0.0.1:5000/api/cast', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ url: url })
        })
            .then(response => response.json())
            .then(data => {
                statusEl.textContent = "✅ 已发送投屏指令！";
                statusEl.style.color = "green";
            })
            .catch(error => {
                console.error(error);
                statusEl.textContent = "❌ 连接失败: Python 服务未启动";
                statusEl.style.color = "red";
            })
            .finally(() => {
                // 恢复按钮状态
                setTimeout(() => {
                    allBtns.forEach(b => b.disabled = false);
                    document.body.style.opacity = "1";
                    // 如果成功了，稍微等下自动关闭
                    if (statusEl.textContent.includes("已发送")) {
                        setTimeout(() => window.close(), 1500);
                    }
                }, 800);
            });
    }

    // 绑定“网页解析”按钮
    castPageBtn.addEventListener('click', function () {
        if (currentTabUrl) {
            castVideo(currentTabUrl);
        } else {
            statusEl.textContent = "无效的页面 URL";
        }
    });
});
