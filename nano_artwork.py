"""Append Nano 6G artwork while preserving every existing record and pixel.

Record layout: gtkpod/libgpod src/db-itunes-parser.h, db-artwork-writer.c,
ithumb-writer.c. This deliberately supports the six RGB565-LE formats observed
on this Nano; it never rewrites iTunesCDB or the music database with libgpod.
"""
from pathlib import Path
import os
import sqlite3
import struct
import tempfile

# width, height, center crop (otherwise preserve the complete thumbnail)
FORMATS = {1073: (240, 240, False), 1074: (50, 50, False),
           1084: (240, 240, False), 1085: (88, 88, True),
           1086: (240, 240, True), 1089: (58, 58, False)}


def u32(data, offset):
    return struct.unpack_from('<I', data, offset)[0]


def record(data, offset, magic, end):
    if offset + 12 > end or data[offset:offset + 4] != magic:
        raise ValueError(f'Invalid artwork record {magic!r} at {offset}')
    header, length = struct.unpack_from('<II', data, offset + 4)
    if not 12 <= header <= length or offset + length > end:
        raise ValueError('Artwork record extends outside its container')
    return header, length


def image_records(data):
    header, length = record(data, 0, b'mhfd', len(data))
    if length != len(data): raise ValueError('ArtworkDB length mismatch')
    sections = []
    offset = header
    for _ in range(u32(data, 20)):
        size, length = record(data, offset, b'mhsd', len(data))
        if u32(data, offset + 12) == 1: sections.append((offset, size, length))
        offset += length
    if offset != len(data) or len(sections) != 1:
        raise ValueError('Unsupported ArtworkDB sections')
    section, header, length = sections[0]
    listing = section + header
    if data[listing:listing + 4] != b'mhli': raise ValueError('Missing artwork image list')
    offset = listing + u32(data, listing + 4)
    result = []
    for _ in range(u32(data, listing + 8)):
        _, size = record(data, offset, b'mhii', section + length)
        result.append((offset, size))
        offset += size
    if offset != section + length: raise ValueError('Artwork image count/length mismatch')
    return section, listing, result


def thumbnails(image):
    offset = u32(image, 4)
    result = []
    for _ in range(u32(image, 12)):
        header, length = record(image, offset, b'mhod', len(image))
        if struct.unpack_from('<H', image, offset + 12)[0] != 2:
            raise ValueError('Unsupported artwork child type')
        node = offset + header
        node_header, _ = record(image, node, b'mhni', offset + length)
        string = node + node_header
        record(image, string, b'mhod', offset + length)
        if struct.unpack_from('<H', image, string + 12)[0] != 3 or image[string + 28] != 2:
            raise ValueError('Unsupported artwork filename encoding')
        name = image[string + 36:string + 36 + u32(image, string + 24)].decode('utf-16le')
        if not name.startswith(':F') or '/' in name or '\\' in name or ':' in name[1:]:
            raise ValueError('Invalid thumbnail filename')
        fmt = u32(image, node + 16)
        if fmt not in FORMATS: raise ValueError(f'Unsupported Nano artwork format {fmt}')
        result.append((node, fmt, name[1:]))
        offset += length
    if offset != len(image): raise ValueError('Artwork thumbnail count mismatch')
    return result


def render(image, fmt):
    from PIL import Image, ImageOps
    width, height, crop = FORMATS[fmt]
    rgb = image.convert('RGB')
    scaled = (ImageOps.fit(rgb, (width, height), method=Image.Resampling.LANCZOS) if crop
              else ImageOps.contain(rgb, (width, height), method=Image.Resampling.LANCZOS))
    x, y = (width - scaled.width) // 2, (height - scaled.height) // 2
    canvas = Image.new('RGB', (width, height)); canvas.paste(scaled, (x, y))
    pixels = bytearray()
    raw = canvas.tobytes()
    for r, g, b in zip(raw[0::3], raw[1::3], raw[2::3]):
        pixels.extend(struct.pack('<H', ((r >> 3) << 11) | ((g >> 2) << 5) | (b >> 3)))
    # libgpod's stored dimensions include the leading padding.
    return bytes(pixels), (y, x, y + scaled.height, x + scaled.width)


