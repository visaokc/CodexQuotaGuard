"""One supersampled meter mark shared by the executable, taskbar and tray."""
from PIL import Image, ImageDraw


def icon_image(size=256):
    scale = 4
    image = Image.new('RGBA', (256*scale, 256*scale))
    draw = ImageDraw.Draw(image)
    box = lambda values: tuple(round(v*scale) for v in values)
    draw.rounded_rectangle(box((5, 5, 251, 251)), radius=57*scale, fill='#111e33',
                           outline='#304b74', width=2*scale)
    draw.rounded_rectangle(box((12, 12, 244, 244)), radius=51*scale, fill='#162741')
    draw.arc(box((49, 49, 207, 207)), 42, 318, fill='#71b5ff', width=27*scale)
    draw.arc(box((49, 49, 207, 207)), 80, 220, fill='#558aff', width=27*scale)
    for x, height in ((113, 23), (140, 39), (167, 56)):
        draw.rounded_rectangle(box((x, 155-height, x+15, 155)), radius=5*scale, fill='#dcecff')
    draw.ellipse(box((205, 58, 229, 82)), fill='#f6b763')
    return image.resize((size, size), Image.Resampling.LANCZOS)
