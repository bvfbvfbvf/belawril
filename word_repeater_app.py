#!/usr/bin/env python3
"""
Word Repeater - desktop app.

Pick a video and an .srt (one word per cue, with its start/end time), write a formula
(like len^2), press Start. Each word's clip is repeated by the formula, and everything
the .srt doesn't cover (intro, gaps, instrumental parts, ending) plays once, in order.

Run it directly:      python word_repeater_app.py      (needs ffmpeg on PATH)
Make an .exe:         double-click build_exe.bat       (Windows)
"""
import os
import queue
import re
import subprocess
import sys
import threading


# ---------------------------------------------------------------- ffmpeg lookup
def _app_dirs():
    dirs = []
    if getattr(sys, "frozen", False):
        dirs.append(getattr(sys, "_MEIPASS", ""))
        dirs.append(os.path.dirname(sys.executable))
    else:
        dirs.append(os.path.dirname(os.path.abspath(__file__)))
    return [d for d in dirs if d]


def setup_ffmpeg_path():
    """Let the app find ffmpeg/ffprobe sitting next to it (or bundled inside it)."""
    os.environ["PATH"] = os.pathsep.join(_app_dirs() + [os.environ.get("PATH", "")])


setup_ffmpeg_path()

# Windowed .exe builds have no console, so these can be None. Give them a harmless sink.
class _Null:
    def write(self, s):
        return len(s or "")

    def flush(self):
        pass


if sys.stdout is None:
    sys.stdout = _Null()
if sys.stderr is None:
    sys.stderr = _Null()

import repeat_video as engine  # noqa: E402  (after PATH setup on purpose)

# --------------------------------------------------------------------- settings
QUALITY = {
    "Fastest (bigger file)": [],                                      # the engine's default fast mode
    "Smaller file (slower)": ["--compress", "--preset", "faster"],
    "Smallest file (slowest)": ["--compress", "--preset", "slow"],
    "Tiny file (instant)": ["--tiny"],
}
TINY = "Tiny file (instant)"
TINY_HINT = ("Tiny file: the video and sound are stored once and the file's index tells the player to repeat "
             "them, so the file stays small however long the result plays and it's made in seconds. It's a normal "
             "MP4 with sound. Each repeat holds its first and last picture for a few hundredths of a second, so "
             "the result runs about 5% longer than the length shown. Very large results need a lot of memory "
             "to build (several GB per 100 days of video).")
DEFAULT_QUALITY = "Fastest (bigger file)"
ROUNDING = {"Round down": "floor", "Round to nearest": "round", "Round up": "ceil"}
PRESET_FORMULAS = ["len", "len^2", "len^3", "2^len", "len*2", "len!", "floor(len/2)",
                   "len%3+1", "max(1,6-len)", "idx*len"]

PROG_RE = re.compile(r"^(?P<label>.+?)\s*\[(?P<bar>[#-]+)\]\s+(?P<pct>[\d.]+)%\s*\|\s*"
                     r"elapsed\s+(?P<el>[\d:]+)\s*\|\s*(?P<eta>.+?)\s*$")
EST_RE = re.compile(r"^ESTIMATE total=([\d.]+) source=([\d.]+) words=(\d+)$")


