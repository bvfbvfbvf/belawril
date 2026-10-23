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


def test_2_word_letter_loop_targets_base_clip():
    # Entry 1 "the" (0-1s), Entry 2 "cat" (1-2s) -> 't' matches "the" (jump 0:00 to 0:01)
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

    assert res["letter_loop_count"] == 1
    assert res["word_loop_count"] == 0
    letter_segs = [s for s in res["segments"] if s["type"] == "letter_loop"]
    assert len(letter_segs) == 1
    assert letter_segs[0]["start"] == 0.0
    assert letter_segs[0]["end"] == 1.0


def test_3_and_4_repeated_letters_and_multiple_loops():
    cues_text = """1
00:00:00,000 --> 00:00:01,000
the

2
00:00:01,000 --> 00:00:02,000
cat

3
00:00:02,000 --> 00:00:03,000
there
"""
    cues = simulator.parse_srt(cues_text, is_filepath=False)
    words = simulator.split_words(cues)
    sim = simulator.TimelineSimulator(words, video_duration=3.0)
    res = sim.generate_timeline()

    # "there": 't' (from "cat"), 'h' (from "the"), 'e' (from "the"), 'r' (none), 2nd 'e' (from "the")
    there_letter_loops = [e for e in res["event_log"] if e["source_word"] == "there" and e["event_type"] == "letter_loop"]
    assert len(there_letter_loops) == 4


def test_5_whole_word_replay_timeline_sequence():
    cues_text = """1
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
"""
    cues = simulator.parse_srt(cues_text, is_filepath=False)
    words = simulator.split_words(cues)
    sim = simulator.TimelineSimulator(words, video_duration=4.0)
    res = sim.generate_timeline()

    assert res["word_loop_count"] == 1
    # Duration = 20.0s matching approved sequence table
    assert res["total_duration"] == 20.0

    # Event log text format check
    log_text = sim.format_event_log_text(res)
    assert "BELAWRIL EVENT LOG" in log_text
