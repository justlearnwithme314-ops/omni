"""Image normalisation like Pi's image-resize-core.ts: images larger than 2000x2000 or ~4.5 MB of base64 are resized
(PNG and JPEG candidates, lower JPEG qualities, then 75% steps) and a dimension note tells the model how to map
coordinates back. Uses Pillow when installed; without it a pure-Python path resizes 8-bit PNGs and passes other images
through when they are already within the limits.
"""
from __future__ import annotations

import base64
import io
import struct
import zlib
from typing import Any, Dict, List, Optional, Tuple

MAX_W = MAX_H = 2000
MAX_BYTES = int(4.5 * 1024 * 1024)
JPEG_QUALITY = 80


def b64len(n: int) -> int:
    return -(-n // 3) * 4


def dimension_note(ow: int, oh: int, w: int, h: int) -> str:
    return (f"[Image: original {ow}x{oh}, displayed at {w}x{h}. Multiply coordinates by {ow / w:.2f} "
            f"to map to original image.]")


def image_size(data: bytes) -> Optional[Tuple[int, int]]:
    """Width/height from the header of PNG, GIF, JPEG, WebP or BMP data."""
    try:
        if data[:8] == b"\x89PNG\r\n\x1a\n":
            return struct.unpack(">II", data[16:24])
        if data[:6] in (b"GIF87a", b"GIF89a"):
            return struct.unpack("<HH", data[6:10])
        if data[:2] == b"BM":
            w, h = struct.unpack("<ii", data[18:26])
            return w, abs(h)
        if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            kind = data[12:16]
            if kind == b"VP8X":
                return 1 + int.from_bytes(data[24:27], "little"), 1 + int.from_bytes(data[27:30], "little")
            if kind == b"VP8L":
                b = data[21:25]
                return 1 + (((b[1] & 0x3F) << 8) | b[0]), 1 + (((b[3] & 0xF) << 10) | (b[2] << 2) | ((b[1] & 0xC0) >> 6))
            if kind == b"VP8 ":
                w, h = struct.unpack("<HH", data[26:30])
                return w & 0x3FFF, h & 0x3FFF
        if data[:2] == b"\xff\xd8":
            i = 2
            while i < len(data):
                while data[i] == 0xFF:
                    i += 1
                marker = data[i]
                i += 1
                if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
                    continue
                ln = struct.unpack(">H", data[i:i + 2])[0]
                if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                    h, w = struct.unpack(">HH", data[i + 3:i + 7])
                    return w, h
                i += ln
    except Exception:
        return None
    return None


def process_image(data: bytes, mime: str, auto_resize: bool = True, max_w: int = MAX_W, max_h: int = MAX_H,
                  max_bytes: int = MAX_BYTES) -> Dict[str, Any]:
    """{ok, data(b64), mimeType, hints[]} or {ok: False, message}."""
    size = image_size(data)
    within = b64len(len(data)) < max_bytes and (size is None or (size[0] <= max_w and size[1] <= max_h))
    if within or not auto_resize:
        if not within and not auto_resize and b64len(len(data)) >= max_bytes:
            return {"ok": False, "message": f"[Image omitted: {len(data) // 1024} KB is over the inline image limit and "
                                            "images.autoResize is off.]"}
        return {"ok": True, "data": base64.b64encode(data).decode("ascii"), "mimeType": mime, "hints": []}
    r = _resize_pillow(data, mime, max_w, max_h, max_bytes)
    if r is None and mime == "image/png":
        r = _resize_png_pure(data, max_w, max_h, max_bytes)
    if r is None:
        return {"ok": False, "message": "[Image omitted: could not be resized below the inline image size limit. "
                                        "Install Pillow (pip install pillow) for full image resizing.]"}
    b64, out_mime, (ow, oh), (w, h) = r
    return {"ok": True, "data": b64, "mimeType": out_mime, "hints": [dimension_note(ow, oh, w, h)] if (w, h) != (ow, oh) else []}


def _fit(ow: int, oh: int, mw: int, mh: int) -> Tuple[int, int]:
    w, h = ow, oh
    if w > mw:
        h, w = round(h * mw / w), mw
    if h > mh:
        w, h = round(w * mh / h), mh
    return max(1, w), max(1, h)


def _resize_pillow(data: bytes, mime: str, mw: int, mh: int, max_bytes: int):
    try:
        from PIL import Image, ImageOps  # type: ignore
    except Exception:
        return None
    try:
        im = Image.open(io.BytesIO(data))
        im = ImageOps.exif_transpose(im)
        ow, oh = im.size
        w, h = _fit(ow, oh, mw, mh)
        qualities = list(dict.fromkeys([JPEG_QUALITY, 85, 70, 55, 40]))
        while True:
            rs = im.resize((w, h), Image.LANCZOS)
            cands: List[Tuple[str, bytes]] = []
            buf = io.BytesIO()
            rs.save(buf, "PNG", optimize=True)
            cands.append(("image/png", buf.getvalue()))
            rgb = rs.convert("RGB")
            for q in qualities:
                buf = io.BytesIO()
                rgb.save(buf, "JPEG", quality=q)
                cands.append(("image/jpeg", buf.getvalue()))
            png, jpeg, rest = cands[0], cands[1], cands[2:]
            order = ([jpeg, png] if len(jpeg[1]) < len(png[1]) else [png, jpeg]) + rest
            for m, b in order:
                if b64len(len(b)) < max_bytes:
                    return base64.b64encode(b).decode("ascii"), m, (ow, oh), (w, h)
            if (w, h) == (1, 1):
                return None
            nw, nh = max(1, int(w * 0.75)), max(1, int(h * 0.75))
            if (nw, nh) == (w, h):
                return None
            w, h = nw, nh
    except Exception:
        return None


# ------------------------------------------------------------------------------- pure-Python PNG fallback
def _paeth(a: int, b: int, c: int) -> int:
    p = a + b - c
    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
    return a if pa <= pb and pa <= pc else (b if pb <= pc else c)


def _decode_png(data: bytes):
    pos, idat, ihdr, plte, trns = 8, [], None, None, None
    while pos < len(data):
        ln, typ = struct.unpack(">I4s", data[pos:pos + 8])
        body = data[pos + 8:pos + 8 + ln]
        pos += 12 + ln
        if typ == b"IHDR":
            ihdr = struct.unpack(">IIBBBBB", body)
        elif typ == b"IDAT":
            idat.append(body)
        elif typ == b"PLTE":
            plte = body
        elif typ == b"tRNS":
            trns = body
        elif typ == b"IEND":
            break
    if not ihdr:
        return None
    w, h, depth, ctype, _c, _f, interlace = ihdr
    if depth != 8 or interlace != 0 or ctype not in (0, 2, 3, 4, 6):
        return None
    ch = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}[ctype]
    raw = zlib.decompress(b"".join(idat))
    stride = w * ch
    rows: List[bytearray] = []
    prev = bytearray(stride)
    i = 0
    for _ in range(h):
        ft = raw[i]
        line = bytearray(raw[i + 1:i + 1 + stride])
        i += 1 + stride
        for x in range(stride):
            a = line[x - ch] if x >= ch else 0
            b = prev[x]
            c = prev[x - ch] if x >= ch else 0
            if ft == 1:
                line[x] = (line[x] + a) & 255
            elif ft == 2:
                line[x] = (line[x] + b) & 255
            elif ft == 3:
                line[x] = (line[x] + ((a + b) >> 1)) & 255
            elif ft == 4:
                line[x] = (line[x] + _paeth(a, b, c)) & 255
        rows.append(line)
        prev = line
    # to RGBA
    out = []
    for line in rows:
        px = bytearray()
        for x in range(w):
            if ctype == 6:
                px += line[x * 4:x * 4 + 4]
            elif ctype == 2:
                px += line[x * 3:x * 3 + 3] + b"\xff"
            elif ctype == 0:
                v = line[x]
                px += bytes((v, v, v, 255))
            elif ctype == 4:
                v = line[x * 2]
                px += bytes((v, v, v, line[x * 2 + 1]))
            else:
                k = line[x]
                rgb = plte[k * 3:k * 3 + 3] if plte else b"\0\0\0"
                alpha = trns[k] if trns and k < len(trns) else 255
                px += rgb + bytes((alpha,))
        out.append(px)
    return w, h, out


