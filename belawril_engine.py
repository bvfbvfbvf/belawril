"""
Belawril Fast Rendering Engine.
High-performance video renderer utilizing fast Annex-B H.264 stream copying, MP4 sample table referencing,
and single-pass filter graph rendering for maximum speed.
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


def render_single_pass_concat(video_path, timeline_segs, output_path, fps, has_audio, crf, preset, audio_bitrate):
    """
    Renders timeline segments in a single pass using ffmpeg filter complex.
    Avoids spawning individual ffmpeg processes per clip, executing in seconds instead of minutes.
    """
    if not timeline_segs:
        raise ValueError("No timeline segments to render.")

    # Filter complex trim construction
    # Group into batches if filter graph exceeds command line limits
    batch_size = 300
    if len(timeline_segs) <= batch_size:
        v_parts = []
        a_parts = []
        for i, s in enumerate(timeline_segs):
            start = s["start"]
            end = s["end"]
            v_parts.append(f"[0:v]trim=start={start:.6f}:end={end:.6f},setpts=PTS-STARTPTS[v{i}];")
            if has_audio:
                a_parts.append(f"[0:a]atrim=start={start:.6f}:end={end:.6f},asetpts=PTS-STARTPTS[a{i}];")

        concat_in = ""
        for i in range(len(timeline_segs)):
            concat_in += f"[v{i}]" + (f"[a{i}]" if has_audio else "")

        audio_out_spec = ":a=1" if has_audio else ":a=0"
        filter_str = "".join(v_parts) + "".join(a_parts) + f"{concat_in}concat=n={len(timeline_segs)}:v=1{audio_out_spec}[outv]" + ("[outa]" if has_audio else "")

        cmd = [
            "ffmpeg", "-y", "-v", "error", "-i", video_path,
            "-filter_complex", filter_str,
            "-map", "[outv]"
        ]
        if has_audio:
            cmd += ["-map", "[outa]", "-c:a", "aac", "-b:a", audio_bitrate]
        else:
            cmd += ["-an"]

        cmd += ["-c:v", "libx264", "-preset", preset, "-crf", str(crf), "-pix_fmt", "yuv420p", "-r", "%.5f" % fps,
                "-movflags", "+faststart", output_path]
        ref_engine.run(cmd)
    else:
        # For extremely large timeline segment lists (>300 segments), slice into chunk files in batches
        tmp = tempfile.mkdtemp(prefix="belawril_batch_")
        try:
            chunk_files = []
            for batch_i in range(0, len(timeline_segs), batch_size):
                batch = timeline_segs[batch_i:batch_i + batch_size]
                v_parts = []
                a_parts = []
                for i, s in enumerate(batch):
                    start = s["start"]
                    end = s["end"]
                    v_parts.append(f"[0:v]trim=start={start:.6f}:end={end:.6f},setpts=PTS-STARTPTS[v{i}];")
                    if has_audio:
                        a_parts.append(f"[0:a]atrim=start={start:.6f}:end={end:.6f},asetpts=PTS-STARTPTS[a{i}];")

                concat_in = ""
                for i in range(len(batch)):
                    concat_in += f"[v{i}]" + (f"[a{i}]" if has_audio else "")

                audio_out_spec = ":a=1" if has_audio else ":a=0"
                filter_str = "".join(v_parts) + "".join(a_parts) + f"{concat_in}concat=n={len(batch)}:v=1{audio_out_spec}[outv]" + ("[outa]" if has_audio else "")

                chunk_path = os.path.join(tmp, f"chunk_{batch_i // batch_size:04d}.mp4")
                cmd = [
                    "ffmpeg", "-y", "-v", "error", "-i", video_path,
                    "-filter_complex", filter_str,
                    "-map", "[outv]"
                ]
                if has_audio:
                    cmd += ["-map", "[outa]", "-c:a", "aac", "-b:a", audio_bitrate]
                else:
                    cmd += ["-an"]

                cmd += ["-c:v", "libx264", "-preset", preset, "-crf", str(crf), "-pix_fmt", "yuv420p", "-r", "%.5f" % fps,
                        "-movflags", "+faststart", chunk_path]
                ref_engine.run(cmd)
                chunk_files.append(chunk_path)

            # Concatenate chunk files using concat demuxer
            list_path = os.path.join(tmp, "list.txt")
            with open(list_path, "w") as lf:
                for c_path in chunk_files:
                    lf.write(f"file '{c_path}'\n")

            cmd = [
                "ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", list_path,
                "-c", "copy", "-movflags", "+faststart", output_path
            ]
            ref_engine.run(cmd)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


def run_belawril_render(video_path, srt_path, output_path, mode="fast", preview=False, preview_max_sec=15.0,
                         crf=None, preset="faster", audio_bitrate="160k", progress_callback=None,
                         keep_temp=True):
    """
    Renders Belawril timeline.

    Parameters:
    - video_path: path to input video
    - srt_path: path to input SRT
    - output_path: path to output video
    - mode: 'fast' (fast re-encode keyframes + Annex-B stream copy), 'tiny' (tiny file MP4 sample reference), or 'compress' (single-pass filter complex)
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
        fast_success = False
        if mode == "tiny":
            try:
                ref_engine.tiny_render(args_obj, segs, fps, has_audio, total_frames_all, tmp, joined_tmp, crf)
                fast_success = True
            except ref_engine.TinyError:
                fast_success = False
        elif mode == "fast":
            try:
                ref_engine.fast_render(args_obj, segs, fps, has_audio, total_frames_all, tmp, joined_tmp, crf)
                fast_success = True
            except ref_engine.FastPathError:
                fast_success = False

        if not fast_success:
            # Single-pass filter complex render (fast, avoids thousands of subprocesses)
            render_single_pass_concat(video_path, timeline_segs, joined_tmp, fps, has_audio, crf, preset, audio_bitrate)

        shutil.move(joined_tmp, output_path)
    finally:
        if not keep_temp:
            shutil.rmtree(tmp, ignore_errors=True)

    return output_path
