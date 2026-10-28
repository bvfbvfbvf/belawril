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

    assert res["letter_loop_count"] == 2
    bat_loops = [e for e in res["event_log"] if e["source_word"] == "BAT" and e["event_type"] == "letter_loop"]
    assert len(bat_loops) == 2
    assert bat_loops[0]["target_word"] == "CAB"
    assert bat_loops[1]["target_word"] == "CAB"


def test_3_repeated_letters():
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

    # CAT 'T' triggers outer loop.
    # AREA 1st 'A', 'E', 2nd 'A' trigger outer loops + replayed CAT triggers inner 'T' loops -> 7 total
    assert res["letter_loop_count"] == 7


def test_4_layered_loops():
    # AB (0-1s) -> BA (1-2s) -> CD (2-3s)
    cues_text = """1
00:00:00,000 --> 00:00:01,000
AB

2
00:00:01,000 --> 00:00:02,000
BA

3
00:00:02,000 --> 00:00:03,000
CD
"""
    cues = simulator.parse_srt(cues_text, is_filepath=False)
    words = simulator.split_words(cues)
    sim = simulator.TimelineSimulator(words, video_duration=3.0)
    res = sim.generate_timeline()

    # BA triggers B (replays AB) and A (replays AB)
    assert res["letter_loop_count"] == 2

    # Check formatted log text output
    log_text = sim.format_event_log_text(res)
    assert "BETALIRILBIDS EVENT LOG" in log_text
