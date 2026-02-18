# Dollop Cast 📺

**Dollop Cast** 是一个轻量级、跨平台的局域网 DLNA 投屏解决方案。它允许您将浏览器中的视频（如 .m3u8, .mp4）一键投射到电视、电视盒子或投影仪上播放。

##✨ 主要特性

*   **浏览器插件嗅探**：自动检测网页中的 HLS/MP4 视频流，点击即可投屏。
*   **智能解析**：对于普通网页，内置 `yt-dlp` 支持在后台自动解析真实视频地址。
*   **托盘管理**：Windows 系统托盘应用，自动扫描局域网设备，一键切换投屏目标。
*   **支持广泛**：支持绝大多数支持 DLNA/UPnP 协议的智能电视、盒子（小米盒子、华为智慧屏、索尼电视等）。

## 🛠️ 安装指南

### 1. 后端依赖安装

确保已安装 [Python 3.8+](https://www.python.org/)，然后在项目根目录运行：

```bash
pip install -r requirements.txt
```
*(如果没有 requirements.txt，可运行: `pip install flask flask-cors yt-dlp requests pystray Pillow`)*

### 2. 浏览器插件安装

该插件支持 Chrome, Edge, Brave 等 Chromium 内核浏览器。

1.  打开浏览器扩展管理页面 (`chrome://extensions` 或 `edge://extensions`)。
2.  开启右上角 **"开发者模式"**。
3.  点击 **"加载已解压的扩展程序"**。
4.  选择本项目下的 `browser_extension` 文件夹。

## 🚀 使用方法

### 第一步：启动服务
双击运行或在命令行启动托盘应用：

```bash
python tray_app.py
```

*   成功启动后，任务栏右下角会出现蓝色电视图标。
*   **右键图标** -> 选择您的电视设备（打钩✅即为选中）。

### 第二步：投屏
1.  在浏览器打开任意视频网站（如 Bilibili, 优酷, 或在线影视站）。
2.  点击浏览器右上角的 **Dollop Cast 图标**。
3.  **推荐方式**：如果图标显示数字，说明嗅探到了直链，点击列表中的绿色 **[投屏]** 按钮即可（速度最快）。
4.  **备用方式**：如果列表为空，点击底部的 **"解析并投屏当前网页"**，后台将尝试自动解析。

## 📂 文件结构说明

*   `tray_app.py`: **[主程序]** 系统托盘应用，整合了设备扫描和 Web 服务。
*   `screen_cast.py`: (实验性) 屏幕镜像投屏工具，基于 FFmpeg。
*   `browser_extension/`: 浏览器插件源码。

## 📝 许可证

本项目采用 MIT License 开源。