# ------------------------------------------------------------ pure helper logic
def fmt_len(sec):
    if sec < 120:
        return "%.0f seconds" % sec
    m = sec / 60.0
    if m < 120:
        return "%.1f minutes" % m
    return "%d h %02d min" % (m // 60, m % 60)


def build_args(video, srt, out, formula, limit=None, rounding="floor", keep_gaps=True,
               count_all=False, keep_once=True, burn=False, quality=DEFAULT_QUALITY,
               dry_run=False):
    """Turn the window's settings into command-line arguments for the engine."""
    a = [video, srt, "--formula=" + formula.strip(), "-o", out, "--round", rounding, "--force"]
    if limit:
        a += ["--max-repeats", str(int(limit))]
    if not keep_gaps:
        a.append("--no-gaps")
    if count_all:
        a.append("--count-all")
    if not keep_once:
        a.append("--drop-zero")
    if burn:
        a.append("--burn")
    a += QUALITY.get(quality, QUALITY[DEFAULT_QUALITY])
    if dry_run:
        a.append("--dry-run")
    return a


def preview_columns(formula, rounding="floor", limit=None, keep_once=True, max_len=12):
    """[(word length, repeats or None)] for the little table under the formula box."""
    f = engine.compile_formula(formula)          # raises ValueError for a bad formula
    cols = []
    for length in range(1, max_len + 1):
        try:
            n = engine.repeat_count(f, length, 1, 1, 1, rounding, not keep_once, limit)
        except (ValueError, ArithmeticError):
            n = None
        cols.append((length, n))
    return cols


def preview_text(cols):
    top, bottom = ["Word length "], ["Repeats     "]
    for length, n in cols:
        cell = "inf" if n is None else format(n, ",")
        w = max(len(str(length)), len(cell)) + 2
        top.append(str(length).rjust(w))
        bottom.append(cell.rjust(w))
    return "".join(top) + "\n" + "".join(bottom)


class Sink:
    """File-like object that turns the engine's printed text into separate lines."""

    def __init__(self, emit):
        self.emit = emit
        self.buf = ""

    def write(self, s):
        if not s:
            return 0
        self.buf += s
        parts = re.split(r"[\r\n]", self.buf)
        self.buf = parts.pop()
        for p in parts:
            p = p.strip()
            if p:
                self.emit(p)
        tail = self.buf.strip()
        if tail and PROG_RE.match(tail):      # show progress straight away, not one update late
            self.emit(tail)
        return len(s)

    def flush(self):
        pass


def run_engine(argv, sink):
    """Run the engine; returns None on success or an error message."""
    old = (sys.stdout, sys.stderr)
    sys.stdout = sys.stderr = sink
    try:
        engine.main(argv)
        return None
    except SystemExit as e:
        if e.code in (None, 0):
            return None
        return e.code if isinstance(e.code, str) else "Those settings weren't accepted."
    except Exception as e:                    # never let the window crash silently
        return "Unexpected error: %r" % (e,)
    finally:
        sys.stdout, sys.stderr = old


def open_path(path):
    try:
        if sys.platform == "win32":
            os.startfile(path)                # noqa: S606
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])
    except Exception:
        pass


