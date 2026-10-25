"""Native artwork publication, preservation, and failure recovery."""
from contextlib import closing
from pathlib import Path
import sqlite3
import struct
import shutil
import tempfile
import unittest
from unittest.mock import patch

import nano_artwork as art
try:
    from PIL import Image
except ImportError:
    Image = None


def header(tag, size, length=None):
    data = bytearray(size)
    data[:4] = tag
    struct.pack_into('<II', data, 4, size, length or size)
    return data


def fixture(mount):
    directory = mount / 'iPod_Control/Artwork'; directory.mkdir(parents=True)
    children = bytearray()
    for fmt, (width, height, _) in art.FORMATS.items():
        name = f':F{fmt}_1.ithmb'.encode('utf-16le')
        filename = header(b'mhod', 36, 36 + len(name))
        struct.pack_into('<I', filename, 4, 24)
        struct.pack_into('<H', filename, 12, 3)
        struct.pack_into('<I', filename, 24, len(name)); filename[28] = 2
        filename.extend(name)
        node = header(b'mhni', 76, 76 + len(filename))
        struct.pack_into('<I', node, 12, 1)
        struct.pack_into('<III4H', node, 16, fmt, 0, width*height*2, 0, 0, height, width)
        node.extend(filename)
        outer = header(b'mhod', 24, 24 + len(node)); struct.pack_into('<H', outer, 12, 2)
        children.extend(outer + node)
        (directory / f'F{fmt}_1.ithmb').write_bytes(b'\x12\x34' * (width*height))
    image = header(b'mhii', 152, 152 + len(children))
    struct.pack_into('<IIQ', image, 12, 6, 100, 1); image.extend(children)
    listing = header(b'mhli', 92); struct.pack_into('<I', listing, 8, 1)
    section = header(b'mhsd', 96, 96 + len(listing) + len(image)); struct.pack_into('<I', section, 12, 1)
    root = header(b'mhfd', 132, 132 + len(section) + len(listing) + len(image))
    struct.pack_into('<I', root, 20, 1); struct.pack_into('<I', root, 28, 101)
    (directory / 'ArtworkDB').write_bytes(root + section + listing + image)
    database = mount / 'iPod_Control/iTunes/iTunes Library.itlp/Library.itdb'
    database.parent.mkdir(parents=True)
    with closing(sqlite3.connect(database)) as con:
        con.executescript('''CREATE TABLE item (pid,title,album_pid,artwork_status,artwork_cache_id);
            INSERT INTO item VALUES (1,'Existing',10,1,100),(2,'New',20,0,0);
            CREATE TABLE album (pid,artwork_status,artwork_item_pid);
            INSERT INTO album VALUES (10,1,1),(20,0,0);''')
    return directory, database


@unittest.skipIf(Image is None, 'Pillow is required')
class ArtworkTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.mount = Path(self.tmp.name)
        self.directory, self.database = fixture(self.mount)
        self.cover = self.mount / 'cover.png'
        Image.new('RGB', (1280, 720), (255, 0, 0)).save(self.cover)
        self.before = {p: p.read_bytes() for p in self.directory.iterdir()}

    def test_add_preserves_existing_records_pixels_and_links_new_cover(self):
        self.assertEqual(art.install_cover(self.mount, 2, self.cover), 101)
        data = (self.directory / 'ArtworkDB').read_bytes()
        _, _, records = art.image_records(data)
        self.assertEqual(len(records), 2)
        original = self.before[self.directory / 'ArtworkDB']
        _, _, previous = art.image_records(original)
        p, n = previous[0]; q, m = records[0]
        self.assertEqual(data[q:q+m], original[p:p+n])
        self.assertEqual(struct.unpack_from('<Q', data, records[1][0] + 20)[0], 2)
        for path, before in self.before.items():
            if path.name == 'ArtworkDB': continue
            self.assertEqual(path.read_bytes()[:len(before)], before)
            fmt = int(path.name[1:5]); width, height, _ = art.FORMATS[fmt]
            self.assertEqual(path.stat().st_size, len(before) + width*height*2)
            # A red RGB565 little-endian pixel in the center of every format.
            pixels = path.read_bytes()[len(before):]
            center = ((height//2)*width + width//2)*2
            self.assertEqual(pixels[center:center+2], b'\x00\xf8')
        with closing(sqlite3.connect(self.database)) as con:
            self.assertEqual(con.execute('select artwork_status,artwork_cache_id from item where pid=2').fetchone(), (1,101))
            self.assertEqual(con.execute('select artwork_status,artwork_item_pid from album where pid=20').fetchone(), (1,2))

    @unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'ffmpeg tools required')
    def test_embedded_thumbnail_is_extracted_and_installed(self):
        from yt_download import run_command
        source = self.mount / 'embedded.m4a'
        run_command(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'sine=duration=0.1',
                     '-i', str(self.cover), '-map', '0:a', '-map', '1:v',
                     '-c:a', 'alac', '-c:v', 'mjpeg', '-disposition:v', 'attached_pic', str(source)])
        self.assertEqual(art.import_embedded_cover(self.mount, 2, source), 101)

    def test_deleted_last_artwork_can_be_replaced_without_reusing_its_id(self):
        path=self.directory/'ArtworkDB'
        path.write_bytes(art.without_track(path.read_bytes(),1))
        self.assertEqual(art.image_records(path.read_bytes())[2],[])
        self.assertEqual(art.install_cover(self.mount,2,self.cover),101)
        data=path.read_bytes();_,_,entries=art.image_records(data)
        self.assertEqual(len(entries),1)
        p,n=entries[0];self.assertEqual(len(art.thumbnails(data[p:p+n])),6)

    def test_retry_does_not_duplicate_native_artwork(self):
        art.install_cover(self.mount, 2, self.cover)
        once = {p: p.read_bytes() for p in self.directory.iterdir()}
        self.assertEqual(art.install_cover(self.mount, 2, self.cover), 101)
        self.assertEqual(once, {p: p.read_bytes() for p in self.directory.iterdir()})

    def test_publication_failure_rolls_back_only_appended_pixels(self):
        with patch.object(art, 'atomic_write', side_effect=OSError('fixture write failed')):
            with self.assertRaisesRegex(OSError, 'fixture write failed'):
                art.install_cover(self.mount, 2, self.cover)
        self.assertEqual(self.before, {p: p.read_bytes() for p in self.directory.iterdir()})
        with closing(sqlite3.connect(self.database)) as con:
            self.assertEqual(con.execute('select artwork_cache_id from item where pid=2').fetchone(), (0,))

    def test_malformed_database_rejected_without_modifying_thumbnail_files(self):
        path = self.directory / 'ArtworkDB'; path.write_bytes(path.read_bytes()[:-1])
        with self.assertRaises(ValueError): art.install_cover(self.mount, 2, self.cover)
        for path, before in self.before.items():
            if path.name != 'ArtworkDB': self.assertEqual(path.read_bytes(), before)


if __name__ == '__main__': unittest.main()
