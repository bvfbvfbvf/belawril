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
    # Entry 1 "the" (0-1s), Entry 2 "cat" (1-2s) -> 't' matches "the"
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
    # Letter loop 't' targets base clip of "the" [0.0, 1.0]
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
    # Sequence: 1 "the", 2 "cat", 3 "sat", 4 "cat"
    # Entry 4 "cat" ->
    #   - Letter loop 'c' (from "cat" 2) -> target "cat" 2 base [1.0, 2.0]
    #   - Letter loop 'a' (from "sat" 3) -> target "sat" 3 base [2.0, 3.0]
    #   - Letter loop 't' (from "sat" 3) -> target "sat" 3 base [2.0, 3.0]
    #   - Whole-word replay of Entry 2 "cat" through Entry 3 "sat":
    #     - Entry 2's letter loop 't' ("the" 1 base [0.0, 1.0])
    #     - Entry 2's base clip ("cat" 2 base [1.0, 2.0])
    #     - Entry 3's letter loop 'a' ("cat" 2 base [1.0, 2.0])
    #     - Entry 3's letter loop 't' ("cat" 2 base [1.0, 2.0])
    #     - Entry 3's base clip ("sat" 3 base [2.0, 3.0])
    #   - Entry 4 base clip ("cat" 4 base [3.0, 4.0])
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
    # Check total duration:
    # Entry 1: 1.0s
    # Entry 2: 1.0s (letter loop 't') + 1.0s (base) = 2.0s
    # Entry 3: 1.0s ('a') + 1.0s ('t') + 1.0s (base) = 3.0s
    # Entry 4: 1.0s ('c') + 1.0s ('a') + 1.0s ('t') + Replay (Entry 2 & 3 seq = 5.0s) + 1.0s (base) = 9.0s
    # Total = 1 + 2 + 3 + 9 = 15.0s
    assert pytest.approx(res["total_duration"], 0.001) == 15.0

    # Event log text format check
    log_text = sim.format_event_log_text(res)
    assert "BELAWRIL EVENT LOG" in log_text