# ------------------------------------------------------------------------- GUI
def main_window():
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk

    try:                                       # crisp text on high-DPI Windows screens
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass

    class App(tk.Tk):
        def __init__(self):
            super().__init__()
            self.title("Word Repeater")
            self.minsize(760, 720)
            try:
                ttk.Style(self).theme_use("vista" if sys.platform == "win32" else "clam")
            except tk.TclError:
                pass

            self.q = queue.Queue()
            self.running = False
            self.estimate = None
            self.after_id = None
            self.out_is_auto = True
            self.step_names = []

            self.v_video, self.v_srt, self.v_out = tk.StringVar(), tk.StringVar(), tk.StringVar()
            self.v_formula = tk.StringVar(value="len^2")
            self.v_limit, self.v_cap = tk.BooleanVar(value=False), tk.StringVar(value="20")
            self.v_round = tk.StringVar(value="Round down")
            self.v_gaps, self.v_countall = tk.BooleanVar(value=True), tk.BooleanVar(value=False)
            self.v_min1, self.v_burn = tk.BooleanVar(value=True), tk.BooleanVar(value=False)
            self.v_quality = tk.StringVar(value=DEFAULT_QUALITY)

            self._build()
            for v in (self.v_video, self.v_srt, self.v_formula, self.v_limit, self.v_cap,
                      self.v_round, self.v_gaps, self.v_countall, self.v_min1):
                v.trace_add("write", lambda *a: self.on_change())
            self.v_quality.trace_add("write", lambda *a: self.on_quality())
            self.protocol("WM_DELETE_WINDOW", self.on_close)
            self.on_change()

        # ---- layout
        def _build(self):
            pad = {"padx": 10, "pady": 6}
            root = ttk.Frame(self, padding=8)
            root.pack(fill="both", expand=True)
            root.columnconfigure(0, weight=1)

            f1 = ttk.LabelFrame(root, text=" 1. Files ")
            f1.grid(row=0, column=0, sticky="ew", **pad)
            f1.columnconfigure(1, weight=1)
            rows = [("Video", self.v_video, self.browse_video),
                    ("Subtitles (.srt)", self.v_srt, self.browse_srt),
                    ("Save result as", self.v_out, self.browse_out)]
            for i, (label, var, cmd) in enumerate(rows):
                ttk.Label(f1, text=label).grid(row=i, column=0, sticky="w", padx=8, pady=4)
                ttk.Entry(f1, textvariable=var).grid(row=i, column=1, sticky="ew", padx=4, pady=4)
                ttk.Button(f1, text="Browse...", command=cmd).grid(row=i, column=2, padx=8, pady=4)

            f2 = ttk.LabelFrame(root, text=" 2. Repeat formula ")
            f2.grid(row=1, column=0, sticky="ew", **pad)
            f2.columnconfigure(0, weight=1)
            self.cb = ttk.Combobox(f2, textvariable=self.v_formula, values=PRESET_FORMULAS,
                                   font=("Consolas", 13))
            self.cb.grid(row=0, column=0, sticky="ew", padx=8, pady=(8, 2))
            self.l_formula = ttk.Label(f2, text="", foreground="#b00020")
            self.l_formula.grid(row=1, column=0, sticky="w", padx=8)
            self.l_preview = ttk.Label(f2, text="", font=("Consolas", 10), justify="left")
            self.l_preview.grid(row=2, column=0, sticky="w", padx=8, pady=4)
            ttk.Label(f2, justify="left", foreground="#666666", text=(
                "len = word length (apostrophes count)   idx = word position   pi, e\n"
                "Operators: + - * / % ^ (power) ! (factorial) and brackets.  "
                "Functions: floor ceil round sqrt abs min max pow log ln log2 fact"
            )).grid(row=3, column=0, sticky="w", padx=8, pady=(0, 8))

            f3 = ttk.LabelFrame(root, text=" 3. Options ")
            f3.grid(row=2, column=0, sticky="ew", **pad)
            f3.columnconfigure(3, weight=1)
            ttk.Checkbutton(f3, text="Limit repeats per word to", variable=self.v_limit,
                            command=self._toggle_cap).grid(row=0, column=0, sticky="w", padx=8, pady=4)
            self.sp = ttk.Spinbox(f3, from_=1, to=100000, width=7, textvariable=self.v_cap,
                                  state="disabled")
            self.sp.grid(row=0, column=1, sticky="w")
            ttk.Combobox(f3, textvariable=self.v_round, values=list(ROUNDING), state="readonly",
                         width=18).grid(row=0, column=2, padx=12)
            ttk.Combobox(f3, textvariable=self.v_quality, values=list(QUALITY), state="readonly",
                         width=28).grid(row=0, column=3, sticky="e", padx=8)
            self.l_hint = ttk.Label(f3, text="", foreground="#555555", wraplength=680, justify="left")
            self.l_hint.grid(row=3, column=0, columnspan=4, sticky="w", padx=8, pady=(2, 6))
            checks = [("Keep instrumental parts and gaps (played once)", self.v_gaps),
                      ("Keep every word at least once", self.v_min1),
                      ("Count every character, including punctuation", self.v_countall),
                      ("Burn the words onto the video as subtitles", self.v_burn)]
            for i, (text, var) in enumerate(checks):
                ttk.Checkbutton(f3, text=text, variable=var).grid(
                    row=1 + i // 2, column=(i % 2) * 2, columnspan=2, sticky="w", padx=8, pady=2)

            f4 = ttk.LabelFrame(root, text=" 4. Make it ")
            f4.grid(row=3, column=0, sticky="ew", **pad)
            f4.columnconfigure(0, weight=1)
            self.l_est = ttk.Label(f4, text="Choose a video and an .srt to see how long the result will be.",
                                   font=("Segoe UI", 10, "bold"))
            self.l_est.grid(row=0, column=0, columnspan=3, sticky="w", padx=8, pady=(8, 2))
            self.bar = ttk.Progressbar(f4, mode="determinate", maximum=100)
            self.bar.grid(row=1, column=0, columnspan=3, sticky="ew", padx=8, pady=4)
            self.l_status = ttk.Label(f4, text="Ready.")
            self.l_status.grid(row=2, column=0, columnspan=3, sticky="w", padx=8)
            btns = ttk.Frame(f4)
            btns.grid(row=3, column=0, columnspan=3, sticky="ew", padx=8, pady=8)
            self.b_start = ttk.Button(btns, text="Start", command=self.start)
            self.b_start.pack(side="left")
            self.b_cancel = ttk.Button(btns, text="Cancel", command=self.cancel, state="disabled")
            self.b_cancel.pack(side="left", padx=8)
            self.b_play = ttk.Button(btns, text="Play result", command=self.play, state="disabled")
            self.b_play.pack(side="right")
            self.b_folder = ttk.Button(btns, text="Open folder", command=self.open_folder,
                                       state="disabled")
            self.b_folder.pack(side="right", padx=8)

            self.log = tk.Text(root, height=6, state="disabled", wrap="word", font=("Consolas", 9))
            self.log.grid(row=4, column=0, sticky="nsew", padx=10, pady=(0, 6))
            root.rowconfigure(4, weight=1)

        # ---- small helpers
        def _toggle_cap(self):
            self.sp.configure(state="normal" if self.v_limit.get() else "disabled")

        def _log(self, text):
            self.log.configure(state="normal")
            self.log.insert("end", text + "\n")
            self.log.see("end")
            self.log.configure(state="disabled")

        def _limit(self):
            if not self.v_limit.get():
                return None
            try:
                return max(1, int(float(self.v_cap.get())))
            except ValueError:
                return None

        def _args(self, dry=False):
            return build_args(self.v_video.get(), self.v_srt.get(), self.v_out.get(),
                              self.v_formula.get(), self._limit(), ROUNDING[self.v_round.get()],
                              self.v_gaps.get(), self.v_countall.get(), self.v_min1.get(),
                              self.v_burn.get(), self.v_quality.get(), dry)

        def is_tiny(self):
            return self.v_quality.get() == TINY

        def on_quality(self):
            self.l_hint.configure(text=TINY_HINT if self.is_tiny() else "")
            self.on_change()

        # ---- file pickers
        def browse_video(self):
            p = filedialog.askopenfilename(title="Choose the video", filetypes=[
                ("Video files", "*.mp4 *.mov *.mkv *.avi *.webm *.m4v"), ("All files", "*.*")])
            if p:
                self.v_video.set(p)
                base = os.path.splitext(p)[0] + "_repeated.mp4"
                if self.out_is_auto or not self.v_out.get():
                    self.v_out.set(base)
                    self.out_is_auto = True

        def browse_srt(self):
            p = filedialog.askopenfilename(title="Choose the subtitle file", filetypes=[
                ("Subtitles", "*.srt *.vtt *.txt"), ("All files", "*.*")])
            if p:
                self.v_srt.set(p)

        def browse_out(self):
            p = filedialog.asksaveasfilename(title="Save the result as", defaultextension=".mp4",
                                             filetypes=[("MP4 video", "*.mp4")],
                                             initialfile=os.path.basename(self.v_out.get() or "result.mp4"))
            if p:
                self.v_out.set(p)
                self.out_is_auto = False

        # ---- live preview + estimate
        def on_change(self):
            if self.running:
                return
            try:
                cols = preview_columns(self.v_formula.get(), ROUNDING[self.v_round.get()],
                                       self._limit(), self.v_min1.get())
                self.l_formula.configure(text="")
                self.l_preview.configure(text=preview_text(cols))
            except ValueError as e:
                self.l_formula.configure(text=str(e))
                self.l_preview.configure(text="")
            if self.after_id:
                self.after_cancel(self.after_id)
            self.after_id = self.after(600, self.refresh_estimate)

        def estimate_now(self):
            lines = []
            err = run_engine(self._args(dry=True), Sink(lines.append))
            info = None
            for l in lines:
                m = EST_RE.match(l)
                if m:
                    info = (float(m.group(1)), float(m.group(2)), int(m.group(3)))
            return info, err

        def refresh_estimate(self):
            self.after_id = None
            self.estimate = None
            if self.running:
                return
            v, s = self.v_video.get(), self.v_srt.get()
            if not (os.path.isfile(v) and os.path.isfile(s)) or self.l_formula.cget("text"):
                self.l_est.configure(text="Choose a video and an .srt to see how long the result will be.",
                                     foreground="")
                return
            info, err = self.estimate_now()
            if info:
                self.estimate = info
                total, src, words = info
                warn = total > 3600 and not self.is_tiny()
                self.l_est.configure(
                    text="%d words. The result will be about %s long (the video is %s)%s"
                         % (words, fmt_len(total), fmt_len(src),
                            "  -  that's very long!" if warn else ""),
                    foreground="#b00020" if warn else "")
            else:
                self.l_est.configure(text=(err or "Couldn't read those files.").splitlines()[0],
                                     foreground="#b00020")

        # ---- running a job
        def start(self):
            video, srt, out = self.v_video.get(), self.v_srt.get(), self.v_out.get().strip()
            if not os.path.isfile(video):
                return messagebox.showwarning("Word Repeater", "Choose a video first.")
            if not os.path.isfile(srt):
                return messagebox.showwarning("Word Repeater", "Choose the .srt file first.")
            if not out:
                return messagebox.showwarning("Word Repeater", "Choose where to save the result.")
            if self.l_formula.cget("text"):
                return messagebox.showwarning("Word Repeater", "Fix the formula first: " + self.l_formula.cget("text"))
            if os.path.abspath(out) == os.path.abspath(video):
                return messagebox.showwarning("Word Repeater", "The result can't overwrite the original video.")
            info, err = self.estimate_now()
            if not info:
                return messagebox.showerror("Word Repeater", err or "Couldn't read those files.")
            total, src, words = info
            if self.is_tiny() and self.v_burn.get():
                return messagebox.showwarning("Word Repeater", "Tiny file can't burn subtitles onto the video. "
                                              "Untick the burn option or pick another quality.")
            if total > 3600 and not self.is_tiny() and not messagebox.askyesno(
                    "That's a long one",
                    "The result will be about %s long (the video is %s).\n\n"
                    "Rendering could take a very long time and the file could be huge. Continue?"
                    % (fmt_len(total), fmt_len(src))):
                return
            if os.path.exists(out) and not messagebox.askyesno(
                    "Word Repeater", "%s already exists. Replace it?" % os.path.basename(out)):
                return

            slow = "--compress" in self._args()
            first, second = ("Cutting clips", "Compressing video") if slow else ("Encoding video", "Building result")
            self.step_names = [first, second] + (["Burning subtitles"] if self.v_burn.get() else [])
            self.running = True
            self.b_start.configure(state="disabled")
            self.b_cancel.configure(state="normal")
            self.b_play.configure(state="disabled")
            self.b_folder.configure(state="disabled")
            self.bar["value"] = 0
            self._pct, self._step = 0.0, 0
            self.l_status.configure(text="Starting...")
            self.log.configure(state="normal")
            self.log.delete("1.0", "end")
            self.log.configure(state="disabled")

            argv = self._args()
            sink = Sink(lambda line: self.q.put(("line", line)))

            def work():
                result = run_engine(argv, sink)
                self.q.put(("done", result))

            threading.Thread(target=work, daemon=True).start()
            self.after(100, self.poll)

        def poll(self):
            last_progress = None
            finished = None
            try:
                while True:
                    kind, data = self.q.get_nowait()
                    if kind == "done":
                        finished = ("done", data)
                        break
                    m = PROG_RE.match(data)
                    if m:
                        last_progress = m
                    elif not EST_RE.match(data):
                        self._log(data)
            except queue.Empty:
                pass
            if last_progress:
                m = last_progress
                label = m.group("label").strip()
                word = label.lower().split()[0]
                if word == "burning":
                    step = len(self.step_names) - 1
                else:
                    step = {"encoding": 0, "cutting": 0, "building": 1, "rendering": 1,
                            "compressing": 1}.get(word, 0)
                step = min(step, len(self.step_names) - 1)
                pct = float(m.group("pct"))
                self._pct, self._step = pct, step
                self.bar["value"] = pct
                eta = m.group("eta")
                eta = "time left " + eta[4:] if eta.startswith("ETA ") and eta[4:] else eta
                self.l_status.configure(text="Step %d of %d: %s  -  %.0f%%  -  elapsed %s  -  %s"
                                        % (step + 1, len(self.step_names), label, pct,
                                           m.group("el"), eta))
            elif (self.running and not finished and getattr(self, "_pct", 0) >= 99.9
                  and getattr(self, "_step", 0) >= len(self.step_names) - 1):
                # the last step reached 100% but the file is still being written/moved
                self.l_status.configure(text="Finishing up: saving the file. Very large videos can take "
                                             "several minutes here - it isn't stuck.")
            if finished:
                self.finish(finished[1])
            else:
                self.after(150, self.poll)

        def finish(self, err):
            self.running = False
            self.b_start.configure(state="normal")
            self.b_cancel.configure(state="disabled")
            out = self.v_out.get()
            if err is None:
                self.bar["value"] = 100
                self.l_status.configure(text="Done! Saved to " + out)
                self.b_play.configure(state="normal")
                self.b_folder.configure(state="normal")
                messagebox.showinfo("Word Repeater", "All done!\n\nSaved to:\n" + out)
            elif err.startswith("Cancelled"):
                self.bar["value"] = 0
                self.l_status.configure(text="Cancelled.")
            else:
                self.l_status.configure(text="Something went wrong (details below).")
                self._log(err)
                messagebox.showerror("Word Repeater", err[:1500])

        def cancel(self):
            if self.running:
                self.l_status.configure(text="Cancelling...")
                engine.cancel()

        def play(self):
            open_path(self.v_out.get())

        def open_folder(self):
            open_path(os.path.dirname(os.path.abspath(self.v_out.get())))

        def on_close(self):
            if self.running:
                if not messagebox.askyesno("Word Repeater", "A video is still being made. Stop it and quit?"):
                    return
                engine.cancel()
            self.destroy()

    App().mainloop()


if __name__ == "__main__":
    main_window()