def _encode_png(w: int, h: int, rows: List[bytearray]) -> bytes:
    raw = b"".join(b"\x00" + bytes(r) for r in rows)

    def chunk(t: bytes, b: bytes) -> bytes:
        return struct.pack(">I", len(b)) + t + b + struct.pack(">I", zlib.crc32(t + b) & 0xFFFFFFFF)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0)) +
            chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


def _resize_png_pure(data: bytes, mw: int, mh: int, max_bytes: int):
    try:
        dec = _decode_png(data)
    except Exception:
        return None
    if not dec:
        return None
    ow, oh, rows = dec
    w, h = _fit(ow, oh, mw, mh)
    while True:
        out = []
        for y in range(h):  # box-sampled nearest neighbour; good enough for screenshots
            src = rows[min(oh - 1, int(y * oh / h))]
            line = bytearray()
            for x in range(w):
                sx = min(ow - 1, int(x * ow / w)) * 4
                line += src[sx:sx + 4]
            out.append(line)
        png = _encode_png(w, h, out)
        if b64len(len(png)) < max_bytes:
            return base64.b64encode(png).decode("ascii"), "image/png", (ow, oh), (w, h)
        if (w, h) == (1, 1):
            return None
        w, h = max(1, int(w * 0.75)), max(1, int(h * 0.75))


def normalize_tool_result_images(content: List[Dict[str, Any]], auto_resize: bool = True) -> List[Dict[str, Any]]:
    """Images produced by extensions / MCP tools are normalised once as they enter history (failures keep the original)."""
    if not any(b.get("type") == "image" for b in content or []):
        return content
    out: List[Dict[str, Any]] = []
    for b in content:
        if b.get("type") != "image":
            out.append(b)
            continue
        try:
            raw = base64.b64decode(b.get("data") or "")
        except Exception:
            out.append(b)
            continue
        r = process_image(raw, b.get("mimeType") or "image/png", auto_resize)
        if not r["ok"] or (r["data"] == b.get("data") and not r["hints"]):
            out.append(b)
            continue
        out.append({"type": "image", "data": r["data"], "mimeType": r["mimeType"]})
        if r["hints"]:
            out.append({"type": "text", "text": "\n".join(r["hints"])})
    return out
