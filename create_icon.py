from PIL import Image, ImageDraw

def create_icon():
    # 创建一个 128x128 的透明背景图像
    img = Image.new('RGBA', (128, 128), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    
    # 画一个绿色的圆
    d.ellipse((10, 10, 118, 118), fill=(76, 175, 80), outline=(76, 175, 80))
    
    # 画一个简单的 TV 屏幕轮廓 (白色)
    d.rectangle((34, 44, 94, 84), fill=(255, 255, 255))
    
    # 保存为 icon.png
    img.save('browser_extension/icon.png')
    print("icon.png created.")

if __name__ == "__main__":
    create_icon()
