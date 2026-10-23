#!/usr/bin/env python3
"""
repeat_video.py - repeat each subtitle word's video clip based on its character length.

Usage:
  python repeat_video.py video.mp4 words.srt -f "len^2" -o output.mp4

Needs ffmpeg and ffprobe on your PATH (https://ffmpeg.org/download.html).

Speed: the default "fast mode" encodes the video once and builds the result by repeating
already-compressed data, so it takes seconds to a minute or two instead of many minutes.
Add --compress if you'd rather have a smaller file and don't mind waiting.

Parts of the video that no word covers (intro, gaps between words, instrumental
sections, the ending) are kept and played once, in their original order.
Cutting is frame-exact, so picture and sound stay in sync however often a clip repeats.

Each .srt cue is treated as a word with its own start/end time. The video clip
for that word is repeated N times, where N comes from your formula. Cues that
contain several words are split into one slice per word, sized by letter count.

Formula variables : len (or n), idx, words, cue, pi, e
Operators         : + - * / % ^ (or **)  !  ( )   and  2len  style multiplication
Functions         : floor ceil round trunc abs sqrt pow min max log ln log2 fact sign
"""
import argparse, math, mmap, os, re, shutil, struct, subprocess, sys, tempfile, threading, time
from concurrent.futures import ThreadPoolExecutor
from fractions import Fraction

# ------------------------------------------------------------------ formula
INF = float("inf")

def _pow(a, b):
    try:
        return math.pow(a, b)
    except OverflowError:
        return INF
    except ValueError:
        return float("nan")

def _div(a, b):
    if b == 0:
        return float("nan") if a == 0 else math.copysign(INF, a)
    return a / b

def _mod(a, b):
    return float("nan") if b == 0 else math.fmod(a, b)

def _fact(x):
    if x < 0 or x != math.floor(x):
        return float("nan")
    if x > 170:
        return INF
    return float(math.factorial(int(x)))

def _log(f):
    def g(x):
        return float("nan") if x <= 0 else f(x)
    return g

FUNCS = {
    "floor": math.floor, "ceil": math.ceil, "trunc": math.trunc, "abs": abs,
    "round": lambda x: math.floor(x + 0.5), "sqrt": lambda x: float("nan") if x < 0 else math.sqrt(x),
    "pow": _pow, "min": min, "max": max, "log": _log(math.log10), "ln": _log(math.log),
    "log2": _log(math.log2), "fact": _fact,
    "sign": lambda x: (x > 0) - (x < 0),
}
VARS = {"len", "n", "idx", "words", "cue", "pi", "e"}

def tokenize(s):
    toks, i = [], 0
    num = re.compile(r"(\d+\.?\d*|\.\d+)(e[+-]?\d+)?", re.I)
    ident = re.compile(r"[a-z_][a-z0-9_]*", re.I)
    while i < len(s):
        c = s[i]
        if c.isspace():
            i += 1
        elif c.isdigit() or c == ".":
            m = num.match(s, i)
            if not m:
                raise ValueError("Bad number near %r" % s[i:i + 4])
            toks.append(("num", float(m.group(0)))); i = m.end()
        elif c.isalpha() or c == "_":
            m = ident.match(s, i)
            toks.append(("id", m.group(0).lower())); i = m.end()
        elif s.startswith("**", i):
            toks.append(("op", "^")); i += 2
        elif c in "+-*/%^(),!":
            toks.append(("op", c)); i += 1
        elif c == "\u00d7":
            toks.append(("op", "*")); i += 1
        elif c == "\u00f7":
            toks.append(("op", "/")); i += 1
        else:
            raise ValueError("Unexpected %r" % c)
    return toks

def compile_formula(src):
    toks = tokenize(src)
    if not toks:
        raise ValueError("Empty formula")
    pos = [0]

    def peek():
        return toks[pos[0]] if pos[0] < len(toks) else None

    def is_op(v):
        t = peek()
        return t is not None and t[0] == "op" and t[1] == v

    def expr():
        left = term()
        while is_op("+") or is_op("-"):
            op = toks[pos[0]][1]; pos[0] += 1
            right, a = term(), left
            left = (lambda a, r: lambda e: a(e) + r(e))(a, right) if op == "+" else \
                   (lambda a, r: lambda e: a(e) - r(e))(a, right)
        return left

    def term():
        left = unary()
        while True:
            t = peek()
            if t is None:
                break
            if t[0] == "op" and t[1] in "*/%":
                pos[0] += 1
                right, a = unary(), left
                if t[1] == "*":
                    left = (lambda a, r: lambda e: a(e) * r(e))(a, right)
                elif t[1] == "/":
                    left = (lambda a, r: lambda e: _div(a(e), r(e)))(a, right)
                else:
                    left = (lambda a, r: lambda e: _mod(a(e), r(e)))(a, right)
            elif t[0] in ("num", "id") or (t[0] == "op" and t[1] == "("):
                right, a = unary(), left
                left = (lambda a, r: lambda e: a(e) * r(e))(a, right)
            else:
                break
        return left

    def unary():
        if is_op("-"):
            pos[0] += 1; a = unary(); return lambda e: -a(e)
        if is_op("+"):
            pos[0] += 1; return unary()
        return power()

    def power():
        base = postfix()
        if is_op("^"):
            pos[0] += 1; ex = unary()
            return lambda e: _pow(base(e), ex(e))
        return base

    def postfix():
        a = primary()
        while is_op("!"):
            pos[0] += 1; b = a; a = (lambda b: lambda e: _fact(b(e)))(b)
        return a

    def primary():
        if pos[0] >= len(toks):
            raise ValueError("Formula ends too early")
        kind, val = toks[pos[0]]; pos[0] += 1
        if kind == "num":
            return lambda e: val
        if kind == "id":
            if is_op("("):
                if val not in FUNCS:
                    raise ValueError("Unknown function %r" % val)
                pos[0] += 1; args = []
                if not is_op(")"):
                    args.append(expr())
                    while is_op(","):
                        pos[0] += 1; args.append(expr())
                if not is_op(")"):
                    raise ValueError("Missing ')'")
                pos[0] += 1
                f = FUNCS[val]
                return lambda e: f(*[a(e) for a in args])
            if val not in VARS:
                raise ValueError("Unknown name %r" % val)
            if val == "pi":
                return lambda e: math.pi
            if val == "e":
                return lambda e: math.e
            key = "len" if val == "n" else val
            return lambda e: e[key]
        if val == "(":
            a = expr()
            if not is_op(")"):
                raise ValueError("Missing ')'")
            pos[0] += 1
            return a
        raise ValueError("Unexpected %r" % val)

    f = expr()
    if pos[0] < len(toks):
        raise ValueError("Unexpected %r" % (toks[pos[0]][1],))
    return f

# ---------------------------------------------------------------------- srt
TIME_RE = re.compile(r"((?:\d+:)?\d+:\d+[,.]\d+)\s*-->\s*((?:\d+:)?\d+:\d+[,.]\d+)")

def to_sec(s):
    t = 0.0
    for part in s.replace(",", ".").split(":"):
        t = t * 60 + float(part)
    return t

def fmt_srt(t):
    ms = int(round(t * 1000))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return "%02d:%02d:%02d,%03d" % (h, m, s, ms)

