"""
Belawril Timeline Simulator and Subtitle Parser.
Handles SRT parsing, word splitting, letter-loop and whole-word loop event calculations,
and timeline duration/event counting.
"""

import re
import math


TIME_RE = re.compile(r"((?:\d+:)?\d+:\d+[,.]\d+)\s*-->\s*((?:\d+:)?\d+:\d+[,.]\d+)")


def to_sec(s):
    """Convert timestamp string (HH:MM:SS,mmm or MM:SS,mmm) to seconds."""
    t = 0.0
    for part in s.replace(",", ".").split(":"):
        t = t * 60 + float(part)
    return t


def fmt_srt(t):
    """Format seconds into SRT timestamp HH:MM:SS,mmm."""
    ms = int(round(t * 1000))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return "%02d:%02d:%02d,%03d" % (h, m, s, ms)


def parse_srt(path_or_text, is_filepath=True):
    """Parse SRT content into a list of cue dicts with start, end, and text."""
    if is_filepath:
        text = open(path_or_text, encoding="utf-8-sig", errors="replace").read()
    else:
        text = path_or_text

    text = text.replace("\r\n", "\n").replace("\r", "\n")
    cues, auto = [], 0
    for block in re.split(r"\n\s*\n", text):
        lines = block.split("\n")
        ti = next((i for i, l in enumerate(lines) if TIME_RE.search(l)), -1)
        if ti < 0:
            continue
        m = TIME_RE.search(lines[ti])
        auto += 1
        prev = lines[ti - 1].strip() if ti > 0 else ""
        num = int(prev) if prev.isdigit() else auto
        body = "\n".join(lines[ti + 1:])
        body = re.sub(r"<[^>]+>", "", body)
        body = re.sub(r"\{\\[^}]*\}", "", body).strip()
        cues.append({"num": num, "start": to_sec(m.group(1)), "end": to_sec(m.group(2)), "text": body})
    cues.sort(key=lambda c: c["start"])
    return cues


def split_words(cues):
    """
    Split cues into word entries.
    If a cue contains multiple words, slice it proportionally by character length.
    """
    out = []
    for c in cues:
        toks = c["text"].split()
        if not toks or c["end"] <= c["start"]:
            continue
        weights = [max(1, len(t)) for t in toks]
        total, cursor = float(sum(weights)), c["start"]
        dur = c["end"] - c["start"]
        for i, (t, w) in enumerate(zip(toks, weights)):
            end = c["end"] if i == len(toks) - 1 else cursor + dur * w / total
            out.append({"tok": t, "start": cursor, "end": end, "cue": c["num"], "idx": i + 1, "words": len(toks)})
            cursor = end
    return out


def clean_word_for_matching(word_text):
    """
    Convert word to lowercase and strip all punctuation EXCEPT apostrophes (standard ' and unicode ’/‘).
    """
    cleaned = re.sub(r"[^\w'\u2019\u2018]|_", "", word_text.lower(), flags=re.UNICODE)
    return cleaned


def clean_letter(c):
    """Return lowercase char if it is a letter/digit or apostrophe, else empty."""
    c_lower = c.lower()
    if re.match(r"[\w'\u2019\u2018]", c_lower) and c_lower != "_":
        return c_lower
    return ""


def get_letter_time_slices(word_entry):
    """
    Given a word entry with start, end and tok,
    divide [start, end] among all valid letters (letters/digits/apostrophes).
    Returns list of dicts: [{'char': c_clean, 'start': s, 'end': e, 'word_idx': idx}, ...]
    """
    tok = word_entry["tok"]
    start = word_entry["start"]
    end = word_entry["end"]
    dur = end - start

    # Filter out valid characters for matching
    valid_chars = [c for c in tok if clean_letter(c)]
    if not valid_chars or dur <= 0:
        return []

    slice_dur = dur / len(valid_chars)
    slices = []
    for i, c in enumerate(valid_chars):
        c_clean = clean_letter(c)
        s = start + i * slice_dur
        e = start + (i + 1) * slice_dur if i < len(valid_chars) - 1 else end
        slices.append({
            "char": c_clean,
            "start": s,
            "end": e,
            "word_tok": tok,
            "word_clean": clean_word_for_matching(tok),
            "word_start": start,
            "word_end": end,
        })
    return slices


