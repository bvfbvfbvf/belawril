import pytest
import simulator


def test_1_word_no_previous_letter_matches():
    cues_text = """1
00:00:00,000 --> 00:00:01,000
the
"""
    cues = simulator.parse_srt(cues_text, is_filepath=False)
    words = simulator.split_words(cues)
    sim = simulator.TimelineSimulator(words, video_duration=1.0)
    res = sim.generate_timeline()

    assert res["letter_loop_count"] == 0
    assert res["word_loop_count"] == 0
    assert len(res["segments"]) == 1
    assert res["segments"][0]["type"] == "word_base"


def test_2_letter_loops_play_before_base_clip():
    # Entry 1 "the" (0-1s), Entry 2 "cat" (1-2s)
    # Entry 2 "cat" letter loop 't' plays FIRST: targets "the" [0-1s]
    # THEN base clip "cat" [1-2s] plays, advancing playback to Entry 3
    cues_text = """1
00:00:00,000 --> 00:00:01,000
the

2
00:00:01,000 --> 00:00:02,000
cat
"""
    cues = simulator.parse_srt(cues_text, is_filepath=False)
    words = simulator.split_words(cues)
    sim = simulator.TimelineSimulator(words, video_duration=2.0)
    res = sim.generate_timeline()

    segs = res["segments"]
    assert len(segs) == 3
    # 1. Base clip "the" [0-1s]
    assert segs[0]["type"] == "word_base" and segs[0]["label"] == "the"
    # 2. Letter loop 't' triggers BEFORE "cat" base clip: targets [0-1s]
    assert segs[1]["type"] == "letter_loop" and segs[1]["start"] == 0.0 and segs[1]["end"] == 1.0
    # 3. Base clip "cat" [1-2s] plays AFTER letter loop 't'
    assert segs[2]["type"] == "word_base" and segs[2]["label"] == "cat"


def test_3_three_words_ordering():
    # 1 "the" (0-1s), 2 "cat" (1-2s), 3 "sat" (2-3s)
    # Entry 1: base "the" (0-1s)
    # Entry 2: letter loop 't' [0-1s] -> base "cat" (1-2s)
    # Entry 3: letter loop 'a' [1-2s] -> letter loop 't' [1-2s] -> base "sat" (2-3s)
    cues_text = """1
00:00:00,000 --> 00:00:01,000
the

2
00:00:01,000 --> 00:00:02,000
cat

3
00:00:02,000 --> 00:00:03,000
sat
"""
    cues = simulator.parse_srt(cues_text, is_filepath=False)
    words = simulator.split_words(cues)
    sim = simulator.TimelineSimulator(words, video_duration=3.0)
    res = sim.generate_timeline()

    segs = res["segments"]
    labels = [s["label"] for s in segs]
    expected_labels = [
        "the",
        "Letter loop 't' -> 'the' (cat)",
        "cat",
        "Letter loop 'a' -> 'cat' (sat)",
        "Letter loop 't' -> 'cat' (sat)",
        "sat",
    ]
    assert labels == expected_labels
    assert res["letter_loop_count"] == 3


def test_4_event_log_formatting():
    cues_text = """1
00:00:00,000 --> 00:00:01,000
AB

2
00:00:01,000 --> 00:00:02,000
BA
"""
    cues = simulator.parse_srt(cues_text, is_filepath=False)
    words = simulator.split_words(cues)
    sim = simulator.TimelineSimulator(words, video_duration=2.0)
    res = sim.generate_timeline()

    log_text = sim.format_event_log_text(res)
    assert "BETALIRILBIDS EVENT LOG" in log_text