def parse_srt(path):
    text = open(path, encoding="utf-8-sig", errors="replace").read().replace("\r\n", "\n").replace("\r", "\n")
    cues, auto = [], 0
    for block in re.split(r"\n\s*\n", text):
        lines = block.split("\n")
        ti = next((i for i, l in enumerate(lines) if TIME_RE.search(l)), -1)
        if ti < 0:
            continue
        m = TIME_RE.search(lines[ti]); auto += 1
        prev = lines[ti - 1].strip() if ti > 0 else ""
        num = int(prev) if prev.isdigit() else auto
        body = "\n".join(lines[ti + 1:])
        body = re.sub(r"<[^>]+>", "", body)
        body = re.sub(r"\{\\[^}]*\}", "", body).strip()
        cues.append({"num": num, "start": to_sec(m.group(1)), "end": to_sec(m.group(2)), "text": body})
    cues.sort(key=lambda c: c["start"])
    return cues

def split_words(cues):
    """One entry per word. A multi-word cue is sliced by character count."""
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

# ----------------------------------------------------------------- ffmpeg
# On Windows, don't pop up a console window for every ffmpeg call (matters for the desktop app).
_NOWIN = {"creationflags": 0x08000000} if os.name == "nt" else {}
_active, _cancelled = [], False

def cancel():
    """Stop the current job (used by the desktop app's Cancel button)."""
    global _cancelled
    _cancelled = True
    for p in list(_active):
        try:
            p.kill()
        except Exception:
            pass

def _check_cancel():
    if _cancelled:
        sys.exit("Cancelled.")

def run(cmd, quiet=True):
    _check_cancel()
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                         encoding="utf-8", errors="replace", **_NOWIN)
    _active.append(p)
    try:
        out, err = p.communicate()
    finally:
        if p in _active:
            _active.remove(p)
    _check_cancel()
    if p.returncode != 0:
        sys.exit("ffmpeg failed:\n" + " ".join(cmd) + "\n" + err[-1500:])
    return out

def probe(video):
    out = run(["ffprobe", "-v", "error", "-show_entries",
               "stream=codec_type,r_frame_rate:format=duration", "-of", "default=nw=1", video])
    fps, has_audio, dur = "30", False, 0.0
    cur = None
    for line in out.splitlines():
        k, _, v = line.partition("=")
        if k == "codec_type":
            cur = v
            if v == "audio":
                has_audio = True
        elif k == "r_frame_rate" and cur == "video":
            fps = v
        elif k == "duration":
            dur = float(v)
    num, _, den = fps.partition("/")
    fpsf = float(num) / float(den or 1) if float(den or 1) else 30.0
    return fpsf, has_audio, dur

def source_video_bitrate(video):
    """Bits per second of the source video stream (None if it can't be worked out)."""
    def q(args):
        try:
            r = subprocess.run(["ffprobe", "-v", "error"] + args + [video], stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, text=True, **_NOWIN)
            return int(r.stdout.strip().splitlines()[0])
        except Exception:
            return None
    br = q(["-select_streams", "v:0", "-show_entries", "stream=bit_rate", "-of", "default=nw=1:nk=1"])
    if br is None:
        total = q(["-show_entries", "format=bit_rate", "-of", "default=nw=1:nk=1"])
        br = total - 192000 if total and total > 400000 else total
    return br

# ---------------------------------------------------------------- progress
def fmt_dur(sec):
    sec = int(round(max(0, sec)))
    h, rem = divmod(sec, 3600)
    m, s_ = divmod(rem, 60)
    return "%d:%02d:%02d" % (h, m, s_) if h else "%02d:%02d" % (m, s_)

def show_progress(label, frac, t0):
    frac = max(0.0, min(1.0, frac))
    elapsed = time.time() - t0
    if frac >= 1.0:
        eta = "done"
    elif frac >= 0.01:
        eta = "ETA " + fmt_dur(elapsed * (1 - frac) / frac)
    else:
        eta = "ETA estimating..."
    width = 26
    filled = int(width * frac)
    line = "%s [%s%s] %5.1f%% | elapsed %s | %s" % (label, "#" * filled, "-" * (width - filled),
                                                    frac * 100, fmt_dur(elapsed), eta)
    sys.stdout.write("\r" + line.ljust(110))
    sys.stdout.flush()

PROGRESS_LINE = re.compile(r"^[A-Za-z0-9_]+=\S*$")

def run_progress(cmd, total_sec, label, cwd=None, hint=""):
    """Run ffmpeg and draw a live progress bar with an estimated time remaining."""
    full = [cmd[0], "-progress", "pipe:1", "-nostats"] + cmd[1:]
    _check_cancel()
    p = subprocess.Popen(full, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                         encoding="utf-8", errors="replace", cwd=cwd, **_NOWIN)
    _active.append(p)
    t0, tail = time.time(), []
    show_progress(label, 0.0, t0)
    for line in p.stdout:
        line = line.strip()
        if PROGRESS_LINE.match(line):
            k, _, v = line.partition("=")
            if k in ("out_time_us", "out_time_ms"):      # both are in microseconds
                try:
                    show_progress(label, (int(v) / 1e6) / max(total_sec, 0.001), t0)
                except ValueError:
                    pass
        elif line:
            tail = (tail + [line])[-30:]
    rc = p.wait()
    if p in _active:
        _active.remove(p)
    if _cancelled:
        sys.stdout.write("\n")
        sys.exit("Cancelled.")
    if rc != 0:
        sys.stdout.write("\n")
        sys.exit("ffmpeg failed %s\n%s" % (hint, "\n".join(tail)))
    show_progress(label, 1.0, t0)
    sys.stdout.write("\n")

# ------------------------------------------------------------- fast renderer
# Idea: encode the video ONCE (cuts forced to land exactly on word boundaries), then build the
# result by repeating the already-compressed bytes of each word. No per-clip ffmpeg runs, no
# per-repeat file opening, no second encode. Audio is encoded once from raw samples.
class FastPathError(Exception):
    pass

_START_RE = re.compile(b"\x00\x00\x01")

def _index_h264(path, want):
    """Scan an Annex-B H.264 file. Returns (picture count, {picture: (byte offset, is_idr)}, size)."""
    size = os.path.getsize(path)
    if size == 0:
        raise FastPathError("encoder produced an empty file")
    starts, frame, pending = {}, -1, None
    with open(path, "rb") as fh:
        mm = mmap.mmap(fh.fileno(), 0, access=mmap.ACCESS_READ)
        try:
            for m in _START_RE.finditer(mm):
                p = m.start()
                if p > 0 and mm[p - 1] == 0:
                    p -= 1                                  # 4-byte start code
                e = m.end()
                if e >= size:
                    break
                t = mm[e] & 0x1F
                if t in (1, 5):
                    if e + 1 < size and (mm[e + 1] & 0x80):   # first_mb_in_slice == 0: a new picture
                        frame += 1
                        if frame in want:
                            starts[frame] = (pending if pending is not None else p, t == 5)
                        pending = None
                elif pending is None:
                    pending = p                              # SPS / PPS / SEI belong to the next picture
        finally:
            mm.close()
    return frame + 1, starts, size

