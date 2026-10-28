"""
Betalirilbids Timeline Simulator and Subtitle Parser.
Handles SRT parsing, word splitting, letter-loop calculations (letter loops play before word base clip, natural gaps preserved),
and timeline duration/event counting with structured event logging.
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
    entry_counter = 1
    for c in cues:
        toks = c["text"].split()
        if not toks or c["end"] <= c["start"]:
            continue
        weights = [max(1, len(t)) for t in toks]
        total, cursor = float(sum(weights)), c["start"]
        dur = c["end"] - c["start"]
        for i, (t, w) in enumerate(zip(toks, weights)):
            end = c["end"] if i == len(toks) - 1 else cursor + dur * w / total
            out.append({
                "entry_idx": entry_counter,
                "tok": t,
                "start": cursor,
                "end": end,
                "cue": c["num"],
                "idx": i + 1,
                "words": len(toks)
            })
            entry_counter += 1
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


def get_word_letters(word_entry):
    """Returns list of clean characters for the word entry."""
    tok = word_entry["tok"]
    return [clean_letter(c) for c in tok if clean_letter(c)]


class TimelineSimulator:
    """
    Simulates Betalirilbids Letter Loop rules and generates timeline segment clips.

    Rules:
    - Input: Video and SRT subtitle entries (one word per entry).
    - Preserves exact SRT start and end times.
    - Letter Loop (triggers BEFORE word base clip):
      - Case-insensitive, stripping punctuation except apostrophes.
      - Process letters of each word left-to-right independently.
      - For each letter occurrence in current word W_curr (start S_curr), search backwards through earlier original subtitle words (W_prev < W_curr)
        for the most recent word containing that letter (start S_prev).
      - Replays continuously through original video timeline from S_prev up to S_curr ([S_prev, S_curr]).
      - Letter loops do NOT trigger nested letter loops.
    - Word Base Clip:
      - Played after W_curr's letter loops, carrying playback forward from S_curr to E_curr.
    - Natural Gaps:
      - Gaps between words [E_curr, S_next] play naturally before W_next's letter loops trigger.
    """

    def __init__(self, words, video_duration=None):
        self.words = words
        self.video_duration = video_duration if video_duration is not None else (words[-1]["end"] if words else 0.0)

    def generate_timeline(self):
        """
        Builds timeline segments and detailed event log.
        Returns dict with:
          'segments': list of segment dicts
          'event_log': list of event log dicts
          'letter_loop_count': int
          'word_loop_count': int (always 0)
          'total_duration': float
        """
        segments = []
        event_log = []

        letter_word_history = {}  # char -> list of original word dicts containing char
        out_cursor = 0.0
        pos = 0.0
        letter_loop_count = 0

        for w in self.words:
            w_start = w["start"]
            w_end = w["end"]
            entry_idx = w.get("entry_idx", 0)

            # Preserve natural gap before word
            if w_start > pos:
                gap_dur = w_start - pos
                segments.append({
                    "type": "gap",
                    "start": pos,
                    "end": w_start,
                    "label": "gap",
                    "is_replay": False
                })
                event_log.append({
                    "source_word": "gap",
                    "source_entry": 0,
                    "event_type": "gap",
                    "match": None,
                    "target_entry": 0,
                    "target_word": None,
                    "target_start": pos,
                    "target_end": w_start,
                    "output_start": out_cursor,
                    "output_end": out_cursor + gap_dur,
                    "is_replay": False
                })
                out_cursor += gap_dur

            w_chars = get_word_letters(w)

            # 1. Letter loops trigger for current word BEFORE its base clip
            for ch in w_chars:
                prev_occurrences = [
                    hist_word for hist_word in letter_word_history.get(ch, [])
                    if hist_word["start"] < w_start
                ]
                if prev_occurrences:
                    most_recent_word = prev_occurrences[-1]
                    s_prev = most_recent_word["start"]
                    s_curr = w_start
                    dur = s_curr - s_prev
                    if dur > 0:
                        segments.append({
                            "type": "letter_loop",
                            "start": s_prev,
                            "end": s_curr,
                            "label": f"Letter loop '{ch}' -> '{most_recent_word['tok']}' ({w['tok']})",
                            "is_replay": True
                        })
                        event_log.append({
                            "source_word": w["tok"],
                            "source_entry": entry_idx,
                            "event_type": "letter_loop",
                            "match": f"Letter '{ch}' -> word '{most_recent_word['tok']}'",
                            "target_entry": most_recent_word.get("entry_idx", 0),
                            "target_word": most_recent_word["tok"],
                            "target_start": s_prev,
                            "target_end": s_curr,
                            "output_start": out_cursor,
                            "output_end": out_cursor + dur,
                            "is_replay": True
                        })
                        out_cursor += dur
                        letter_loop_count += 1

            # 2. Base Word Clip plays after letter loops
            word_dur = w_end - w_start
            segments.append({
                "type": "word_base",
                "start": w_start,
                "end": w_end,
                "label": w["tok"],
                "is_replay": False
            })
            event_log.append({
                "source_word": w["tok"],
                "source_entry": entry_idx,
                "event_type": "word_base",
                "match": None,
                "target_entry": entry_idx,
                "target_word": w["tok"],
                "target_start": w_start,
                "target_end": w_end,
                "output_start": out_cursor,
                "output_end": out_cursor + word_dur,
                "is_replay": False
            })
            out_cursor += word_dur

            # Record in history after processing current word
            unique_chars_in_w = set(w_chars)
            for ch in unique_chars_in_w:
                if ch not in letter_word_history:
                    letter_word_history[ch] = []
                letter_word_history[ch].append(w)

            pos = w_end

        # Preserve remaining gap/ending
        if self.video_duration > pos:
            gap_dur = self.video_duration - pos
            segments.append({
                "type": "gap",
                "start": pos,
                "end": self.video_duration,
                "label": "gap",
                "is_replay": False
            })
            event_log.append({
                "source_word": "gap",
                "source_entry": 0,
                "event_type": "gap",
                "match": None,
                "target_entry": 0,
                "target_word": None,
                "target_start": pos,
                "target_end": self.video_duration,
                "output_start": out_cursor,
                "output_end": out_cursor + gap_dur,
                "is_replay": False
            })
            out_cursor += gap_dur

        total_dur = sum(s["end"] - s["start"] for s in segments)

        return {
            "segments": segments,
            "event_log": event_log,
            "letter_loop_count": letter_loop_count,
            "word_loop_count": 0,
            "total_duration": total_dur,
        }

    def format_event_log_text(self, sim_res=None):
        """Returns readable formatted text string of the event log."""
        if sim_res is None:
            sim_res = self.generate_timeline()

        lines = [
            f"BETALIRILBIDS EVENT LOG (Total Duration: {sim_res['total_duration']:.3f}s | "
            f"Letter Loops: {sim_res['letter_loop_count']})",
            "=" * 90,
            f"{'Out Time Range':<20} | {'Type':<18} | {'Source Word':<12} | {'Match / Target':<25} | {'Source Cut Range'}",
            "-" * 90
        ]
        for e in sim_res["event_log"]:
            out_time = f"{e['output_start']:.2f}s - {e['output_end']:.2f}s"
            cut_time = f"{e['target_start']:.2f}s - {e['target_end']:.2f}s"
            match_info = e['match'] or f"Entry {e['target_entry']} '{e['target_word'] or ''}'"
            rep_flag = " [loop]" if e['is_replay'] else ""
            lines.append(
                f"{out_time:<20} | {e['event_type'] + rep_flag:<18} | {e['source_word']:<12} | {match_info:<25} | {cut_time}"
            )
        return "\n".join(lines)