def atomic_write(path, data):
    fd, temporary = tempfile.mkstemp(prefix='.artwork-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data); stream.flush(); os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary): os.remove(temporary)


def empty_image_template():
    """Rebuild one known-format image record after the last old image is deleted."""
    def header(tag,size,length):
        data=bytearray(size);data[:4]=tag;struct.pack_into('<II',data,4,size,length);return data
    children=bytearray()
    for fmt,(width,height,_) in FORMATS.items():
        text=f':F{fmt}_1.ithmb'.encode('utf-16le')
        filename=header(b'mhod',36,36+len(text))
        struct.pack_into('<I',filename,4,24);struct.pack_into('<H',filename,12,3)
        struct.pack_into('<I',filename,24,len(text));filename[28]=2;filename.extend(text)
        node=header(b'mhni',76,76+len(filename))
        struct.pack_into('<IIII4H',node,12,1,fmt,0,width*height*2,0,0,height,width);node.extend(filename)
        outer=header(b'mhod',24,24+len(node));struct.pack_into('<H',outer,12,2);children.extend(outer+node)
    image=header(b'mhii',152,152+len(children));struct.pack_into('<I',image,12,len(FORMATS))
    image.extend(children);return image


def install_cover(mount, item_pid, cover):
    """Publish native artwork first, then its SQLite pointer. Safe to retry."""
    from PIL import Image
    mount = Path(mount)
    artdir = mount / 'iPod_Control/Artwork'
    path = artdir / 'ArtworkDB'
    original = path.read_bytes()
    section, listing, entries = image_records(original)
    existing = next((offset for offset, _ in entries
                     if struct.unpack_from('<Q', original, offset + 20)[0] == item_pid), None)
    if existing is not None:
        artwork_id = u32(original, existing + 16)
    else:
        if entries:
            start,length=entries[0];image=bytearray(original[start:start+length])
        else:image=empty_image_template()
        nodes = thumbnails(image)
        artwork_id = max(u32(original,28),max((u32(original,p+16) for p,_ in entries),default=99)+1)
        if artwork_id>=2**32:raise ValueError('Artwork ID limit reached')
        struct.pack_into('<I', image, 16, artwork_id)
        struct.pack_into('<Q', image, 20, item_pid)
        struct.pack_into('<I', image, 48, Path(cover).stat().st_size)
        with Image.open(cover) as bitmap:
            bitmap.load()
            rendered = [(node, fmt, name, *render(bitmap, fmt)) for node, fmt, name in nodes]
        # Append only: existing artwork pixels and all original mhii records survive.
        appended = []
        published = False
        try:
            for node, fmt, name, pixels, dimensions in rendered:
                target = artdir / name
                size = target.stat().st_size
                expected = FORMATS[fmt][0] * FORMATS[fmt][1] * 2
                if u32(image, node + 24) != expected or size % expected:
                    raise ValueError(f'Unexpected thumbnail slot size for format {fmt}')
                if size + len(pixels) >= 2**32: raise ValueError('Thumbnail offset limit reached')
                appended.append((target, size))
                with target.open('ab') as stream:
                    stream.write(pixels); stream.flush(); os.fsync(stream.fileno())
                struct.pack_into('<II4H', image, node + 20, size, len(pixels), *dimensions)
            end = section + u32(original, section + 8)
            updated = bytearray(original[:end] + image + original[end:])
            struct.pack_into('<I', updated, 8, len(updated))
            struct.pack_into('<I', updated, 28, max(u32(original, 28), artwork_id + 1))
            struct.pack_into('<I', updated, section + 8, u32(original, section + 8) + len(image))
            struct.pack_into('<I', updated, listing + 8, len(entries) + 1)
            image_records(updated)
            atomic_write(path, updated)
            published = True
        finally:
            if not published:
                for target, size in reversed(appended):
                    with target.open('r+b') as stream:
                        stream.truncate(size)
    database = mount / 'iPod_Control/iTunes/iTunes Library.itlp/Library.itdb'
    con = sqlite3.connect(database)
    try:
        with con:
            if not con.execute('select 1 from item where pid=?', (item_pid,)).fetchone():
                raise ValueError('Artwork track is absent from the library')
            con.execute('update item set artwork_status=1,artwork_cache_id=? where pid=?', (artwork_id, item_pid))
            con.execute('''update album set artwork_status=1,artwork_item_pid=? where pid in
                (select album_pid from item where pid=? and album_pid<>0)''', (item_pid, item_pid))
    finally:
        con.close()
    return artwork_id


def import_embedded_cover(mount, item_pid, audio_path):
    """Use the embedded APIC/covr image, including URL-download thumbnails."""
    from yt_download import probe, run_command
    info = probe(audio_path)
    cover = next((s for s in info.get('streams', [])
                  if s.get('codec_type') == 'video' and s.get('disposition', {}).get('attached_pic')), None)
    if cover is None:
        return None
    with tempfile.TemporaryDirectory(prefix='nano-cover-') as directory:
        output = Path(directory) / 'cover.png'
        run_command(['ffmpeg', '-nostdin', '-v', 'error', '-y', '-i', str(audio_path),
                     '-map', f"0:{cover['index']}", '-frames:v', '1', str(output)], timeout=60)
        return install_cover(mount, item_pid, output)


def without_track(data, item_pid):
    """Drop a track's image records, retaining pixel slots and other artwork IDs."""
    section, listing, entries = image_records(data)
    removed = [(p,n) for p,n in entries if struct.unpack_from('<Q',data,p+20)[0] == (item_pid & (2**64-1))]
    if not removed: return data
    start = listing + u32(data, listing + 4)
    end = section + u32(data, section + 8)
    body = b''.join(data[p:p+n] for p,n in entries if (p,n) not in removed)
    updated = bytearray(data[:start] + body + data[end:])
    size = sum(n for _,n in removed)
    struct.pack_into('<I',updated,8,len(updated))
    struct.pack_into('<I',updated,section+8,u32(data,section+8)-size)
    struct.pack_into('<I',updated,listing+8,len(entries)-len(removed))
    image_records(updated)
    return bytes(updated)