def count_video_frames(video):
    """Exact number of video frames (counted from the file's packets; fast, no decoding)."""
    try:
        r = subprocess.run(["ffprobe", "-v", "error", "-count_packets", "-select_streams", "v:0",
                            "-show_entries", "stream=nb_read_packets", "-of", "default=nw=1:nk=1", video],
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, **_NOWIN)
        return int(r.stdout.strip().splitlines()[0])
    except Exception:
        return None

def source_audio_rate(video):
    try:
        r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries",
                            "stream=sample_rate", "-of", "default=nw=1:nk=1", video],
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, **_NOWIN)
        return int(r.stdout.strip().splitlines()[0])
    except Exception:
        return 48000

def _abort_active():
    for p in list(_active):
        try:
            p.kill()
        except Exception:
            pass

def _write_repeated(write, blob, n, step_bytes=8 << 20):
    """write(blob) n times, in big batches so tiny words with huge repeat counts stay fast."""
    if n <= 0 or not blob:
        return
    reps = max(1, step_bytes // len(blob))
    big = blob * reps
    full, rem = divmod(n, reps)
    for _ in range(full):
        write(big)
    if rem:
        write(blob * rem)

def _encode_chunks(a, segs, fps, total_frames, tmp, crf, preset="ultrafast"):
    """Encode the whole video once, with a keyframe exactly on every cut point."""
    fr = Fraction(fps).limit_denominator(1001)
    fps_str = "%d/%d" % (fr.numerator, fr.denominator)
    fps_f = float(fr)

    # 1. every place the video must be cut
    bset = {0, total_frames}
    for sf, ef, n, lab in segs:
        bset.add(sf)
        bset.add(ef)
    bounds = sorted(b for b in bset if 0 <= b <= total_frames)
    chunks, cur = [], [bounds[0]]
    for b in bounds[1:]:
        cur.append(b)
        if len(cur) > 400 or (b - cur[0]) > 3600:
            chunks.append(cur)
            cur = [b]
    if len(cur) > 1:
        chunks.append(cur)
    if not chunks:
        raise FastPathError("nothing to encode")

    # 2. encode the video in parallel chunks, with a keyframe forced exactly on every cut point
    paths = [os.path.join(tmp, "chunk_%04d.h264" % i) for i in range(len(chunks))]
    done = [0] * len(chunks)

    def enc(i):
        c = chunks[i]
        b0, b1 = c[0], c[-1]
        cmd = ["ffmpeg", "-progress", "pipe:1", "-nostats", "-y", "-v", "error"]
        if b0 > 0:
            cmd += ["-ss", "%.6f" % ((b0 - 0.25) / fps_f)]
        cmd += ["-i", a.video, "-an", "-frames:v", str(b1 - b0), "-r", fps_str,
                "-c:v", "libx264", "-preset", preset, "-crf", str(crf), "-pix_fmt", "yuv420p", "-bf", "0"]
        inner = ["%.6f" % ((b - b0 - 0.5) / fps_f) for b in c[1:-1]]
        if inner:
            cmd += ["-force_key_frames", ",".join(inner)]
        cmd += ["-f", "h264", paths[i]]
        _check_cancel()
        pr = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                              encoding="utf-8", errors="replace", **_NOWIN)
        _active.append(pr)
        tail = []
        for line in pr.stdout:
            line = line.strip()
            if PROGRESS_LINE.match(line):
                k, _, v = line.partition("=")
                if k in ("out_time_us", "out_time_ms"):
                    try:
                        done[i] = min(b1 - b0, int(int(v) / 1e6 * fps_f))
                    except ValueError:
                        pass
            elif line:
                tail = (tail + [line])[-10:]
        rc = pr.wait()
        if pr in _active:
            _active.remove(pr)
        _check_cancel()
        if rc != 0:
            raise FastPathError("video encoder failed: " + " ".join(tail)[-300:])
        done[i] = b1 - b0

    t0 = time.time()
    workers = max(1, min(len(chunks), os.cpu_count() or 2))
    ex = ThreadPoolExecutor(max_workers=workers)
    futs = [ex.submit(enc, i) for i in range(len(chunks))]
    try:
        while True:
            show_progress("Encoding video", sum(done) / float(max(1, total_frames)), t0)
            for f in futs:
                if f.done() and f.exception() is not None:
                    raise f.exception()
            if all(f.done() for f in futs):
                break
            time.sleep(0.15)
    except BaseException:
        for f in futs:
            f.cancel()
        _abort_active()
        ex.shutdown(wait=True)
        sys.stdout.write("\n")
        raise
    ex.shutdown(wait=True)
    show_progress("Encoding video", 1.0, t0)
    sys.stdout.write("\n")

    return fr, fps_str, fps_f, chunks, paths


