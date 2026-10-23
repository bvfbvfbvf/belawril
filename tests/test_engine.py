import os
import subprocess
import pytest
import belawril_engine
import simulator
import repeat_video


@pytest.fixture
def sample_video_and_srt(tmp_path):
    video_path = str(tmp_path / "test_input.mp4")
    srt_path = str(tmp_path / "test_input.srt")

    # Generate a 3-second test video with sine wave audio using ffmpeg
    cmd = [
        "ffmpeg", "-y", "-v", "error",
        "-f", "lavfi", "-i", "testsrc=duration=3:size=320x240:rate=30",
        "-f", "lavfi", "-i", "sine=frequency=1000:duration=3",
        "-c:v", "libx264", "-pix_fmt", "yuv420p",
        "-c:a", "aac", video_path
    ]
    subprocess.run(cmd, check=True)

    srt_content = """1
00:00:00,000 --> 00:00:01,000
cat

2
00:00:01,500 --> 00:00:02,500
cat
"""
    with open(srt_path, "w", encoding="utf-8") as f:
        f.write(srt_content)

    return video_path, srt_path


def test_engine_preview_render(sample_video_and_srt, tmp_path):
    video_path, srt_path = sample_video_and_srt
    output_path = str(tmp_path / "preview_out.mp4")

    belawril_engine.run_belawril_render(
        video_path=video_path,
        srt_path=srt_path,
        output_path=output_path,
        mode="compress",
        preview=True,
        preview_max_sec=2.0
    )

    assert os.path.exists(output_path)
    fps, has_audio, dur = repeat_video.probe(output_path)
    assert has_audio is True
    assert dur > 0.5 and dur <= 2.5
