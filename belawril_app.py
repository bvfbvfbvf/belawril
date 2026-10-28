#!/usr/bin/env python3
"""
Belawril - Fast Word-and-Letter Video Looper
Desktop GUI application.
"""

import os
import sys
import queue
import subprocess
import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

import simulator
import belawril_engine
import repeat_video as ref_engine


# Setup path for bundled/local ffmpeg
def setup_ffmpeg_path():
    dirs = []
    if getattr(sys, "frozen", False):
        dirs.append(getattr(sys, "_MEIPASS", ""))
        dirs.append(os.path.dirname(sys.executable))
    else:
        dirs.append(os.path.dirname(os.path.abspath(__file__)))
    dirs.append(os.path.expanduser("~/.local/bin"))
    os.environ["PATH"] = os.pathsep.join([d for d in dirs if d] + [os.environ.get("PATH", "")])


setup_ffmpeg_path()


class NullWriter:
    def write(self, s):
        return len(s or "")

    def flush(self):
        pass


if sys.stdout is None:
    sys.stdout = NullWriter()
if sys.stderr is None:
    sys.stderr = NullWriter()


RENDERING_MODES = {
    "Fastest (byte re-copy)": "fast",
    "Smaller file (compressed)": "compress",
    "Tiny file (instant reference)": "tiny",
}
DEFAULT_MODE = "Fastest (byte re-copy)"


def fmt_len(sec):
    if sec < 120:
        return f"{sec:.0f} seconds"
    m = sec / 60.0
    if m < 120:
        return f"{m:.1f} minutes"
    return f"{int(m // 60)} h {int(m % 60):02d} min"


def open_path(path):
    try:
        if sys.platform == "win32":
            os.startfile(path)
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])
    except Exception:
        pass


class BelawrilApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Belawril — Fast Word-and-Letter Video Looper")
        self.minsize(780, 680)

        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass

        try:
            ttk.Style(self).theme_use("vista" if sys.platform == "win32" else "clam")
        except tk.TclError:
            pass

        self.q = queue.Queue()
        self.running = False
        self.after_id = None
        self.out_is_auto = True

        self.v_video = tk.StringVar()
        self.v_srt = tk.StringVar()
        self.v_out = tk.StringVar()
        self.v_mode = tk.StringVar(value=DEFAULT_MODE)
        self.v_preview = tk.BooleanVar(value=False)

        self._build()

        for v in (self.v_video, self.v_srt):
            v.trace_add("write", lambda *a: self.on_change())
        self.protocol("WM_DELETE_WINDOW", self.on_close)

    def _build(self):
        pad = {"padx": 10, "pady": 6}
        root = ttk.Frame(self, padding=8)
        root.pack(fill="both", expand=True)
        root.columnconfigure(0, weight=1)

        # Section 1: File Pickers
        f1 = ttk.LabelFrame(root, text=" 1. Select Files ")
        f1.grid(row=0, column=0, sticky="ew", **pad)
        f1.columnconfigure(1, weight=1)

        rows = [
            ("Video file", self.v_video, self.browse_video),
            ("Subtitles (.srt)", self.v_srt, self.browse_srt),
            ("Save output as", self.v_out, self.browse_out),
        ]
        for i, (label, var, cmd) in enumerate(rows):
            ttk.Label(f1, text=label).grid(row=i, column=0, sticky="w", padx=8, pady=4)
            ttk.Entry(f1, textvariable=var).grid(row=i, column=1, sticky="ew", padx=4, pady=4)
            ttk.Button(f1, text="Browse...", command=cmd).grid(row=i, column=2, padx=8, pady=4)

        # Section 2: Belawril Looping Info & Settings
        f2 = ttk.LabelFrame(root, text=" 2. Looping Rules & Rendering Options ")
        f2.grid(row=1, column=0, sticky="ew", **pad)
        f2.columnconfigure(1, weight=1)

        ttk.Label(f2, text="Render mode:").grid(row=0, column=0, sticky="w", padx=8, pady=6)
        ttk.Combobox(f2, textvariable=self.v_mode, values=list(RENDERING_MODES.keys()),
                     state="readonly", width=30).grid(row=0, column=1, sticky="w", padx=4, pady=6)

        ttk.Checkbutton(f2, text="Short Preview mode (first 15 seconds)", variable=self.v_preview).grid(
            row=1, column=0, columnspan=2, sticky="w", padx=8, pady=4)

        ttk.Label(f2, justify="left", foreground="#555555", text=(
            "Belawril Loop Logic:\n"
            "• Every letter occurrence triggers a loop to its most recent previous occurrence.\n"
            "• Whole-word loops replay previous word's letter loops + clip (without nested word loops).\n"
            "• Input and intermediate render files are strictly retained."
        )).grid(row=2, column=0, columnspan=2, sticky="w", padx=8, pady=(4, 8))

        # Section 3: Timeline Predictions & Loop Event Statistics
        f3 = ttk.LabelFrame(root, text=" 3. Predicted Output & Loop Event Statistics ")
        f3.grid(row=2, column=0, sticky="ew", **pad)
        f3.columnconfigure(0, weight=1)

        self.l_est = ttk.Label(f3, text="Select a video and SRT file to calculate loop predictions.",
                               font=("Segoe UI", 10, "bold"))
        self.l_est.grid(row=0, column=0, sticky="w", padx=8, pady=6)

        self.l_counts = ttk.Label(f3, text="Letter loops: 0 | Whole-word loops: 0",
                                  foreground="#333333")
        self.l_counts.grid(row=1, column=0, sticky="w", padx=8, pady=(0, 6))

        # Section 4: Controls & Progress
        f4 = ttk.LabelFrame(root, text=" 4. Render Controls ")
        f4.grid(row=3, column=0, sticky="ew", **pad)
        f4.columnconfigure(0, weight=1)

        self.bar = ttk.Progressbar(f4, mode="indeterminate")
        self.bar.grid(row=0, column=0, columnspan=3, sticky="ew", padx=8, pady=6)

        self.l_status = ttk.Label(f4, text="Ready.")
        self.l_status.grid(row=1, column=0, columnspan=3, sticky="w", padx=8)

        btns = ttk.Frame(f4)
        btns.grid(row=2, column=0, columnspan=3, sticky="ew", padx=8, pady=8)

        self.b_start = ttk.Button(btns, text="Start Belawril Render", command=self.start)
        self.b_start.pack(side="left")

        self.b_cancel = ttk.Button(btns, text="Cancel", command=self.cancel, state="disabled")
        self.b_cancel.pack(side="left", padx=8)

        self.b_play = ttk.Button(btns, text="Play Result", command=self.play, state="disabled")
        self.b_play.pack(side="right")

        self.b_folder = ttk.Button(btns, text="Open Folder", command=self.open_folder, state="disabled")
        self.b_folder.pack(side="right", padx=8)

        # Log View
        self.log = tk.Text(root, height=6, state="disabled", wrap="word", font=("Consolas", 9))
        self.log.grid(row=4, column=0, sticky="nsew", padx=10, pady=(0, 6))
        root.rowconfigure(4, weight=1)

    def _log(self, text):
        self.log.configure(state="normal")
        self.log.insert("end", text + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def browse_video(self):
        p = filedialog.askopenfilename(title="Select Video File", filetypes=[
            ("Video files", "*.mp4 *.mov *.mkv *.avi *.webm *.m4v"), ("All files", "*.*")])
        if p:
            self.v_video.set(p)
            base = os.path.splitext(p)[0] + "_belawril.mp4"
            if self.out_is_auto or not self.v_out.get():
                self.v_out.set(base)
                self.out_is_auto = True

    def browse_srt(self):
        p = filedialog.askopenfilename(title="Select SRT Subtitle File", filetypes=[
            ("Subtitles", "*.srt *.vtt *.txt"), ("All files", "*.*")])
        if p:
            self.v_srt.set(p)

    def browse_out(self):
        p = filedialog.asksaveasfilename(title="Save Output Video As", defaultextension=".mp4",
                                         filetypes=[("MP4 video", "*.mp4")],
                                         initialfile=os.path.basename(self.v_out.get() or "belawril_output.mp4"))
        if p:
            self.v_out.set(p)
            self.out_is_auto = False

    def on_change(self):
        if self.running:
            return
        if self.after_id:
            self.after_cancel(self.after_id)
        self.after_id = self.after(400, self.refresh_predictions)

    def refresh_predictions(self):
        self.after_id = None
        if self.running:
            return
        video, srt = self.v_video.get(), self.v_srt.get()
        if not (os.path.isfile(video) and os.path.isfile(srt)):
            self.l_est.configure(text="Select a video and SRT file to calculate loop predictions.", foreground="")
            self.l_counts.configure(text="Letter loops: 0 | Whole-word loops: 0")
            return

        try:
            cues = simulator.parse_srt(srt)
            words = simulator.split_words(cues)
            if not words:
                self.l_est.configure(text="No valid word cues found in SRT.", foreground="#b00020")
                return

            fps, has_audio, vid_dur = ref_engine.probe(video)
            sim = simulator.TimelineSimulator(words, video_duration=vid_dur)
            res = sim.generate_timeline()

            total_dur = res["total_duration"]
            l_count = res["letter_loop_count"]
            w_count = res["word_loop_count"]

            self.l_est.configure(
                text=f"Predicted output duration: {fmt_len(total_dur)} (Original video: {fmt_len(vid_dur)})",
                foreground=""
            )
            self.l_counts.configure(text=f"Letter loops: {l_count} | Whole-word loops: {w_count}")
        except Exception as e:
            self.l_est.configure(text=f"Prediction error: {e}", foreground="#b00020")

    def start(self):
        video, srt, out = self.v_video.get(), self.v_srt.get(), self.v_out.get().strip()
        if not os.path.isfile(video):
            return messagebox.showwarning("Belawril", "Please select a valid video file.")
        if not os.path.isfile(srt):
            return messagebox.showwarning("Belawril", "Please select a valid SRT subtitle file.")
        if not out:
            return messagebox.showwarning("Belawril", "Please specify where to save the output file.")

        self.running = True
        self.b_start.configure(state="disabled")
        self.b_cancel.configure(state="normal")
        self.b_play.configure(state="disabled")
        self.b_folder.configure(state="disabled")
        self.bar.start()
        self.l_status.configure(text="Rendering Belawril video...")

        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")

        mode_key = RENDERING_MODES.get(self.v_mode.get(), "fast")
        is_preview = self.v_preview.get()

        def work():
            try:
                belawril_engine.run_belawril_render(
                    video_path=video,
                    srt_path=srt,
                    output_path=out,
                    mode=mode_key,
                    preview=is_preview,
                    preview_max_sec=15.0,
                    keep_temp=True  # Preserve original & intermediate files
                )
                self.q.put(("done", None))
            except Exception as e:
                self.q.put(("done", str(e)))

        threading.Thread(target=work, daemon=True).start()
        self.after(100, self.poll)

    def poll(self):
        finished = None
        try:
            while True:
                kind, data = self.q.get_nowait()
                if kind == "done":
                    finished = data
                    break
        except queue.Empty:
            pass

        if finished is not None or (finished is None and not self.running):
            self.finish(finished)
        else:
            self.after(150, self.poll)

    def finish(self, err):
        self.running = False
        self.bar.stop()
        self.b_start.configure(state="normal")
        self.b_cancel.configure(state="disabled")
        out = self.v_out.get()

        if err is None:
            self.l_status.configure(text=f"Done! Output saved to: {out}")
            self.b_play.configure(state="normal")
            self.b_folder.configure(state="normal")
            messagebox.showinfo("Belawril", f"Rendering completed successfully!\n\nSaved to:\n{out}")
        else:
            self.l_status.configure(text="Error occurred during rendering.")
            self._log(f"Error: {err}")
            messagebox.showerror("Belawril", f"Render error:\n{err}")

    def cancel(self):
        if self.running:
            self.l_status.configure(text="Cancelling...")
            ref_engine.cancel()

    def play(self):
        open_path(self.v_out.get())

    def open_folder(self):
        open_path(os.path.dirname(os.path.abspath(self.v_out.get())))

    def on_close(self):
        if self.running:
            if not messagebox.askyesno("Belawril", "Rendering in progress. Stop render and exit?"):
                return
            ref_engine.cancel()
        self.destroy()


def main():
    BelawrilApp().mainloop()


if __name__ == "__main__":
    main()
