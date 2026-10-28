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


def test_2_basic_letter_matching():
    # CAB (0-1s) -> DOG (1-2s) -> BAT (2-3s) -> FISH (3-4s)
    cues_text = """1
00:00:00,000 --> 00:00:01,000
CAB

2
00:00:01,000 --> 00:00:02,000
DOG

3
00:00:02,000 --> 00:00:03,000
BAT

4
00:00:03,000 --> 00:00:04,000
FISH
"""
    cues = simulator.parse_srt(cues_text, is_filepath=False)
    words = simulator.split_words(cues)
    sim = simulator.TimelineSimulator(words, video_duration=4.0)
    res = sim.generate_timeline()

    # BAT triggers 'B' and 'A' (each generating outer letter loop + inner loops at W_curr for letters up to and including trigger letter)
    assert res["letter_loop_count"] == 5
    bat_loops = [e for e in res["event_log"] if e["source_word"] == "BAT" and e["event_type"] == "letter_loop"]
    assert len(bat_loops) == 5
    assert bat_loops[0]["target_word"] == "CAB"


def test_3_layered_inner_loops():
    # THE (0-1s) -> CAT (1-2s) -> AREA (2-3s)
    cues_text = """1
00:00:00,000 --> 00:00:01,000
THE

2
00:00:01,000 --> 00:00:02,000
CAT

3
00:00:02,000 --> 00:00:03,000
AREA
"""
    cues = simulator.parse_srt(cues_text, is_filepath=False)
    words = simulator.split_words(cues)
    sim = simulator.TimelineSimulator(words, video_duration=3.0)
    res = sim.generate_timeline()

    # Outer letter loops + 2nd-layer inner loops = 14 total letter loop events
    assert res["letter_loop_count"] == 14

    # Check formatted log text output
    log_text = sim.format_event_log_text(res)
    assert "BETALIRILBIDS EVENT LOG" in log_text
