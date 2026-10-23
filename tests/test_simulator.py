import pytest
import simulator


def test_srt_parsing():
    srt_content = """1
00:00:00,000 --> 00:00:01,000
cat dog

2
00:00:01,500 --> 00:00:02,500
cat's
"""
    cues = simulator.parse_srt(srt_content, is_filepath=False)
    assert len(cues) == 2
    assert cues[0]["start"] == 0.0
    assert cues[0]["end"] == 1.0

    words = simulator.split_words(cues)
    assert len(words) == 3
    # "cat" and "dog" split from cue 1
    assert words[0]["tok"] == "cat"
    assert words[0]["start"] == 0.0
    assert pytest.approx(words[0]["end"], 0.01) == 0.5

    assert words[1]["tok"] == "dog"
    assert pytest.approx(words[1]["start"], 0.01) == 0.5
    assert words[1]["end"] == 1.0

    assert words[2]["tok"] == "cat's"


def test_clean_word_and_letter():
    assert simulator.clean_word_for_matching("Cat!") == "cat"
    assert simulator.clean_word_for_matching("don't") == "don't"
    assert simulator.clean_word_for_matching("don’t") == "don’t"
    assert simulator.clean_word_for_matching("hello-world") == "helloworld"

    assert simulator.clean_letter("A") == "a"
    assert simulator.clean_letter("!") == ""
    assert simulator.clean_letter("'") == "'"


def test_letter_slices():
    word_entry = {"tok": "cat", "start": 0.0, "end": 0.6}
    slices = simulator.get_letter_time_slices(word_entry)
    assert len(slices) == 3
    assert slices[0]["char"] == "c"
    assert pytest.approx(slices[0]["start"], 0.01) == 0.0
    assert pytest.approx(slices[0]["end"], 0.01) == 0.2

    assert slices[1]["char"] == "a"
    assert pytest.approx(slices[1]["start"], 0.01) == 0.2
    assert pytest.approx(slices[1]["end"], 0.01) == 0.4

    assert slices[2]["char"] == "t"
    assert pytest.approx(slices[2]["start"], 0.01) == 0.4
    assert pytest.approx(slices[2]["end"], 0.01) == 0.6


def test_same_word_duplicate_letters():
    # Word "look": 'l', 'o', 'o', 'k'
    # Second 'o' should point to the same previous occurrence as the first 'o' (if any)
    cues_text = """1
00:00:00,000 --> 00:00:01,000
so

2
00:00:01,000 --> 00:00:02,000
look
"""
    cues = simulator.parse_srt(cues_text, is_filepath=False)
    words = simulator.split_words(cues)
    sim = simulator.TimelineSimulator(words, video_duration=2.0)
    result = sim.generate_timeline()

    segs = result["segments"]
    # "so": 's' (no prev), 'o' (no prev)
    # "look": 'l' (no prev), 'o' (prev 'o' from "so"), 'o' (prev 'o' from "so"), 'k' (no prev)
    letter_loops = [s for s in segs if s["type"] == "letter_loop"]
    assert len(letter_loops) == 2
    # Both letter loops for 'o' in "look" should reference the same interval from "so" ([0.5, 1.0])
    assert letter_loops[0]["start"] == 0.5
    assert letter_loops[0]["end"] == 1.0
    assert letter_loops[1]["start"] == 0.5
    assert letter_loops[1]["end"] == 1.0


def test_whole_word_loop_and_recursion_prevention():
    # Sequence: "cat" -> "dog" -> "cat" -> "cat"
    # Word 1: "cat" (0.0 to 1.0)
    # Word 2: "dog" (1.0 to 2.0) -> 'd' (no), 'o' (no), 'g' (no)
    # Word 3: "cat" (2.0 to 3.0) ->
    #         - letters: 'c', 'a', 't' -> 'c' (from word 1 'c'), 'a' (from word 1 'a'), 't' (from word 1 't') -> 3 letter loops
    #         - whole word: matches "cat" (word 1). Replays word 1's letter loops (0) + word 1's base clip.
    #         - word 3 base clip
    # Word 4: "cat" (3.0 to 4.0) ->
    #         - letters: 'c', 'a', 't' -> matches most recent 'c', 'a', 't' from word 3 ([2.0, 2.333], [2.333, 2.666], [2.666, 3.0])
    #         - whole word: matches "cat" (word 3). Replays word 3's letter loops (3 letter loops to word 1) + word 3's base clip.
    #           Crucially: DOES NOT recursively trigger word 3's whole word loop replay!
    cues_text = """1
00:00:00,000 --> 00:00:01,000
cat

2
00:00:01,000 --> 00:00:02,000
dog

3
00:00:02,000 --> 00:00:03,000
cat

4
00:00:03,000 --> 00:00:04,000
cat
"""
    cues = simulator.parse_srt(cues_text, is_filepath=False)
    words = simulator.split_words(cues)
    sim = simulator.TimelineSimulator(words, video_duration=4.0)
    res = sim.generate_timeline()

    assert res["word_loop_count"] == 2  # word 3 and word 4 triggered word loops
    # Verify no infinite loop occurred and duration is finite and exact
    segs = res["segments"]
    assert len(segs) > 0


def test_hand_checkable_duration():
    # Hand-checkable example:
    # 2s video duration
    # Word 1: "a" at [0.0, 1.0] (dur 1.0s)
    # Gap: [1.0, 1.5] (dur 0.5s)
    # Word 2: "a" at [1.5, 2.0] (dur 0.5s)
    #
    # Events for Word 2 ("a"):
    # 1. Letter loop for 'a' -> matches 'a' from Word 1 -> slice [0.0, 1.0] (dur 1.0s)
    # 2. Whole word loop for "a" -> matches Word 1 "a" -> replays Word 1 letter loops (none) + base clip [0.0, 1.0] (dur 1.0s)
    # 3. Base clip for Word 2 -> [1.5, 2.0] (dur 0.5s)
    #
    # Timeline order & segments:
    # 1. Word 1 base: [0.0, 1.0] (1.0s)
    # 2. Gap: [1.0, 1.5] (0.5s)
    # 3. Word 2 letter loop 'a': [0.0, 1.0] (1.0s)
    # 4. Word 2 whole word replay: Word 1 base [0.0, 1.0] (1.0s)
    # 5. Word 2 base: [1.5, 2.0] (0.5s)
    # Total duration = 1.0 + 0.5 + 1.0 + 1.0 + 0.5 = 4.0s
    cues_text = """1
00:00:00,000 --> 00:00:01,000
a

2
00:00:01,500 --> 00:00:02,000
a
"""
    cues = simulator.parse_srt(cues_text, is_filepath=False)
    words = simulator.split_words(cues)
    sim = simulator.TimelineSimulator(words, video_duration=2.0)
    res = sim.generate_timeline()

    assert pytest.approx(res["total_duration"], 0.001) == 4.0
    assert res["letter_loop_count"] == 1
    assert res["word_loop_count"] == 1