def fast_render(a, segs, fps, has_audio, total_frames, tmp, dest, crf):
    fr, fps_str, fps_f, chunks, paths = _encode_chunks(a, segs, fps, total_frames, tmp, crf)

    # 3. find the exact byte positions of every cut point, and check each one is a clean keyframe
    index = []
    for i, c in enumerate(chunks):
        b0 = c[0]
        n_pics, starts, size = _index_h264(paths[i], {b - b0 for b in c[:-1]})
        if n_pics != c[-1] - b0:
            raise FastPathError("frame count mismatch (%d, expected %d)" % (n_pics, c[-1] - b0))
        for b in c[:-1]:
            st = starts.get(b - b0)
            if st is None or not st[1]:
                raise FastPathError("a cut point was not a keyframe")
        index.append((starts, size))
    chunk_of = {}
    for i, c in enumerate(chunks):
        for b in c[:-1]:
            chunk_of[b] = i

    # 4. audio: one raw decode, then repeat sample ranges and encode once
    audio_path = None
    if has_audio:
        rate = source_audio_rate(a.video)
        raw = os.path.join(tmp, "audio.raw")
        run(["ffmpeg", "-y", "-v", "error", "-i", a.video, "-vn", "-af", "aresample=async=1:first_pts=0",
             "-ac", "2", "-ar", str(rate), "-f", "s16le", raw])
        audio_path = os.path.join(tmp, "audio.m4a")
        errf = open(os.path.join(tmp, "audio_err.txt"), "w")
        ap_ = subprocess.Popen(["ffmpeg", "-y", "-v", "error", "-f", "s16le", "-ar", str(rate), "-ac", "2",
                                "-i", "pipe:0", "-c:a", "aac", "-b:a", a.audio_bitrate, audio_path],
                               stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=errf, **_NOWIN)
        _active.append(ap_)
        total_samples = sum(int(round(Fraction(ef - sf) * rate / fr)) * n for sf, ef, n, _ in segs) or 1
        written, t1, last = [0], time.time(), [0.0]

        def awrite(b):
            ap_.stdin.write(b)
            written[0] += len(b) // 4
            if time.time() - last[0] > 0.1:            # don't flood the screen / the app with updates
                last[0] = time.time()
                show_progress("Building result (audio)", written[0] / float(total_samples), t1)
        try:
            with open(raw, "rb") as rf:
                for sf, ef, n, _ in segs:
                    lx = Fraction(ef - sf) * rate / fr                 # samples per repeat, exactly
                    rnd = lambda q: int(math.floor(q + Fraction(1, 2)))
                    s0 = rnd(Fraction(sf) * rate / fr)
                    period = lx.denominator                            # rounding pattern repeats this often
                    lens = [rnd(lx * (k + 1)) - rnd(lx * k) for k in range(period)]
                    rf.seek(s0 * 4)
                    base = rf.read(max(lens) * 4)
                    base += b"\x00" * (max(lens) * 4 - len(base))
                    block = b"".join(base[:l * 4] for l in lens)
                    full, rem = divmod(n, period)
                    _write_repeated(awrite, block, full)
                    if rem:
                        awrite(b"".join(base[:l * 4] for l in lens[:rem]))
            ap_.stdin.close()
        except (BrokenPipeError, OSError):
            pass
        rc = ap_.wait()
        if ap_ in _active:
            _active.remove(ap_)
        errf.close()
        _check_cancel()
        if rc != 0:
            raise FastPathError("audio encoder failed")
        sys.stdout.write("\n")

    # 5. build the video by repeating compressed bytes straight into a muxer
    cmd = ["ffmpeg", "-y", "-v", "error", "-f", "h264", "-framerate", fps_str, "-i", "pipe:0"]
    if audio_path:
        cmd += ["-i", audio_path, "-map", "0:v:0", "-map", "1:a:0"]
    cmd += ["-c", "copy", dest]
    errf2 = open(os.path.join(tmp, "mux_err.txt"), "w")
    vp = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=errf2, **_NOWIN)
    _active.append(vp)
    out_frames, t2, last_v = [0], time.time(), [0.0]
    total_out = sum((ef - sf) * n for sf, ef, n, _ in segs) or 1
    handles = [open(pth, "rb") for pth in paths]
    maps = [mmap.mmap(h.fileno(), 0, access=mmap.ACCESS_READ) for h in handles]
    try:
        for sf, ef, n, _ in segs:
            i = chunk_of[sf]
            c = chunks[i]
            starts, size = index[i]
            start = starts[sf - c[0]][0]
            end = size if ef == c[-1] else starts[ef - c[0]][0]
            blob = maps[i][start:end]

            _write_repeated(vp.stdin.write, blob, n)
            out_frames[0] += (ef - sf) * n
            if time.time() - last_v[0] > 0.1:
                last_v[0] = time.time()
                show_progress("Building result (video)", out_frames[0] / float(total_out), t2)
        vp.stdin.close()
    except (BrokenPipeError, OSError):
        pass
    finally:
        for m in maps:
            m.close()
        for h in handles:
            h.close()
    rc = vp.wait()
    if vp in _active:
        _active.remove(vp)
    errf2.close()
    _check_cancel()
    if rc != 0:
        raise FastPathError("video assembly failed")
    show_progress("Building result (video)", 1.0, t2)
    sys.stdout.write("\n")

    # 6. sanity check: the result must be the length we planned
    expect = total_out / fps_f
    try:
        _, _, got = probe(dest)
    except SystemExit:
        raise FastPathError("couldn't read the result back")
    if abs(got - expect) > max(0.6, expect * 0.002):
        raise FastPathError("result is %.1fs, expected %.1fs" % (got, expect))


# ------------------------------------------------------------ tiny-file mode
# The video and sound are stored ONCE. The file's sample tables (the index that tells a player
# where each frame / sound packet lives) simply point at the same bytes again and again.
# The file stays about the size of the original no matter how long the result plays.
#
# Sound is normal AAC, so every player can play it. AAC can only be cut on 1024-sample packet
# boundaries, so each repeated word gets its own small, separately encoded sound clip. The picture
# holds its first and last frame for a few hundredths of a second per repeat so that picture and
# sound stay exactly in step.
class TinyError(Exception):
    pass

_TINY_RATE = 48000

def _mp4_box(typ, payload):
    size = 8 + len(payload)
    if size > 0xFFFFFFFF:
        return struct.pack(">I4sQ", 1, typ, 16 + len(payload)) + payload
    return struct.pack(">I4s", size, typ) + payload

def _mp4_boxes(b, off, end):
    while off + 8 <= end:
        size, typ = struct.unpack(">I4s", b[off:off + 8])
        hdr = 8
        if size == 1:
            size = struct.unpack(">Q", b[off + 8:off + 16])[0]
            hdr = 16
        elif size == 0:
            size = end - off
        if size < hdr:
            raise TinyError("damaged mp4 file")
        yield typ, off, size, hdr
        off += size

def _mp4_find(b, typ, off, end):
    for t, o, sz, h in _mp4_boxes(b, off, end):
        if t == typ:
            return o, sz, h
    raise TinyError("mp4 box %s not found" % typ.decode("latin1"))

def _be32(items):
    """Pack an iterable of ints as big-endian uint32 quickly."""
    from array import array
    arr = array("I", items)
    if arr.itemsize != 4:
        arr = array("L", items)
    if sys.byteorder == "little":
        arr.byteswap()
    return arr.tobytes()

def _parse_simple_mov(path, strict_timing=True):
    """Read a single-track mp4 written by ffmpeg whose media data is one contiguous block."""
    with open(path, "rb") as f:
        f.seek(0, 2)
        total = f.tell()
        ftyp, mdat, moov, pos = b"", None, None, 0
        while pos + 8 <= total:
            f.seek(pos)
            size, typ = struct.unpack(">I4s", f.read(8))
            hdr = 8
            if size == 1:
                size = struct.unpack(">Q", f.read(8))[0]
                hdr = 16
            elif size == 0:
                size = total - pos
            if typ == b"ftyp":
                f.seek(pos)
                ftyp = f.read(size)
            elif typ == b"mdat":
                mdat = (pos + hdr, size - hdr)
            elif typ == b"moov":
                f.seek(pos + hdr)
                moov = f.read(size - hdr)
            pos += size
    if moov is None or mdat is None:
        raise TinyError("couldn't read an intermediate file")
    t_o, t_s, t_h = _mp4_find(moov, b"trak", 0, len(moov))
    md = _mp4_find(moov, b"mdia", t_o + t_h, t_o + t_s)
    mdhd = _mp4_find(moov, b"mdhd", md[0] + md[2], md[0] + md[1])
    hdlr = _mp4_find(moov, b"hdlr", md[0] + md[2], md[0] + md[1])
    minf = _mp4_find(moov, b"minf", md[0] + md[2], md[0] + md[1])
    stbl = _mp4_find(moov, b"stbl", minf[0] + minf[2], minf[0] + minf[1])
    sb, se = stbl[0] + stbl[2], stbl[0] + stbl[1]
    tkhd = _mp4_find(moov, b"tkhd", t_o + t_h, t_o + t_s)
    mvhd = _mp4_find(moov, b"mvhd", 0, len(moov))

    p = mdhd[0] + mdhd[2]
    if moov[p] == 0:
        cre, mod, ts, _d = struct.unpack(">IIII", moov[p + 4:p + 20]); lang = moov[p + 20:p + 22]
    else:
        cre, mod, ts, _d = struct.unpack(">QQIQ", moov[p + 4:p + 32]); lang = moov[p + 32:p + 34]
    stsd = _mp4_find(moov, b"stsd", sb, se)
    try:
        _mp4_find(moov, b"ctts", sb, se)
        raise TinyError("the video has reordered frames (B-frames), which tiny mode doesn't support")
    except TinyError as e:
        if "B-frames" in str(e):
            raise

    z = _mp4_find(moov, b"stsz", sb, se)
    q = z[0] + z[2]
    ssize, count = struct.unpack(">II", moov[q + 4:q + 12])
    sizes = list(struct.unpack(">%dI" % count, moov[q + 12:q + 12 + 4 * count])) if ssize == 0 else None
    tt = _mp4_find(moov, b"stts", sb, se)
    q = tt[0] + tt[2]
    n_ent = struct.unpack(">I", moov[q + 4:q + 8])[0]
    ents = [struct.unpack(">II", moov[q + 8 + 8 * i:q + 16 + 8 * i]) for i in range(n_ent)]
    deltas = {d for _, d in ents}
    if strict_timing and max(deltas) - min(deltas) > 1:
        raise TinyError("variable frame timing isn't supported in tiny mode")
    delta = max(ents, key=lambda e: e[0])[1]
    keys = None
    try:
        k = _mp4_find(moov, b"stss", sb, se)
        q = k[0] + k[2]
        kc = struct.unpack(">I", moov[q + 4:q + 8])[0]
        keys = [x - 1 for x in struct.unpack(">%dI" % kc, moov[q + 8:q + 8 + 4 * kc])]
    except TinyError:
        pass
    try:
        c = _mp4_find(moov, b"stco", sb, se)
        q = c[0] + c[2]
        first = struct.unpack(">I", moov[q + 8:q + 12])[0]
    except TinyError:
        c = _mp4_find(moov, b"co64", sb, se)
        q = c[0] + c[2]
        first = struct.unpack(">Q", moov[q + 8:q + 16])[0]
    data_len = sum(sizes) if sizes is not None else ssize * count
    if first != mdat[0] or data_len != mdat[1]:
        raise TinyError("unexpected layout in an intermediate file")
    return {
        "ftyp": ftyp, "ts": ts, "cre": cre, "mod": mod, "lang": lang,
        "tkhd": bytes(moov[tkhd[0]:tkhd[0] + tkhd[1]]), "mvhd": bytes(moov[mvhd[0]:mvhd[0] + mvhd[1]]),
        "hdlr": bytes(moov[hdlr[0]:hdlr[0] + hdlr[1]]), "stsd": bytes(moov[stsd[0]:stsd[0] + stsd[1]]),
        "minf_other": b"".join(bytes(moov[o:o + s]) for t, o, s, h in _mp4_boxes(moov, minf[0] + minf[2], minf[0] + minf[1])
                               if t != b"stbl"),
        "sizes": sizes, "sample_size": ssize, "count": count, "delta": delta, "keys": keys,
        "data_off": mdat[0], "data_len": mdat[1], "path": path,
    }

