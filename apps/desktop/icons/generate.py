"""Deterministic rasterization of SONORA's existing five-bar vector mark.

Standard library only. Rebuilds Windows ICO and RGBA PNG from the supplied
geometric SVG; no network, fonts, third-party images or model generation.
"""
from pathlib import Path
import struct
import zlib

ROOT = Path(__file__).resolve().parent


def rounded(x, y, left, top, right, bottom, radius):
    if not (left <= x <= right and top <= y <= bottom):
        return False
    nearest_x = min(max(x, left + radius), right - radius)
    nearest_y = min(max(y, top + radius), bottom - radius)
    return (x-nearest_x)**2 + (y-nearest_y)**2 <= radius**2


def chunk(kind, data):
    return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data))


def png(size):
    rows = bytearray()
    for row in range(size):
        rows.append(0)
        for col in range(size):
            color = [0, 0, 0, 0]
            for dy in (0.125, 0.375, 0.625, 0.875):
                for dx in (0.125, 0.375, 0.625, 0.875):
                    x, y = (col+dx)*256/size, (row+dy)*256/size
                    sample = (197, 235, 133, 255) if rounded(x,y,8,8,248,248,56) else (0,0,0,0)
                    for center, top, bottom in [(56,112,144),(92,80,176),(128,56,200),(164,88,168),(200,112,144)]:
                        if rounded(x,y,center-7,top-7,center+7,bottom+7,7):
                            sample = (37,51,28,255)
                    for k in range(4): color[k] += sample[k]
            rows.extend(round(c/16) for c in color)
    return b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR',struct.pack('>IIBBBBB',size,size,8,6,0,0,0)) + chunk(b'IDAT',zlib.compress(rows,9)) + chunk(b'IEND',b'')


sizes = [16, 32, 48, 64, 128, 256]
images = [png(size) for size in sizes]
offset = 6 + 16*len(images)
directory = bytearray(struct.pack('<HHH',0,1,len(images)))
for size, image in zip(sizes,images):
    directory.extend(struct.pack('<BBBBHHII',size%256,size%256,0,0,1,32,len(image),offset))
    offset += len(image)
(ROOT/'icon.ico').write_bytes(directory + b''.join(images))
(ROOT/'icon.png').write_bytes(images[-1])
