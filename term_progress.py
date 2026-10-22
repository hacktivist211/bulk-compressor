import os
import shutil
import subprocess
import sys
import threading
import time

def fmt_bytes(n):
    try:
        n = float(n)
    except (TypeError, ValueError):
        return "N/A"
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(n) < 1024.0:
            return "%.1f %s" % (n, unit)
        n /= 1024.0
    return "%.1f PiB" % n


def fmt_seconds(seconds):
    if seconds is None:
        return "--:--"
    try:
        seconds = int(seconds)
    except (TypeError, ValueError):
        return "--:--"
    if seconds < 0 or seconds > 31449600:
        return "--:--"
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return "%d:%02d:%02d" % (hours, minutes, secs)
    return "%02d:%02d" % (minutes, secs)


def parse_timecode(value):
    try:
        parts = str(value).split(":")
        if len(parts) == 3:
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
        if len(parts) == 2:
            return int(parts[0]) * 60 + float(parts[1])
        return float(value)
    except Exception:
        return None


def parse_speed(value):
    try:
        return float(str(value).lower().rstrip("x").strip())
    except Exception:
        return None


def enable_ansi():
    if os.name == "nt":
        try:
            import ctypes
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.GetStdHandle(-11)
            mode = ctypes.c_uint32()
            if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
                kernel32.SetConsoleMode(handle, mode.value | 0x0004)
        except Exception:
            pass


class ProgressDisplay:
    def __init__(self, total_items, total_bytes=0, video_slots=1, tty=None):
        self.lock = threading.Lock()
        self.total_items = max(0, int(total_items))
        self.total_bytes = max(0, int(total_bytes or 0))
        self.done_items = 0
        self.done_bytes = 0
        self.saved_bytes = 0
        self.failed_items = 0
        self.status = ""
        self.slots = [None for _ in range(max(1, int(video_slots)))]
        self.started = time.time()
        self.lines_drawn = 0
        self.last_draw = 0.0
        self.last_plain = 0.0
        self.min_interval = 0.15
        self.finished = False
        self.tty = sys.stdout.isatty() if tty is None else bool(tty)
        self.width = shutil.get_terminal_size((100, 30)).columns if self.tty else 100
        self.filled = "="
        self.empty = "-"
        if self.tty:
            try:
                "█".encode(sys.stdout.encoding or "ascii")
                self.filled = "█"
                self.empty = "░"
            except Exception:
                pass

    def bar(self, fraction, width):
        try:
            fraction = float(fraction)
        except (TypeError, ValueError):
            fraction = 0.0
        fraction = min(1.0, max(0.0, fraction))
        filled = int(round(fraction * width))
        return self.filled * filled + self.empty * (width - filled)

    def claim_slot(self, label):
        with self.lock:
            for index, slot in enumerate(self.slots):
                if slot is None:
                    self.slots[index] = {"label": str(label), "fraction": 0.0,
                                         "speed": "", "eta": None}
                    return index
            self.slots.append({"label": str(label), "fraction": 0.0,
                               "speed": "", "eta": None})
            return len(self.slots) - 1

    def release_slot(self, index):
        with self.lock:
            if isinstance(index, int) and 0 <= index < len(self.slots):
                self.slots[index] = None
        self.redraw()

    def update_slot(self, index, fraction, speed="", eta=None):
        with self.lock:
            if isinstance(index, int) and 0 <= index < len(self.slots) and self.slots[index] is not None:
                self.slots[index]["fraction"] = fraction
                self.slots[index]["speed"] = speed
                self.slots[index]["eta"] = eta
        self.redraw()

    def advance(self, done_bytes=0, saved_bytes=0, failed=False, status=None):
        with self.lock:
            self.done_items += 1
            self.done_bytes += done_bytes or 0
            self.saved_bytes += saved_bytes or 0
            if failed:
                self.failed_items += 1
            if status is not None:
                self.status = str(status)
        self.redraw()

    def set_status(self, status):
        with self.lock:
            self.status = str(status)
        self.redraw()

    def render(self):
        elapsed = time.time() - self.started
        fraction = self.done_items / self.total_items if self.total_items else 0.0
        eta = elapsed * (1.0 - fraction) / fraction if fraction > 0.0001 else None
        pieces = [
            "TOTAL [%s] %3d%%" % (self.bar(fraction, 22), int(fraction * 100)),
            "%d/%d files" % (self.done_items, self.total_items),
        ]
        if self.total_bytes > 0:
            pieces.append("%s / %s" % (fmt_bytes(self.done_bytes), fmt_bytes(self.total_bytes)))
        pieces.append("saved %s" % fmt_bytes(self.saved_bytes))
        pieces.append("elapsed %s" % fmt_seconds(elapsed))
        pieces.append("ETA %s" % fmt_seconds(eta))
        if self.failed_items:
            pieces.append("errors %d" % self.failed_items)
        lines = [" ".join(pieces)]
        for index, slot in enumerate(self.slots):
            if slot is None:
                lines.append("  V%d waiting" % (index + 1))
                continue
            pct = int(min(1.0, max(0.0, slot["fraction"])) * 100)
            eta_text = fmt_seconds(slot["eta"]) if slot["eta"] is not None else ""
            lines.append("  V%d [%s] %3d%% %s %s %s" % (
                index + 1,
                self.bar(slot["fraction"], 22),
                pct,
                slot["speed"] or "",
                eta_text,
                slot["label"],
            ))
        lines.append("  %s" % (self.status or ""))
        return lines

    def plain_line(self):
        elapsed = time.time() - self.started
        fraction = self.done_items / self.total_items if self.total_items else 0.0
        eta = elapsed * (1.0 - fraction) / fraction if fraction > 0.0001 else None
        return "[%d/%d] %.1f%% saved %s elapsed %s ETA %s%s" % (
            self.done_items, self.total_items, fraction * 100,
            fmt_bytes(self.saved_bytes), fmt_seconds(elapsed), fmt_seconds(eta),
            (" " + self.status) if self.status else "")

    def redraw(self, force=False):
        if self.finished:
            return
        now = time.time()
        if not force and now - self.last_draw < self.min_interval:
            return
        with self.lock:
            self.last_draw = now
            if self.tty:
                chunks = []
                if self.lines_drawn:
                    chunks.append("\x1b[%dA" % self.lines_drawn)
                for line in self.render():
                    chunks.append("\r" + line[: self.width] + "\x1b[K\n")
                try:
                    sys.stdout.write("".join(chunks))
                    sys.stdout.flush()
                    self.lines_drawn = len(self.slots) + 2
                except Exception:
                    self.tty = False
            else:
                if force or now - self.last_plain >= 10:
                    self.last_plain = now
                    try:
                        sys.stdout.write(self.plain_line() + "\n")
                        sys.stdout.flush()
                    except Exception:
                        pass

    def finish(self):
        with self.lock:
            if self.finished:
                return
            self.finished = True
            if self.tty:
                chunks = []
                if self.lines_drawn:
                    chunks.append("\x1b[%dA" % self.lines_drawn)
                for line in self.render():
                    chunks.append("\r" + line[: self.width] + "\x1b[K\n")
                try:
                    sys.stdout.write("".join(chunks) + "\n")
                    sys.stdout.flush()
                except Exception:
                    pass
                self.lines_drawn = 0
            else:
                try:
                    sys.stdout.write(self.plain_line() + "\n")
                    sys.stdout.flush()
                except Exception:
                    pass


