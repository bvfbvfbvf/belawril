"""
Belawril Fast Rendering Engine.
Uses fast rendering (Annex-B H.264 slice repetition) and tiny-file mode (MP4 sample table referencing)
adapted from repeat_video.py for Belawril word-and-letter timelines.
"""

import os
import sys
import shutil
import tempfile
import time
import subprocess
from fractions import Fraction

import repeat_video as ref_engine
from simulator import parse_srt, split_words, TimelineSimulator


def setup_ffmpeg_path():
    """Ensure ffmpeg and ffprobe can be located."""
    user_bin = os.path.expanduser("~/.local/bin")
    app_dir = os.path.dirname(os.path.abspath(__file__))
    current_path = os.environ.get("PATH", "")
    dirs = [user_bin, app_dir]
    for d in dirs:
        if d and d not in current_path.split(os.pathsep):
            current_path = d + os.pathsep + current_path
    os.environ["PATH"] = current_path


setup_ffmpeg_path()


def run_belawril_render(video_path, srt_path, output_path, mode="fast", preview=False, preview_max_sec=15.0,
                         crf=None, preset="faster", audio_bitrate="160k", progress_callback=None,
                         keep_temp=True):
    """
    Renders Belawril timeline.

    Parameters:
    - video_path: path to input video
    - srt_path: path to input SRT
    - output_path: path to output video
    - mode: 'fast' (fast re-encode keyframes + byte copy), 'tiny' (tiny file MP4 sample reference), or 'compress' (full re-encode concat)
    - preview: if True, limits render to first `preview_max_sec` seconds of timeline
    - crf: video quality CRF
    - preset: x264 preset
    - audio_bitrate: audio bitrate string
    - progress_callback: optional function for progress reporting
    - keep_temp: if True, preserves intermediate files without deleting them
    """
    setup_ffmpeg_path()

    for tool in ("ffmpeg", "ffprobe"):
        if not shutil.which(tool):
            raise RuntimeError(f"'{tool}' not found on PATH. Please install FFmpeg.")

    fps, has_audio, vid_dur = ref_engine.probe(video_path)
    cues = parse_srt(srt_path)
    words = split_words(cues)

    if not words:
        raise ValueError("No valid word cues found in SRT file.")

    sim = TimelineSimulator(words, video_duration=vid_dur)
    sim_res = sim.generate_timeline()
    timeline_segs = sim_res["segments"]

    if preview:
        accum = 0.0
        p_segs = []
        for s in timeline_segs:
            dur = s["end"] - s["start"]
            if accum + dur > preview_max_sec:
                rem = preview_max_sec - accum
                if rem > 0.05:
                    p_segs.append({
                        "type": s["type"],
                        "start": s["start"],
                        "end": s["start"] + rem,
                        "label": s["label"]
                    })
                break
            p_segs.append(s)
            accum += dur
        timeline_segs = p_segs or timeline_segs[:1]

    total_frames_all = int(round(vid_dur * fps))
    exact = ref_engine.count_video_frames(video_path)
    if exact and abs(exact - total_frames_all) <= max(3, total_frames_all * 0.01):
        total_frames_all = exact

    segs = []
    for s in timeline_segs:
        sf = int(round(s["start"] * fps))
        ef = int(round(s["end"] * fps))
        if ef <= sf:
            ef = min(sf + 1, total_frames_all)
        sf = max(0, min(sf, total_frames_all))
        ef = max(0, min(ef, total_frames_all))
        if ef > sf:
            segs.append((sf, ef, 1, s["label"]))

    if not segs:
        raise ValueError("Timeline resulting video has 0 duration.")

    if crf is None:
        crf = 22 if (mode in ("compress", "tiny")) else 26

    class DummyArgs:
        def __init__(self):
            self.video = video_path
            self.preset = preset
            self.audio_bitrate = audio_bitrate

    args_obj = DummyArgs()
    tmp = tempfile.mkdtemp(prefix="belawril_")
    joined_tmp = os.path.join(tmp, "joined.mp4")

    try:
        if mode == "tiny":
            ref_engine.tiny_render(args_obj, segs, fps, has_audio, total_frames_all, tmp, joined_tmp, crf)
        elif mode == "fast":
            try:
                ref_engine.fast_render(args_obj, segs, fps, has_audio, total_frames_all, tmp, joined_tmp, crf)
            except ref_engine.FastPathError as e:
                mode = "compress"

        if mode == "compress":
            vopts = ["-c:v", "libx264", "-preset", preset, "-crf", str(crf), "-pix_fmt", "yuv420p", "-r", "%.5f" % fps]
            aopts = ["-c:a", "pcm_s16le", "-ar", "48000", "-ac", "2"] if has_audio else ["-an"]
            files = {}
            uniq = []
            for s_, e_, n_, _ in segs:
                if (s_, e_) not in files:
                    files[(s_, e_)] = f"clip_{len(files):05d}.mov"
                    uniq.append((s_, e_))
            for i, (s_, e_) in enumerate(uniq, 1):
                af = ["-af", "apad,atrim=duration=%.6f" % ((e_ - s_) / fps)] if has_audio else []
                ref_engine.run(["ffmpeg", "-y", "-v", "error", "-ss", "%.6f" % (s_ / fps), "-i", video_path,
                               "-frames:v", str(e_ - s_)] + vopts + aopts + af + [os.path.join(tmp, files[(s_, e_)])])
            list_path = os.path.join(tmp, "list.txt")
            with open(list_path, "w") as lf:
                for s_, e_, n_, _ in segs:
                    lf.write(f"file '{files[(s_, e_)]}'\n")
            ref_engine.run(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", list_path,
                           "-c:v", "copy"] + (["-c:a", "aac", "-b:a", audio_bitrate] if has_audio else ["-an"])
                           + ["-movflags", "+faststart", joined_tmp])

        shutil.move(joined_tmp, output_path)
    finally:
        if not keep_temp:
            shutil.rmtree(tmp, ignore_errors=True)

    return output_path
