"""Read actual volume capacity and allocated media space without opening audio."""
from pathlib import Path
import os
import shutil


class ScanCanceled(Exception):
    pass


def storage_usage(mount, canceled=lambda: False):
    root=Path(mount)
    if not (root/'iPod_Control').is_dir():raise FileNotFoundError('No iPod at this mount.')
    device=root.stat().st_dev
    unit=os.statvfs(root).f_frsize or 4096
    seen=set()
    def size(directory):
        if canceled():raise ScanCanceled()
        if not directory.exists():return 0
        total=0
        with os.scandir(directory) as entries:
            for entry in entries:
                if canceled():raise ScanCanceled()
                if entry.is_symlink():continue
                stat=entry.stat(follow_symlinks=False)
                if stat.st_dev!=device:continue
                if entry.is_dir(follow_symlinks=False):total+=size(Path(entry.path))
                elif entry.is_file(follow_symlinks=False):
                    key=(stat.st_dev,stat.st_ino)
                    if key in seen:continue
                    seen.add(key)
                    blocks=getattr(stat,'st_blocks',None)
                    total+=blocks*512 if blocks is not None else ((stat.st_size+unit-1)//unit)*unit
        return total
    music=size(root/'iPod_Control/Music')+size(root/'iPod_Control/Artwork')
    photos=size(root/'Photos')
    if canceled():raise ScanCanceled()
    # Recheck after scanning: unplugging must not display the host disk's capacity.
    if not (root/'iPod_Control').is_dir() or root.stat().st_dev!=device:
        raise FileNotFoundError('iPod disconnected during storage scan.')
    capacity=shutil.disk_usage(root)
    total=capacity.total;free=capacity.free;used=total-free
    music=min(music,used);photos=min(photos,max(0,used-music))
    return dict(total_bytes=total,used_bytes=used,free_bytes=free,music_bytes=music,
                photo_bytes=photos,other_bytes=max(0,used-music-photos))


def format_bytes(value):
    for unit,size in [('GB',10**9),('MB',10**6),('KB',10**3)]:
        if value>=size:return f'{value/size:.2f} {unit}' if unit=='GB' else f'{value/size:.1f} {unit}'
    return f'{value} B'