def run_subprocess(cmd, timeout=None, stop_event=None):
    if stop_event is None:
        return subprocess.run(
            cmd, capture_output=True, text=True, errors="replace", timeout=timeout, check=False)
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, errors="replace")
    deadline = None if timeout is None else time.monotonic() + timeout
    while True:
        try:
            stdout, stderr = proc.communicate(timeout=0.2)
            return subprocess.CompletedProcess(cmd, proc.returncode, stdout, stderr)
        except subprocess.TimeoutExpired:
            if stop_event.is_set():
                proc.terminate()
                try:
                    stdout, stderr = proc.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    stdout, stderr = proc.communicate()
                raise InterruptedError("subprocess cancelled")
            if deadline is not None and time.monotonic() >= deadline:
                proc.kill()
                stdout, stderr = proc.communicate()
                raise subprocess.TimeoutExpired(
                    cmd, timeout, output=stdout, stderr=stderr)


def run_ffmpeg_progress(cmd, duration=None, display=None, slot=None, timeout=21600,
                        stop_event=None):
    if not cmd:
        return False, "empty command"
    full = list(cmd[:-1]) + ["-progress", "pipe:1", "-nostats", str(cmd[-1])]
    try:
        proc = subprocess.Popen(
            full, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, errors="replace")
    except Exception as e:
        return False, str(e)

    stderr_tail = []
    state = {"seconds": 0.0, "speed": ""}

    def drain_stderr():
        try:
            for line in proc.stderr:
                stderr_tail.append(line)
                if len(stderr_tail) > 200:
                    del stderr_tail[:100]
        except Exception:
            pass

    def handle_line(raw):
        line = raw.strip()
        if "=" not in line:
            return
        key, value = line.split("=", 1)
        if key == "out_time_us":
            try:
                state["seconds"] = max(state["seconds"], int(value) / 1000000.0)
            except ValueError:
                pass
        elif key == "out_time":
            parsed = parse_timecode(value)
            if parsed is not None:
                state["seconds"] = max(state["seconds"], parsed)
        elif key == "speed":
            state["speed"] = value
        if display is not None and slot is not None and duration and duration > 0:
            seconds = state["seconds"]
            speed = parse_speed(state["speed"])
            eta = None
            if speed and speed > 0 and seconds > 0:
                eta = (duration - seconds) / speed
            display.update_slot(slot, seconds / duration, state["speed"], eta)

    def drain_stdout():
        try:
            for line in proc.stdout:
                handle_line(line)
        except Exception:
            pass

    err_thread = threading.Thread(target=drain_stderr, daemon=True)
    out_thread = threading.Thread(target=drain_stdout, daemon=True)
    err_thread.start()
    out_thread.start()

    deadline = time.monotonic() + (timeout or 21600)
    while proc.poll() is None:
        if stop_event is not None and stop_event.is_set():
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
            out_thread.join(timeout=5)
            err_thread.join(timeout=5)
            return False, "interrupted"
        if time.monotonic() > deadline:
            proc.kill()
            try:
                proc.wait(timeout=15)
            except Exception:
                pass
            out_thread.join(timeout=2)
            err_thread.join(timeout=2)
            return False, "ffmpeg timed out"
        time.sleep(0.1)

    out_thread.join(timeout=5)
    err_thread.join(timeout=5)
    if proc.returncode != 0:
        return False, "".join(stderr_tail)[-600:].strip()
    return True, None
