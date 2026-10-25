"""Persistent storage footer with a background, cancelable allocation scan."""
from pathlib import Path
import threading
from PySide6.QtCore import QObject,QRunnable,QThreadPool,Signal,Slot,Qt,QTimer,QRectF
from PySide6.QtGui import QColor,QPainter,QPainterPath
from PySide6.QtWidgets import QWidget,QVBoxLayout,QHBoxLayout,QLabel,QProgressBar
from storage_usage import storage_usage,format_bytes,ScanCanceled

COLORS={'music_bytes':'#579aff','photo_bytes':'#e5a34a','other_bytes':'#929aaa','free_bytes':'#539d80'}
CATEGORIES=[('music_bytes','Music','Audio files and their album artwork.'),
            ('photo_bytes','Photos','Stored photo thumbnails and originals.'),
            ('other_bytes','Other','Library databases, system files, trash and filesystem overhead.'),
            ('free_bytes','Free','Available for more music, photos and other files.')]


class SegmentedBar(QProgressBar):
    def __init__(self,parent=None):
        super().__init__(parent)
        self.usage=None;self.setRange(0,100);self.setValue(0);self.setTextVisible(False)
        self.setFixedHeight(16);self.setAccessibleName('iPod storage used')
    def set_usage(self,usage):
        self.usage=usage
        self.setValue(round(usage['used_bytes']*100/usage['total_bytes']) if usage and usage['total_bytes'] else 0)
        self.update()
    def paintEvent(self,event):
        painter=QPainter(self);painter.setRenderHint(QPainter.Antialiasing)
        rect=QRectF(self.rect()).adjusted(.5,.5,-.5,-.5)
        path=QPainterPath();path.addRoundedRect(rect,5,5)
        painter.fillPath(path,self.palette().alternateBase())
        if self.usage and self.usage['total_bytes']:
            painter.save();painter.setClipPath(path)
            x=rect.left();total=self.usage['total_bytes']
            for key,_,_ in CATEGORIES:
                width=rect.width()*self.usage[key]/total
                painter.fillRect(QRectF(x,rect.top(),width,rect.height()),QColor(COLORS[key]));x+=width
            painter.restore()
        painter.setPen(self.palette().mid().color());painter.drawPath(path)


class ScanSignals(QObject):
    done=Signal(int,object,object)


class Scan(QRunnable):
    def __init__(self,generation,mount):
        super().__init__();self.generation=generation;self.mount=mount
        self.cancel=threading.Event();self.signals=ScanSignals()
    def run(self):
        try:self.signals.done.emit(self.generation,storage_usage(self.mount,self.cancel.is_set),None)
        except ScanCanceled:self.signals.done.emit(self.generation,None,None)
        except Exception as error:self.signals.done.emit(self.generation,None,str(error))


class StorageFooter(QWidget):
    def __init__(self,parent=None):
        super().__init__(parent)
        self.usage=None;self.mount='';self._generation=0;self._active=None;self._pending=None
        layout=QVBoxLayout(self);layout.setContentsMargins(0,3,0,0);layout.setSpacing(6)
        heading=QHBoxLayout();title=QLabel('Storage');title.setStyleSheet('font-weight: 600;')
        heading.addWidget(title);heading.addStretch();self.summary=QLabel();heading.addWidget(self.summary)
        layout.addLayout(heading)
        self.bar=SegmentedBar();layout.addWidget(self.bar)
        legend=QHBoxLayout();self.labels={}
        for key,name,description in CATEGORIES:
            label=QLabel();label.setTextFormat(Qt.RichText);label.setToolTip(description)
            self.labels[key]=label;legend.addWidget(label)
        layout.addLayout(legend)
        self.clear()
    def clear(self,message='Connect an iPod to see available storage.'):
        self._generation+=1;self._pending=None;self.mount='';self.usage=None
        if self._active:self._active.cancel.set()
        self.bar.set_usage(None);self.summary.setText(message);self.bar.setToolTip(message)
        self.bar.setAccessibleDescription(message)
        for key,name,_ in CATEGORIES:self.labels[key].setText(f'<span style="color:{COLORS[key]}">●</span> {name} —')
    def refresh(self,mount):
        mount=str(Path(mount).resolve()) if mount else ''
        if not mount or not (Path(mount)/'iPod_Control').is_dir():self.clear();return
        if mount!=self.mount:
            self.clear('Reading iPod storage…');self.mount=mount
        self._generation+=1;self._pending=(self._generation,mount)
        if self._active:
            self._active.cancel.set();return
        self._start()
    def _start(self):
        if not self._pending:return
        generation,mount=self._pending;self._pending=None
        self._active=Scan(generation,mount);self._active.signals.done.connect(self._done)
        QThreadPool.globalInstance().start(self._active)
    @Slot(int,object,object)
    def _done(self,generation,usage,error):
        self._active=None
        if generation==self._generation:
            if error:
                self.clear('Storage unavailable — choose Refresh to retry.');self.setToolTip(error)
            elif usage is not None:
                self.set_usage(usage)
        self._start()
    def set_usage(self,usage):
        self.usage=usage;total=usage['total_bytes'];percent=usage['used_bytes']*100/total if total else 0
        summary=f"{format_bytes(usage['used_bytes'])} of {format_bytes(total)} used ({percent:.1f}%) · {format_bytes(usage['free_bytes'])} free"
        self.summary.setText(summary);self.bar.set_usage(usage)
        tooltip=[]
        for key,name,description in CATEGORIES:
            share=usage[key]*100/total if total else 0
            text=f'{name} {format_bytes(usage[key])} ({share:.1f}%)'
            self.labels[key].setText(f'<span style="color:{COLORS[key]}">●</span> {text}')
            self.labels[key].setToolTip(f'{description}\n{usage[key]:,} bytes · {share:.2f}% of capacity')
            tooltip.append(text)
        self.bar.setToolTip('\n'.join(tooltip));self.bar.setAccessibleDescription(summary+'; '+', '.join(tooltip))
        self.setToolTip('Sizes use the space allocated on the iPod. GB = 1,000,000,000 bytes.')
    def stop(self):
        self._generation+=1;self._pending=None
        if self._active:self._active.cancel.set()