class TimelineSimulator:
    """
    Simulates Belawril loop rules and generates timeline segment clips.

    Rules:
    - Input: Video and SRT file (or parsed word list).
    - Exact SRT start/end times preserved.
    - Letter loop:
      - Case insensitive matching.
      - Strips all punctuation EXCEPT apostrophes.
      - For each letter occurrence in a word, find its most recent previous occurrence in the preceding subtitle sequence.
      - Duplicate letters in same word (e.g., second 'o' in "look") trigger repeat to the same previous occurrence that the first 'o' went to.
      - Letter loops occur immediately before the word itself.
    - Whole-word loop:
      - Occurs if exact word appeared previously in the subtitle sequence (most recent previous occurrence).
      - Occurs immediately before the word itself, after the word's letter loops.
      - Replaying W_prev includes W_prev's letter loops followed by W_prev's base clip.
      - Whole-word loops MUST NOT recursively trigger additional whole-word loops within replays.
      - Letter loops remain active inside whole-word replays.
    - Gaps/intro/ending:
      - Preserved once in original timeline order.
    """

    def __init__(self, words, video_duration=None):
        self.words = words
        self.video_duration = video_duration if video_duration is not None else (words[-1]["end"] if words else 0.0)

    def generate_timeline(self):
        """
        Builds timeline segments.
        Each segment is a dict:
          {'type': 'gap'|'word_base'|'letter_loop'|'word_loop',
           'start': float, 'end': float, 'label': str}

        Also computes total stats:
          'letter_loop_count': int
          'word_loop_count': int
          'total_duration': float
        """
        segments = []
        letter_history = {}  # char -> list of letter_slice dicts
        word_history = {}    # clean_word -> list of dicts with word_entry & letter_slices

        def build_word_replay_segments(target_word_entry, target_letter_slices):
            """
            Replays target_word_entry.
            Per Belawril rules: Replaying W_prev includes W_prev's letter loops followed by W_prev's base clip,
            but suppresses any nested whole-word loops (no recursion).
            """
            replay_segs = []
            # 1. Target word's letter loops (using history at the time target_word occurred)
            for l_slice in target_letter_slices:
                ch = l_slice["char"]
                # Previous occurrences before target_word_entry
                prev_occurrences = [
                    hist_slice for hist_slice in letter_history.get(ch, [])
                    if hist_slice["word_start"] < target_word_entry["start"]
                ]
                if prev_occurrences:
                    most_recent = prev_occurrences[-1]
                    replay_segs.append({
                        "type": "letter_loop",
                        "start": most_recent["start"],
                        "end": most_recent["end"],
                        "label": f"Letter loop '{ch}' ({target_word_entry['tok']})"
                    })
            # 2. Target word's base clip
            replay_segs.append({
                "type": "word_base",
                "start": target_word_entry["start"],
                "end": target_word_entry["end"],
                "label": target_word_entry["tok"]
            })
            return replay_segs

        pos = 0.0
        letter_loop_count = 0
        word_loop_count = 0

        for w in self.words:
            w_clean = clean_word_for_matching(w["tok"])
            w_start = w["start"]
            w_end = w["end"]

            # Preserve gap before word
            if w_start > pos:
                segments.append({
                    "type": "gap",
                    "start": pos,
                    "end": w_start,
                    "label": "gap"
                })

            w_slices = get_letter_time_slices(w)

            # 1. Letter loops for current word
            word_letter_loop_segs = []
            for l_slice in w_slices:
                ch = l_slice["char"]
                # Find most recent previous occurrence in preceding subtitle sequence (before w_start)
                prev_occurrences = [
                    hist_slice for hist_slice in letter_history.get(ch, [])
                    if hist_slice["word_start"] < w_start
                ]
                if prev_occurrences:
                    most_recent = prev_occurrences[-1]
                    word_letter_loop_segs.append({
                        "type": "letter_loop",
                        "start": most_recent["start"],
                        "end": most_recent["end"],
                        "label": f"Letter loop '{ch}' ({w['tok']})"
                    })
                    letter_loop_count += 1

            # Append letter loops immediately before the word
            segments.extend(word_letter_loop_segs)

            # 2. Whole-word loop for current word
            if w_clean and w_clean in word_history and word_history[w_clean]:
                prev_w_info = word_history[w_clean][-1]
                prev_w_entry = prev_w_info["word_entry"]
                prev_w_slices = prev_w_info["letter_slices"]

                # Replay W_prev (letter loops + base clip, no nested whole-word loop)
                replay_segs = build_word_replay_segments(prev_w_entry, prev_w_slices)
                # Count letter loops triggered inside whole-word replay
                for r_seg in replay_segs:
                    if r_seg["type"] == "letter_loop":
                        letter_loop_count += 1

                segments.extend(replay_segs)
                word_loop_count += 1

            # 3. Base word clip
            segments.append({
                "type": "word_base",
                "start": w_start,
                "end": w_end,
                "label": w["tok"]
            })

            # Record in history after processing
            for l_slice in w_slices:
                ch = l_slice["char"]
                if ch not in letter_history:
                    letter_history[ch] = []
                letter_history[ch].append(l_slice)

            if w_clean:
                if w_clean not in word_history:
                    word_history[w_clean] = []
                word_history[w_clean].append({
                    "word_entry": w,
                    "letter_slices": w_slices
                })

            pos = w_end

        # Preserve remaining gap/ending
        if self.video_duration > pos:
            segments.append({
                "type": "gap",
                "start": pos,
                "end": self.video_duration,
                "label": "gap"
            })

        total_dur = sum(s["end"] - s["start"] for s in segments)

        return {
            "segments": segments,
            "letter_loop_count": letter_loop_count,
            "word_loop_count": word_loop_count,
            "total_duration": total_dur,
        }
