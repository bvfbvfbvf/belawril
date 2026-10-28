import time
import os
import subprocess
import simulator
import belawril_engine
import repeat_video


def test_render_benchmark(tmp_path):
    video_path = str(tmp_path / "bench_input.mp4")
    srt_path = str(tmp_path / "bench_input.srt")
    out_path = str(tmp_path / "bench_output.mp4")

    cmd = [
        "ffmpeg", "-y", "-v", "error",
        "-f", "lavfi", "-i", "testsrc=duration=10:size=320x240:rate=30",
        "-f", "lavfi", "-i", "sine=frequency=1000:duration=10",
        "-c:v", "libx264", "-pix_fmt", "yuv420p",
        "-c:a", "aac", video_path
    ]
    subprocess.run(cmd, check=True)

    srt_content = """1
00:00:00,000 --> 00:00:01,000
the

2
00:00:01,000 --> 00:00:02,000
cat

3
00:00:02,000 --> 00:00:03,000
sat

4
00:00:03,000 --> 00:00:04,000
cat

5
00:00:04,000 --> 00:00:05,000
dog
"""
    with open(srt_path, "w", encoding="utf-8") as f:
        f.write(srt_content)

    cues = simulator.parse_srt(srt_path)
    words = simulator.split_words(cues)
    sim = simulator.TimelineSimulator(words, video_duration=10.0)
    sim_res = sim.generate_timeline()

    t0 = time.time()
    belawril_engine.run_belawril_render(
        video_path=video_path,
        srt_path=srt_path,
        output_path=out_path,
        mode="fast",
        keep_temp=True
    )
    elapsed = time.time() - t0

    assert os.path.exists(out_path)
    fps, has_audio, dur = repeat_video.probe(out_path)
    assert has_audio is True
    assert abs(dur - sim_res["total_duration"]) <= 0.5
    assert elapsed < 10.0  # Must render in under 10 seconds
