"""Record the visible page in operator order, including popups and file editors."""
import json
from pathlib import Path
import time


class BrowserRecording:
    def __init__(self, browser, videos, chapter, *, viewport=None):
        self.directory = videos
        self.chapter = chapter
        self.open_pages = []
        self.segments = []
        size = viewport or {'width': 1280, 'height': 720}
        self.context = browser.new_context(
            ignore_https_errors=True, viewport=size,
            record_video_dir=str(videos), record_video_size=size)
        self.context.on('page', self.open_page)

    def pause(self, now):
        if self.open_pages:
            page, opened = self.open_pages[-1]
            self.segments.append(dict(chapter=self.chapter, time=self.active_since,
                                      video=page.video, start=self.active_since - opened,
                                      end=now - opened))

    def open_page(self, page):
        now = time.monotonic()
        self.pause(now)
        self.open_pages.append((page, now))
        self.active_since = now
        page.on('close', self.close_page)

    def close_page(self, page):
        index = next(i for i, (opened, _) in enumerate(self.open_pages) if opened is page)
        if index == len(self.open_pages) - 1:
            now = time.monotonic()
            self.pause(now)
            self.active_since = now
        self.open_pages.pop(index)

    def finish(self, trace):
        self.pause(time.monotonic())
        self.context.remove_listener('page', self.open_page)
        for page, _ in self.open_pages:
            page.remove_listener('close', self.close_page)
        try:
            self.context.tracing.stop(path=str(trace))
        finally:
            self.context.close()
        if self.segments:
            for segment in self.segments:
                segment['path'] = Path(segment.pop('video').path()).name
            (self.directory / 'segments.json').write_text(json.dumps(self.segments, indent=2))
