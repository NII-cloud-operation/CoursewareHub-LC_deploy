"""Render the recorded foreground pages as chapters and one normal-speed walkthrough."""
import argparse
from itertools import groupby
import json
from pathlib import Path
import subprocess
import tempfile


def ffmpeg(*args, cwd):
    subprocess.run(['ffmpeg', '-hide_banner', '-loglevel', 'warning', '-y', *args], cwd=cwd, check=True)


def concatenate(parts, output, work):
    (work / 'concat.txt').write_text(''.join(f"file '{part}'\n" for part in parts))
    ffmpeg('-f', 'concat', '-safe', '0', '-i', 'concat.txt', '-c', 'copy',
           '-movflags', '+faststart', str(output), cwd=work)


def render(results):
    segments = []
    for manifest in results.rglob('segments.json'):
        for segment in json.loads(manifest.read_text()):
            segment['path'] = manifest.parent / segment['path']
            segments.append(segment)
    if not segments:
        raise ValueError(f'No recorded pages in {results}')
    segments.sort(key=lambda segment: segment['time'])
    movies = results / 'movies'
    movies.mkdir(exist_ok=True)
    encoding = ['-an', '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '23', '-pix_fmt', 'yuv420p',
                '-r', '25', '-video_track_timescale', '12800']
    chapters = []
    with tempfile.TemporaryDirectory(dir=movies) as directory:
        work = Path(directory)
        for number, (title, group) in enumerate(groupby(segments, key=lambda s: s['chapter']), 1):
            (work / 'title.txt').write_text(title)
            card = work / 'title.mp4'
            ffmpeg('-f', 'lavfi', '-i', 'color=c=0x17202a:s=1600x1000:r=25:d=2',
                   '-vf', 'drawtext=fontfile=/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc:'
                   'textfile=title.txt:expansion=none:fontcolor=white:fontsize=32:x=(w-text_w)/2:y=(h-text_h)/2',
                   *encoding, str(card), cwd=work)
            clips = [card]
            for index, segment in enumerate(group):
                clip = work / f'{index}.mp4'
                ffmpeg('-ss', str(segment['start']), '-i', str(segment['path']),
                       '-t', str(segment['end'] - segment['start']),
                       '-vf', 'fps=25,scale=1600:1000:force_original_aspect_ratio=decrease,pad=1600:1000:(ow-iw)/2:(oh-ih)/2,setsar=1',
                       *encoding, str(clip), cwd=work)
                clips.append(clip)
            chapter = movies / f'{number:02d}-{title}.mp4'
            concatenate(clips, chapter, work)
            chapters.append(chapter)
        concatenate(chapters, movies / 'walkthrough.mp4', work)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('results', type=Path)
    render(parser.parse_args().results.resolve())
