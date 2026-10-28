# Belawril — Fast Word-and-Letter Video Looper

Belawril is a fast Windows desktop application that creates word-and-letter video loops using an SRT subtitle timeline.

## Features & Loop Rules
1. **Timestamp Preservation**: Uses exact word timestamps from `.srt` subtitle files.
2. **Letter Loops**:
   - Case-insensitive letter matching (excluding punctuation except apostrophes `'` and `’`).
   - For every letter occurrence, loops to its most recent previous occurrence in the subtitle sequence.
   - All letter loops for a word occur immediately before the word plays.
3. **Whole-Word Loops**:
   - Occurs when an exact word matches a previous occurrence.
   - Replays the previous word's letter loops + base clip without recursive nested whole-word loops.
4. **Fast Engine & Tiny File Mode**:
   - Fast mode re-uses encoded H.264 byte streams for ultra-fast rendering.
   - Tiny-file mode creates referencing MP4 sample tables.
5. **GUI Desktop Interface**:
   - Built with Python `tkinter` / `ttk`.
   - File pickers for video, subtitle (`.srt`), and output location.
   - Live timeline prediction showing output duration and counts for letter/word loops.
   - Short preview render option.

## How to Run on Windows

### Option 1: Running from Python
1. Ensure Python 3.9+ and FFmpeg are installed and available on `PATH`.
2. Run the application:
   ```cmd
   python belawril_app.py
   ```

### Option 2: Running Tests
Run tests with `pytest`:
```cmd
pytest
```

### Option 3: Building Standalone Windows Executable (.exe)
Double-click `build_exe.bat` or run in Command Prompt:
```cmd
build_exe.bat
```
The standalone executable will be located at `dist\Belawril\Belawril.exe`.
