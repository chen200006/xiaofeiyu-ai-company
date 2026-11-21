from PIL import Image
src, dst = r"images\img_20261121_183637_cb8fb5.webp", r"icon.ico"
im = Image.open(src).convert("RGBA")
w, h = im.size
side = min(w, h)
im = im.crop(((w - side) // 2, (h - side) // 2, (w - side) // 2 + side, (h - side) // 2 + side))
px = im.load()
for y in range(im.size[1]):
    for x in range(im.size[0]):
        r, g, b, a = px[x, y]
        if r > 246 and g > 246 and b > 246:
            px[x, y] = (r, g, b, 0)
im.resize((256, 256), Image.LANCZOS).save(dst, sizes=[(16,16),(32,32),(48,48),(64,64),(128,128),(256,256)])
print("icon ->", dst)