def _tkhd_rebuilt(raw, track_id, dur):
    p = 8
    ver = raw[p]
    flags = raw[p + 1:p + 4]
    if ver == 0:
        cre, mod, _tid, res, _d = struct.unpack(">IIIII", raw[p + 4:p + 24]); rest = raw[p + 24:]
    else:
        cre, mod, _tid, res, _d = struct.unpack(">QQIIQ", raw[p + 4:p + 36]); rest = raw[p + 36:]
    if dur < 2 ** 32 and cre < 2 ** 32 and mod < 2 ** 32:
        pl = b"\x00" + flags + struct.pack(">IIIII", cre, mod, track_id, res, dur) + rest
    else:
        pl = b"\x01" + flags + struct.pack(">QQIIQ", cre, mod, track_id, res, dur) + rest
    return _mp4_box(b"tkhd", pl)

def _mdhd_rebuilt(M, ts, dur):
    if dur < 2 ** 32 and M["cre"] < 2 ** 32 and M["mod"] < 2 ** 32:
        pl = b"\x00\x00\x00\x00" + struct.pack(">IIII", M["cre"], M["mod"], ts, dur) + M["lang"] + b"\x00\x00"
    else:
        pl = b"\x01\x00\x00\x00" + struct.pack(">QQIQ", M["cre"], M["mod"], ts, dur) + M["lang"] + b"\x00\x00"
    return _mp4_box(b"mdhd", pl)

def _tiny_units(segs):
    """Group the play list into units: every repeated word alone, runs of plays-once parts together."""
    units = []
    for sf, ef, n, _ in segs:
        if n == 1 and units and units[-1]["n"] == 1 and units[-1]["ef"] == sf:
            units[-1]["ef"] = ef
        else:
            units.append({"sf": sf, "ef": ef, "n": n})
    return units

