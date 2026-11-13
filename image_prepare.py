from PIL import Image, ImageOps


def prepare_instagram_image(source, target):
    """EXIF向きを反映し、Instagram対応比率のJPEGを作る。"""
    with Image.open(source) as opened:
        image = ImageOps.exif_transpose(opened).convert("RGB")
        width, height = image.size
        ratio = width / height

        # 対応範囲 4:5〜1.91:1。中央トリミングし、管理画面にも完成形を表示する。
        if ratio < 0.8:
            crop_height = round(width / 0.8)
            top = max(0, (height - crop_height) // 2)
            image = image.crop((0, top, width, top + crop_height))
        elif ratio > 1.91:
            crop_width = round(height * 1.91)
            left = max(0, (width - crop_width) // 2)
            image = image.crop((left, 0, left + crop_width, height))

        if image.width > 1440:
            new_height = round(image.height * (1440 / image.width))
            image = image.resize((1440, new_height), Image.Resampling.LANCZOS)
        elif image.width < 320:
            new_height = round(image.height * (320 / image.width))
            image = image.resize((320, new_height), Image.Resampling.LANCZOS)

        image.save(target, "JPEG", quality=94, optimize=True, progressive=True)