def _make_referencing(vfile, units, achunks, dst, fr):
    """Write dst: video (+ per-unit AAC clips) stored once, with tables that replay each unit n times."""
    from bisect import bisect_left
    V = _parse_simple_mov(vfile)
    if V["sizes"] is None:
        raise TinyError("unexpected video layout")
    vs = V["sizes"]
    keys = V["keys"] if V["keys"] is not None else list(range(len(vs)))
    voff = [0]
    for s_ in vs:
        voff.append(voff[-1] + s_)

    num, den = fr.numerator, fr.denominator
    rate = _TINY_RATE
    vts = rate * num                                   # video ticks: exactly `num` ticks per sound sample
    D = den * rate                                     # one frame, in video ticks
    movie_ts = rate

    ftyp = V["ftyp"]
    v_start = len(ftyp) + 16
    a_start = v_start + V["data_len"]
    a_len = sum(c["data_len"] for c in achunks) if achunks else 0
    mdat_len = V["data_len"] + a_len
    wide = (a_start + a_len) > 0xFFFFFFFF
    off_fmt = ">Q" if wide else ">I"

    # chunk offsets inside the sound block
    a_offs, acc = [], 0
    for c in (achunks or []):
        a_offs.append(a_start + acc)
        acc += c["data_len"]

    v_sz, v_co, v_ss, v_tt, v_sc = [], [], [], [], []
    a_sz, a_co, a_sc = [], [], []
    v_chunk = v_sample = v_tt_count = 0
    v_chunk, v_sample = 1, 1
    a_chunk = 1
    total_ticks_samples = 0                             # whole result length, in sound samples (1/48000 s)
    pk_total = 0
    for ui, u in enumerate(units):
        sf, ef, n = u["sf"], u["ef"], u["n"]
        F = ef - sf
        if F <= 0 or ef > len(vs):
            raise TinyError("a cut point is outside the video")
        i0 = bisect_left(keys, sf)
        ks = []
        while i0 < len(keys) and keys[i0] < ef:
            ks.append(keys[i0] - sf)
            i0 += 1
        if not ks or ks[0] != 0:
            raise TinyError("a cut point is not on a keyframe")
        # ---- video
        v_sz.append(struct.pack(">%dI" % F, *vs[sf:ef]) * n)
        v_co.append(struct.pack(off_fmt, v_start + voff[sf]) * n)
        if not v_sc or v_sc[-1][1] != F:
            v_sc.append((v_chunk, F, 1))
        if len(ks) == 1:
            v_ss.append(_be32(range(v_sample, v_sample + n * F, F)))
        else:
            v_ss.append(_be32([v_sample + j * F + k for j in range(n) for k in ks]))
        # frame timing: with sound, hold the first frame while the AAC decoder warms up and the last
        # frame while the clip's tail plays, so every repeat is exactly as long as its sound clip
        natural = F * D
        if achunks:
            N = len(achunks[ui]["sizes"])
            unit_ticks = N * 1024 * num
            extra = unit_ticks - natural
            if extra < 0:
                raise TinyError("a sound clip is shorter than its picture")
            lead = min(1024 * num, extra)
            tail = extra - lead
        else:
            N, unit_ticks, lead, tail = 0, natural, 0, 0
        if F == 1:
            ents = [(1, D + extra if achunks else D)]
        else:
            ents = [(1, D + lead)] + ([(F - 2, D)] if F > 2 else []) + [(1, D + tail)]
        ents = [e for e in ents if e[0] > 0]
        for c_, d_ in ents:
            if d_ >= 2 ** 32:
                raise TinyError("a frame is held for too long")
        v_tt.append(b"".join(struct.pack(">II", *e) for e in ents) * n)
        v_tt_count += len(ents) * n
        total_ticks_samples += (unit_ticks * n) // num if achunks else 0
        # ---- sound
        if achunks:
            a_sz.append(struct.pack(">%dI" % N, *achunks[ui]["sizes"]) * n)
            a_co.append(struct.pack(off_fmt, a_offs[ui]) * n)
            if not a_sc or a_sc[-1][1] != N:
                a_sc.append((a_chunk, N, 1))
            a_chunk += n
            pk_total += N * n
        v_chunk += n
        v_sample += n * F
    v_total = v_sample - 1
    n_chunks_v = v_chunk - 1
    if v_total >= 2 ** 32 or pk_total >= 2 ** 32:
        raise TinyError("that result is too long for one file (over 4 billion frames)")

    total_ticks = sum(((u["ef"] - u["sf"]) * D + 0) * u["n"] for u in units)
    if achunks:
        total_ticks = pk_total * 1024 * num
    movie_dur = (pk_total * 1024) if achunks else (total_ticks // num)

    v_stbl = (V["stsd"]
              + _mp4_box(b"stts", b"\x00\x00\x00\x00" + struct.pack(">I", v_tt_count) + b"".join(v_tt))
              + _mp4_box(b"stss", b"\x00\x00\x00\x00" + struct.pack(">I", sum(len(x) for x in v_ss) // 4) + b"".join(v_ss))
              + _mp4_box(b"stsc", b"\x00\x00\x00\x00" + struct.pack(">I", len(v_sc)) + b"".join(struct.pack(">III", *e) for e in v_sc))
              + _mp4_box(b"stsz", b"\x00\x00\x00\x00" + struct.pack(">II", 0, v_total) + b"".join(v_sz))
              + _mp4_box(b"co64" if wide else b"stco", b"\x00\x00\x00\x00" + struct.pack(">I", n_chunks_v) + b"".join(v_co)))
    del v_sz, v_co, v_ss, v_tt
    v_trak = _mp4_box(b"trak", _tkhd_rebuilt(V["tkhd"], 1, movie_dur)
                      + _mp4_box(b"mdia", _mdhd_rebuilt(V, vts, total_ticks) + V["hdlr"]
                                 + _mp4_box(b"minf", V["minf_other"] + _mp4_box(b"stbl", v_stbl))))
    a_trak = b""
    if achunks:
        A0 = achunks[0]["meta"]
        a_stbl = (A0["stsd"]
                  + _mp4_box(b"stts", b"\x00\x00\x00\x00" + struct.pack(">III", 1, pk_total, 1024))
                  + _mp4_box(b"stsc", b"\x00\x00\x00\x00" + struct.pack(">I", len(a_sc)) + b"".join(struct.pack(">III", *e) for e in a_sc))
                  + _mp4_box(b"stsz", b"\x00\x00\x00\x00" + struct.pack(">II", 0, pk_total) + b"".join(a_sz))
                  + _mp4_box(b"co64" if wide else b"stco", b"\x00\x00\x00\x00" + struct.pack(">I", a_chunk - 1) + b"".join(a_co)))
        del a_sz, a_co
        a_trak = _mp4_box(b"trak", _tkhd_rebuilt(A0["tkhd"], 2, movie_dur)
                          + _mp4_box(b"mdia", _mdhd_rebuilt(A0, rate, pk_total * 1024) + A0["hdlr"]
                                     + _mp4_box(b"minf", A0["minf_other"] + _mp4_box(b"stbl", a_stbl))))

    mv = V["mvhd"][8:]
    ver = mv[0]
    if ver == 0:
        cre, mod, _ts, _d = struct.unpack(">IIII", mv[4:20]); rest = mv[20:]
    else:
        cre, mod, _ts, _d = struct.unpack(">QQIQ", mv[4:32]); rest = mv[32:]
    rest = rest[:-4] + struct.pack(">I", 3)
    if movie_dur < 2 ** 32 and cre < 2 ** 32 and mod < 2 ** 32:
        mvhd = _mp4_box(b"mvhd", b"\x00\x00\x00\x00" + struct.pack(">IIII", cre, mod, movie_ts, movie_dur) + rest)
    else:
        mvhd = _mp4_box(b"mvhd", b"\x01\x00\x00\x00" + struct.pack(">QQIQ", cre, mod, movie_ts, movie_dur) + rest)
    moov = _mp4_box(b"moov", mvhd + v_trak + a_trak)

    def copy_range(src, off, length, out):
        with open(src, "rb") as f:
            f.seek(off)
            left = length
            while left > 0:
                chunk = f.read(min(left, 8 << 20))
                if not chunk:
                    raise TinyError("an intermediate file ended early")
                out.write(chunk)
                left -= len(chunk)

    with open(dst, "wb") as out:
        out.write(ftyp)
        out.write(struct.pack(">I4sQ", 1, b"mdat", 16 + mdat_len))
        copy_range(V["path"], V["data_off"], V["data_len"], out)
        for c in (achunks or []):
            copy_range(c["path"], c["data_off"], c["data_len"], out)
        out.write(moov)

def _run_stdin(cmd, data):
    """Like run(), but feeds `data` to ffmpeg's standard input."""
    _check_cancel()
    p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, **_NOWIN)
    _active.append(p)
    try:
        _, err = p.communicate(data)
    finally:
        if p in _active:
            _active.remove(p)
    _check_cancel()
    if p.returncode != 0:
        sys.exit("ffmpeg failed:\n" + " ".join(cmd) + "\n" + err.decode("utf-8", "replace")[-1500:])

def _encode_sound_units(a, units, fr, total_frames, fps_f, tmp):
    """One small AAC clip per unit (each encoded on its own, so any repeat starts cleanly)."""
    rate, bpf = _TINY_RATE, 4
    raw = os.path.join(tmp, "audio48.raw")
    run(["ffmpeg", "-y", "-v", "error", "-i", a.video, "-vn",
         "-af", "aresample=%d:async=1:first_pts=0,apad" % rate, "-t", "%.3f" % (total_frames / fps_f + 0.5),
         "-ac", "2", "-f", "s16le", raw])
    rnd = lambda q: int(math.floor(q + Fraction(1, 2)))
    done = [0]
    t0 = time.time()
    lock = threading.Lock()

    def enc(i):
        u = units[i]
        s0 = rnd(Fraction(u["sf"]) * rate / fr)
        L = rnd(Fraction(u["ef"] - u["sf"]) * rate / fr)
        S = -(-L // 1024) * 1024
        with open(raw, "rb") as f:
            f.seek(s0 * bpf)
            data = f.read(L * bpf)
        data = data.ljust(S * bpf, b"\0")
        out = os.path.join(tmp, "ac_%05d.mp4" % i)
        _run_stdin(["ffmpeg", "-y", "-v", "error", "-f", "s16le", "-ar", str(rate), "-ac", "2", "-i", "-",
                    "-c:a", "aac", "-b:a", a.audio_bitrate, "-f", "mp4", "-movflags", "0", out], data)
        M = _parse_simple_mov(out, strict_timing=False)
        with lock:
            done[0] += 1
            show_progress("Preparing sound %d/%d" % (done[0], len(units)), done[0] / float(len(units)), t0)
        return {"path": out, "sizes": M["sizes"], "data_off": M["data_off"], "data_len": M["data_len"], "meta": M}

    with ThreadPoolExecutor(max_workers=max(1, min(4, os.cpu_count() or 2))) as ex:
        res = list(ex.map(enc, range(len(units))))
    sys.stdout.write("\n")
    return res

def tiny_render(a, segs, fps, has_audio, total_frames, tmp, dest, crf):
    # the video is only encoded once here, so it can afford better compression than the fast mode
    fr, fps_str, fps_f, chunks, paths = _encode_chunks(a, segs, fps, total_frames, tmp, crf, a.preset)
    raw = os.path.join(tmp, "unique.h264")
    with open(raw, "wb") as out:
        for pth in paths:
            with open(pth, "rb") as f:
                shutil.copyfileobj(f, out)
    t0 = time.time()
    vfile = os.path.join(tmp, "unique_v.mp4")
    vts = fr.numerator * max(1, -(-10000 // fr.numerator))
    run(["ffmpeg", "-y", "-v", "error", "-f", "h264", "-framerate", fps_str, "-i", raw,
         "-c:v", "copy", "-an", "-video_track_timescale", str(vts), "-f", "mp4", "-movflags", "0", vfile])
    units = _tiny_units(segs)
    achunks = _encode_sound_units(a, units, fr, total_frames, fps_f, tmp) if has_audio else None
    show_progress("Building result", 0.5, t0)
    _make_referencing(vfile, units, achunks, dest, fr)
    show_progress("Building result", 1.0, t0)
    sys.stdout.write("\n")

def repeat_count(f, length, idx=1, words=1, cue=1, round_mode="floor", drop_zero=False, max_repeats=None):
    """How many times a word of this length plays. Raises ValueError with a readable message."""
    try:
        v = f({"len": length, "idx": idx, "words": words, "cue": cue})
    except OverflowError:
        v = INF
    if v != v:
        raise ValueError("Formula gave an invalid number for len=%d" % length)
    if v == INF and max_repeats is None:
        raise ValueError("Formula gives an infinite repeat count for len=%d. Set a max repeats limit." % length)
    if v == -INF:
        v = 0
    elif v == INF:
        v = max_repeats
    else:
        v = {"floor": math.floor, "round": lambda x: math.floor(x + 0.5), "ceil": math.ceil}[round_mode](v)
    if v < 1:
        v = 0 if drop_zero else 1
    if max_repeats is not None:
        v = min(v, max_repeats)
    return int(v)

# -------------------------------------------------------------------- main
def main(argv=None):
    global _cancelled
    _cancelled = False
    ap = argparse.ArgumentParser(description="Repeat each subtitle word's video clip based on its length.")
    ap.add_argument("video"); ap.add_argument("srt")
    ap.add_argument("-f", "--formula", default="len", help='repeat count formula, e.g. "len^2" (default: len)')
    ap.add_argument("-o", "--output", default=None, help="output .mp4 (default: <video>_repeated.mp4)")
    ap.add_argument("--max-repeats", type=int, default=None, help="optional cap per word (default: no cap)")
    ap.add_argument("--round", choices=["floor", "round", "ceil"], default="floor")
    ap.add_argument("--count-all", action="store_true", help="count punctuation in the length (default: letters/digits only)")
    ap.add_argument("--drop-zero", action="store_true", help="remove words whose count is below 1 (default: keep once)")
    ap.add_argument("--no-gaps", action="store_true", help="only output the word clips, drop everything between words")
    ap.add_argument("--burn", action="store_true", help="burn the word text into the video (needs ffmpeg with libass)")
    ap.add_argument("--max-minutes", type=float, default=180, help="refuse to build output longer than this (default 180)")
    ap.add_argument("--force", action="store_true", help="ignore --max-minutes")
    ap.add_argument("--crf", type=int, default=None, help="video quality, lower = better and bigger (default 26 in fast mode, 22 with --compress)")
    ap.add_argument("--max-bitrate", default="auto",
                    help='cap the final video bitrate, e.g. "4M". Default "auto" = never above the source video\'s own bitrate; "none" = no cap')
    ap.add_argument("--preset", default="faster", help="with --compress: x264 speed/size trade-off, ultrafast..veryslow (default faster; slower = smaller file)")
    ap.add_argument("--audio-bitrate", default="160k", help="audio bitrate (default 160k)")
    ap.add_argument("--compress", action="store_true",
                    help="slower: re-encode the whole result for a smaller file (default is the fast mode)")
    ap.add_argument("--no-compress", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--tiny", action="store_true",
                    help="store the video once and make the player repeat it (tiny file, plays instantly; "
                         "works in VLC/mpv and other ffmpeg-based players, not everywhere)")
    ap.add_argument("--no-srt", action="store_true", help="don't write the matching .srt file")
    ap.add_argument("--dry-run", action="store_true", help="only work out the output length, don't build anything")
    a = ap.parse_args(argv)

    for tool in ("ffmpeg", "ffprobe"):
        if not shutil.which(tool):
            sys.exit("%s not found. Install ffmpeg first: https://ffmpeg.org/download.html" % tool)
    try:
        f = compile_formula(a.formula)
    except ValueError as e:
        sys.exit("Formula error: " + str(e))

    fps, has_audio, vid_dur = probe(a.video)
    words = split_words(parse_srt(a.srt))
    if not words:
        sys.exit("No usable cues found in the .srt file.")

    # ---- repeat counts
    for w in words:
        # letters, digits and apostrophes count; other punctuation (, . ! ? etc.) does not
        letters = re.sub(r"[^\w'\u2019\u2018]|_", "", w["tok"], flags=re.UNICODE)
        if not re.search(r"[^\W_]", w["tok"], flags=re.UNICODE):
            letters = ""                    # a token made only of symbols/apostrophes has no length
        length = len(w["tok"]) if a.count_all else len(letters)
        if not a.count_all and length == 0:
            w["n"] = 1; continue
        try:
            w["n"] = repeat_count(f, length, w["idx"], w["words"], w["cue"], a.round, a.drop_zero, a.max_repeats)
        except ValueError as e:
            sys.exit(str(e))

    # ---- timeline of (start, end, count, label)
    # Everything is snapped to whole video frames so picture and sound stay in sync
    # no matter how many times a clip is repeated. Parts of the video that no word
    # covers (intro, gaps, instrumental sections, the ending) are kept once, in order.
    total_frames = int(round(vid_dur * fps))
    exact = count_video_frames(a.video)           # the container length can include slightly longer audio
    if exact and abs(exact - total_frames) <= max(3, total_frames * 0.01):
        total_frames = exact
    segs, pos = [], 0                      # (start_frame, end_frame, repeats, label)
    for w in words:
        sf = max(int(round(w["start"] * fps)), pos)
        ef = min(int(round(w["end"] * fps)), total_frames)
        if ef <= sf:
            ef = min(sf + 1, total_frames)
        if ef <= sf:
            continue
        if not a.no_gaps and sf > pos:
            segs.append((pos, sf, 1, None))
        if w["n"] > 0:
            segs.append((sf, ef, w["n"], w["tok"]))
        pos = ef
    if not a.no_gaps and total_frames > pos:
        segs.append((pos, total_frames, 1, None))

    total = sum((e - s) * n for s, e, n, _ in segs) / fps
    print("%d words -> %d clips, output length about %.1f minutes (source is %.1f min)"
          % (len(words), sum(1 for s in segs if s[3] is not None), total / 60, vid_dur / 60))
    print("ESTIMATE total=%.2f source=%.2f words=%d" % (total, vid_dur, len(words)))
    if a.dry_run:
        return
    if total / 60 > a.max_minutes and not a.force:
        sys.exit("That's longer than --max-minutes (%g). Use a smaller formula, set --max-repeats, or pass --force." % a.max_minutes)

    if a.tiny and a.burn:
        sys.exit("--tiny can't be combined with --burn (burning needs a full re-encode).")
    compress = a.compress and not a.no_compress
    crf = a.crf if a.crf is not None else (22 if (compress or a.tiny) else 26)
    if compress:
        print("Compressed mode (crf %d, preset %s): smaller file, but slower than the default fast mode." % (crf, a.preset))
    out_path = a.output or os.path.splitext(a.video)[0] + "_repeated.mp4"
    srt_path = os.path.splitext(out_path)[0] + ".srt"
    total_frames_all = total_frames
    tmp = tempfile.mkdtemp(prefix="repeat_video_")
    try:
        # ---- matching subtitle file (timed to the NEW video)
        cue_total = sum(n_ for _, _, n_, label in segs if label)
        write_srt = not a.no_srt and cue_total <= 2000000
        if write_srt:
            t, cue_no = 0.0, 0
            with open(srt_path, "w", encoding="utf-8") as sf_:
                for s_, e_, n_, label in segs:
                    d = (e_ - s_) / fps
                    if not label:
                        t += d * n_
                        continue
                    batch = []
                    for _ in range(n_):
                        cue_no += 1
                        batch.append("%d\n%s --> %s\n%s\n\n" % (cue_no, fmt_srt(t), fmt_srt(t + d), label))
                        t += d
                        if len(batch) >= 20000:
                            sf_.write("".join(batch))
                            batch = []
                    sf_.write("".join(batch))
        elif a.burn:
            sys.exit("--burn needs the matching subtitles, which are skipped for results with this many words.")

        final_v = ["-c:v", "libx264", "-preset", a.preset if compress else "veryfast", "-crf", str(crf),
                   "-pix_fmt", "yuv420p", "-g", "240"]
        if compress or a.burn:
            cap = None
            if a.max_bitrate.lower() == "auto":
                src_br = source_video_bitrate(a.video)
                if src_br:
                    cap = str(max(int(src_br), 1000000))      # never starve a low-bitrate source below 1 Mbps
            elif a.max_bitrate.lower() != "none":
                cap = a.max_bitrate
            if cap:
                mult = 2 * int(cap[:-1]) if cap[-1:] in "kKmM" and cap[:-1].isdigit() else None
                buf = (str(mult) + cap[-1]) if mult else str(2 * int(cap))
                final_v += ["-maxrate", cap, "-bufsize", buf]
                print("Bitrate cap for the final video: %s (keeps the file no bigger than the source per second)" % cap)

        joined = os.path.join(tmp, "joined.mp4")
        fast_ok = False
        if a.tiny:
            try:
                tiny_render(a, segs, fps, has_audio, total_frames_all, tmp, joined, crf)
            except (FastPathError, TinyError) as e:
                sys.exit("Tiny-file mode couldn't be used: %s" % e)
            fast_ok = True
        elif not compress:
            try:
                fast_render(a, segs, fps, has_audio, total_frames_all, tmp, joined, crf)
                fast_ok = True
            except FastPathError as e:
                sys.stdout.write("\nFast mode couldn't be used (%s). Switching to the careful method...\n" % e)
        if not fast_ok:
            # ---- careful method: cut each unique piece once, then join (slower, always works)
            clip_crf, clip_preset = ("12", "veryfast") if compress else (str(crf), "ultrafast")
            vopts = ["-c:v", "libx264", "-preset", clip_preset, "-crf", clip_crf, "-pix_fmt", "yuv420p", "-r", "%.5f" % fps]
            aopts = ["-c:a", "pcm_s16le", "-ar", "48000", "-ac", "2"] if has_audio else ["-an"]
            files, uniq = {}, []
            for s_, e_, n_, _ in segs:
                if (s_, e_) not in files:
                    files[(s_, e_)] = "clip_%05d.mov" % len(files)
                    uniq.append((s_, e_))
            cut_total = float(sum(e_ - s_ for s_, e_ in uniq)) or 1.0
            cut_done, t_cut = 0.0, time.time()
            for i, (s_, e_) in enumerate(uniq, 1):
                show_progress("Cutting clips %d/%d" % (i - 1, len(uniq)), cut_done / cut_total, t_cut)
                af = ["-af", "apad,atrim=duration=%.6f" % ((e_ - s_) / fps)] if has_audio else []
                run(["ffmpeg", "-y", "-v", "error", "-ss", "%.6f" % (s_ / fps), "-i", a.video,
                     "-frames:v", str(e_ - s_)] + vopts + aopts + af + [os.path.join(tmp, files[(s_, e_)])])
                cut_done += e_ - s_
            show_progress("Cutting clips %d/%d" % (len(uniq), len(uniq)), 1.0, t_cut)
            print()
            list_path = os.path.join(tmp, "list.txt")
            with open(list_path, "w") as lf:
                for s_, e_, n_, _ in segs:
                    lf.write(("file '%s'\n" % files[(s_, e_)]) * n_)
            join_now = compress and not a.burn          # with --burn the compression happens in the burn pass
            run_progress(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", list_path]
                         + (final_v if join_now else ["-c:v", "copy"])
                         + (["-c:a", "aac", "-b:a", a.audio_bitrate] if has_audio else ["-an"])
                         + ["-movflags", "+faststart", joined],
                         total, "Compressing video" if join_now else "Rendering video   ")

        if a.burn:
            shutil.copy(srt_path, os.path.join(tmp, "subs.srt"))
            cmd = ["ffmpeg", "-y", "-v", "error", "-i", "joined.mp4", "-vf", "subtitles=subs.srt"] \
                  + final_v + (["-c:a", "copy"] if has_audio else ["-an"]) + ["-movflags", "+faststart", "burned.mp4"]
            run_progress(cmd, total, "Burning subtitles ", cwd=tmp, hint="while burning in subtitles (does your ffmpeg include libass?)")
            joined = os.path.join(tmp, "burned.mp4")

        shutil.move(joined, out_path)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("Done: " + out_path)
    if write_srt:
        print("Matching subtitles: " + srt_path)
    else:
        print("Matching subtitles skipped (%s words is too many for an .srt file)" % format(cue_total, ","))

if __name__ == "__main__":
    main()
