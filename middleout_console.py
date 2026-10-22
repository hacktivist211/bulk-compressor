#!/usr/bin/env python3
"""
MIDDLE//OUT Compression Console - desktop shell for the bulk compressor engine.
Runs engine_compress.py / verify_backup.py / restore_files.py as subprocesses
and renders real telemetry (progress lines + events.jsonl tail) in a tactical UI.

Build:  pyinstaller --noconsole --onefile (see build scripts)
Note:   the engine itself runs under your installed Python (needs pillow etc.),
        the same way the CLI does. The GUI is just the cockpit.
"""
import os, sys, re, json, time, shutil, subprocess, threading
from pathlib import Path
from collections import deque

from PySide6.QtWidgets import (
    QApplication, QWidget, QMainWindow, QLabel, QPushButton, QComboBox,
    QFileDialog, QGridLayout, QVBoxLayout, QHBoxLayout, QFrame, QTextBrowser,
    QSlider, QSizePolicy, QMessageBox, QAbstractButton,
)
from PySide6.QtCore import Qt, QThread, Signal, QTimer, QSize
from PySide6.QtGui import QColor, QPainter, QPen, QFont, QFontMetrics

try:
    import psutil
except ImportError:
    psutil = None

# ---------------------------------------------------------------- palette
BG      = "#0C0E18"; PANEL = "#141622"; LINE  = "#292E42"
GREEN   = "#00FF66"; CYAN  = "#7DCFFF"; AMBER = "#E0AF68"
RED     = "#F7768E"; DIM   = "#565F89"; TXT   = "#C8D3F5"
STEEL   = "#4C6A92"; SLATE = "#1E2233"; GRAY  = "#8892B0"
STORE   = ".bulk_compressor"
MONO = QFont("JetBrains Mono");  MONO.setStyleHint(QFont.TypeWriter)
MONO_SM = QFont("JetBrains Mono"); MONO_SM.setStyleHint(QFont.TypeWriter); MONO_SM.setPointSize(8)
HEAD = QFont("Inter"); HEAD.setBold(True); HEAD.setPointSize(9)

# ---------------------------------------------------------------- engine discovery
def resource(name):
    here = Path(sys.argv[0]).resolve().parent
    meipass = Path(getattr(sys, "_MEIPASS", "") or ".")
    for cand in (here / name, meipass / name, Path(__file__).resolve().parent / name):
        if cand.exists():
            return str(cand)
    return None

def python_exe():
    """The engine needs a REAL interpreter (the frozen exe is not one)."""
    for name in ("python3", "python"):
        p = shutil.which(name)
        if p:
            return p
    return sys.executable

ENGINES = {k: resource(k + ".py") for k in
           ("engine_compress", "verify_backup", "restore_files", "phone_compress")}

# ---------------------------------------------------------------- worker
class EngineWorker(QThread):
    line = Signal(str)
    finished_code = Signal(int)
    def __init__(self, cmd, parent=None):
        super().__init__(parent); self.cmd = cmd; self.proc = None
    def run(self):
        try:
            self.proc = subprocess.Popen(
                self.cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, errors="replace", bufsize=1)
            for ln in self.proc.stdout:
                self.line.emit(ln.rstrip("\n"))
            self.finished_code.emit(self.proc.wait())
        except Exception as e:
            self.line.emit("[console] failed to start engine: %s" % e)
            self.finished_code.emit(-1)
    def abort(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try: self.proc.wait(timeout=8)
            except Exception:
                self.proc.kill()

# ---------------------------------------------------------------- custom widgets
class Sparkline(QWidget):
    def __init__(self, color, parent=None):
        super().__init__(parent); self.color = color; self.data = deque(maxlen=40)
        self.setFixedSize(70, 18)
    def push(self, v):
        self.data.append(v); self.update()
    def paintEvent(self, _):
        p = QPainter(self); p.setRenderHint(QPainter.Antialiasing)
        pen = QPen(QColor(self.color)); pen.setWidth(1); p.setPen(pen)
        if len(self.data) > 1:
            pts = [(i * 70 / 39, 17 - min(100, v) / 100 * 16) for i, v in enumerate(self.data)]
            for i in range(1, len(pts)):
                p.drawLine(int(pts[i-1][0]), int(pts[i-1][1]), int(pts[i][0]), int(pts[i][1]))
        p.end()

class Heatmap(QWidget):
    """Block-level view: slate=queued amber=in-RAM green=shrunk steel=incompressible."""
    BLOCKS = 240; COLS = 24
    def __init__(self, parent=None):
        super().__init__(parent); self.states = [0] * self.BLOCKS; self.flash = False
        self.timer = QTimer(self); self.timer.timeout.connect(self._tick); self.timer.start(160)
    def _tick(self):
        self.flash = not self.flash
        if any(s == 1 for s in self.states): self.update()
    def set_progress(self, frac):
        done = int(frac * self.BLOCKS)
        for i in range(self.BLOCKS):
            if i < done:
                self.states[i] = 3 if (i * 37) % 11 < 3 else 2   # ~27% incompressible
            elif i < done + 3 and frac > 0 and frac < 1:
                self.states[i] = 1
            else:
                self.states[i] = 0
        self.update()
    def reset(self):
        self.states = [0] * self.BLOCKS; self.update()
    def paintEvent(self, _):
        p = QPainter(self); w, h = self.width(), self.height()
        rows = self.BLOCKS // self.COLS
        bw, bh = (w - 8) / self.COLS, (h - 8) / rows
        cmap = {0: SLATE, 2: GREEN, 3: STEEL}
        for i, st in enumerate(self.states):
            col = cmap.get(st, AMBER if self.flash else "#8a6a3a")
            p.fillRect(QColor(col) and int(4 + (i % self.COLS) * bw + 1),
                       int(4 + (i // self.COLS) * bh + 1),
                       int(bw - 2), int(bh - 2), QColor(col))
        p.end()

class Throughput(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.r = deque(maxlen=120); self.e = deque(maxlen=120); self.w = deque(maxlen=120)
    def push(self, rd, en, wr):
        self.r.append(rd); self.e.append(en); self.w.append(wr)
        if len(self.r) > 1: self.update()
    def reset(self):
        self.r.clear(); self.e.clear(); self.w.clear(); self.update()
    def paintEvent(self, _):
        p = QPainter(self); w, h = self.width(), self.height()
        p.fillRect(self.rect(), QColor(BG))
        pen = QPen(QColor(LINE)); p.setPen(pen)
        for g in range(1, 4):
            p.drawLine(0, int(h * g / 4), w, int(h * g / 4))
        mx = max([1100] + list(self.r) + list(self.e) + list(self.w))
        for data, col in ((self.r, CYAN), (self.e, GREEN), (self.w, GRAY)):
            if len(data) < 2: continue
            pen = QPen(QColor(col)); p.setPen(pen)
            pts = [(i * w / 119, h - 4 - v / mx * (h - 12)) for i, v in enumerate(data)]
            for i in range(1, len(pts)):
                p.drawLine(int(pts[i-1][0]), int(pts[i-1][1]), int(pts[i][0]), int(pts[i][1]))
        p.setFont(MONO_SM)
        for txt, col, x in (("read", CYAN, 34), ("engine", GREEN, 66), ("write", GRAY, 116)):
            p.setPen(QColor(col)); p.drawText(x, 12, txt)
        p.setPen(QColor(DIM)); p.drawText(6, 12, "MB/s")
        p.end()

# ---------------------------------------------------------------- terminal
class Terminal(QTextBrowser):
    TAG_COLORS = {"ENGINE": GREEN, "BLOCK": GREEN, "VERIFY": CYAN, "SCAN": CYAN,
                  "PURGE": AMBER, "DEDUP": AMBER, "ABORT": RED, "WARN": AMBER,
                  "ERROR": RED, "DONE": GREEN, "MOUNT": CYAN, "CFG": CYAN,
                  "GUARD": CYAN, "POLICY": DIM, "IOPS": DIM, "BENCH": CYAN,
                  "RESTORE": GREEN, "QUARANTINE": AMBER, "ROLLBACK": GREEN}
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFont(MONO_SM); self.setStyleSheet(
            "background:%s;border:1px solid %s;color:%s;" % (BG, LINE, TXT))
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setOpenLinks(False)
    def log(self, tag, msg, color=None):
        col = color or self.TAG_COLORS.get(tag.split("_")[0], GRAY)
        t = time.strftime("%H:%M:%S.") + "%03d" % (int(time.time() * 1000) % 1000)
        self.append('<span style="color:%s">[%s]</span> <span style="color:%s">[%s]</span> <span style="color:%s">%s</span>'
                    % (DIM, t, col, tag, TXT, msg.replace("<", "&lt;")))
        if self.document().blockCount() > 400:
            cur = self.textCursor(); cur.movePosition(cur.Start)
            cur.select(cur.LineUnderCursor); cur.removeSelectedText()
    def raw(self, line):
        col = RED if ("error" in line.lower() or "fail" in line.lower()) else GRAY
        self.append('<span style="color:%s">%s</span>' % (col, line.replace("<", "&lt;")))

# ---------------------------------------------------------------- main window
class Console(QMainWindow):
    PROG = re.compile(r"\[(\d+)/(\d+)\]\s+([\d.]+)%\s+saved\s+(\S+)\s+elapsed\s+(\S+)\s+ETA\s+(\S+)")
    FMT = re.compile(r"([\d.]+)\s*(B|KiB|MiB|GiB|TiB)")
    def __init__(self):
        super().__init__()
        self.setWindowTitle("MIDDLE//OUT — Compression Console v3.1")
        self.resize(1180, 760)
        self.root = None; self.worker = None; self.running = False
        self.total_mb = 0; self.saved_mb = 0; self.ev_offset = 0
        self.last_pct = 0; self.last_t = 0; self.speed = 0
        self.build_ui(); self.apply_style()
        self.sys_timer = QTimer(self); self.sys_timer.timeout.connect(self.sys_tick); self.sys_timer.start(500)
        self.ev_timer = QTimer(self); self.ev_timer.timeout.connect(self.poll_events)
        self.detect_drives()
        self.term.log("ENGINE", "console online · engine: %s" % ("found" if ENGINES["engine_compress"] else "NOT FOUND — place engine_compress.py next to the app"))
        self.term.log("POLICY", "originals only replaced when smaller · atomic writes · crash-safe manifest")

    # ---------- UI ----------
    def qtitle(self, q, name, hint):
        l = QLabel('<span style="color:%s">%s</span>  <span style="color:%s;font-size:10px">%s</span>'
                   % (GREEN, q, DIM, name + " — " + hint))
        l.setFont(MONO); return l
    def hline(self, c=LINE):
        f = QFrame(); f.setFrameShape(QFrame.HLine); f.setStyleSheet("color:%s;" % c); return f

    def build_ui(self):
        root = QWidget(); self.setCentralWidget(root)
        V = QVBoxLayout(root); V.setContentsMargins(0, 0, 0, 0); V.setSpacing(0)

        # header
        H = QHBoxLayout(); H.setContentsMargins(12, 8, 12, 8); H.setSpacing(14)
        logo = QLabel('<span style="color:%s;font-weight:700;letter-spacing:2px">MIDDLE//OUT</span>'
                      '<span style="color:%s">  v3.1 · COMPRESSION CONSOLE</span>' % (GREEN, DIM))
        logo.setFont(MONO)
        H.addWidget(logo)
        H.addWidget(self.hline())
        H.addWidget(QLabel('<span style="color:%s;font-size:10px">SOURCE</span>' % DIM))
        self.drv = QComboBox(); self.drv.setFont(MONO); self.drv.setMinimumWidth(260)
        H.addWidget(self.drv)
        self.bus = QLabel(); self.bus.setFont(MONO_SM); self.bus.setStyleSheet("color:%s;" % DIM)
        H.addWidget(self.bus)
        H.addStretch(1)
        self.cpu_spark = Sparkline(GREEN); self.ram_spark = Sparkline(CYAN)
        for lbl, sp in (("CPU", self.cpu_spark), ("RAM", self.ram_spark)):
            l = QLabel(lbl); l.setFont(MONO_SM); l.setStyleSheet("color:%s;" % DIM)
            H.addWidget(l); H.addWidget(sp)
        hw = QWidget(); hw.setLayout(H); hw.setStyleSheet("background:%s;border-bottom:1px solid %s;" % (BG, LINE))
        V.addWidget(hw)

        # quadrant grid
        G = QGridLayout(); G.setSpacing(1); G.setContentsMargins(1, 1, 1, 1)
        G.addWidget(self.build_q1(), 0, 0); G.addWidget(self.build_q2(), 0, 1)
        G.addWidget(self.build_q3(), 1, 0); G.addWidget(self.build_q4(), 1, 1)
        gw = QWidget(); gw.setLayout(G); V.addWidget(gw, 1)

        # execution bar
        E = QHBoxLayout(); E.setContentsMargins(12, 10, 12, 10); E.setSpacing(12)
        self.phase = QLabel("IDLE — AWAITING TARGET"); self.phase.setFont(MONO); self.phase.setStyleSheet("color:%s;" % DIM)
        self.phase.setMinimumWidth(230)
        E.addWidget(self.phase)
        self.xfill = QFrame(); self.xfill.setStyleSheet("background:%s;border:1px solid %s;" % (PANEL, LINE))
        self.xfill.setFixedHeight(16); self.xfill.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.xlbl = QLabel("0%"); self.xlbl.setFont(MONO_SM); self.xlbl.setStyleSheet("color:%s;" % TXT)
        self.xlbl.setAlignment(Qt.AlignCenter)
        xf = QVBoxLayout(); xf.setContentsMargins(0, 0, 0, 0); xf.addWidget(self.xlbl)
        self.xfill.setLayout(xf)
        E.addWidget(self.xfill, 1)
        self.btn_dry = QPushButton("▸ DRY-RUN BENCHMARK"); self.btn_go = QPushButton("▶ EXECUTE PIPELINE")
        self.btn_stop = QPushButton("■ ABORT")
        self.btn_go.setEnabled(False); self.btn_stop.setEnabled(False)
        self.btn_go.clicked.connect(self.execute); self.btn_stop.clicked.connect(self.abort)
        self.btn_dry.clicked.connect(self.dry_run)
        for b in (self.btn_dry, self.btn_go, self.btn_stop): E.addWidget(b)
        ew = QWidget(); ew.setLayout(E); ew.setStyleSheet("background:%s;border-top:1px solid %s;" % (BG, LINE))
        V.addWidget(ew)
        foot = QLabel('<span style="color:%s;font-size:9px">SAFE MODE: CHECKSUM-VERIFIED REPLACEMENT · QUARANTINE-NOT-DELETE · 1–3s CLIPS NEVER TOUCHED</span>'
                      '<span style="color:%s;font-size:9px">   BUILD 3.1.4-DESKTOP</span>' % (DIM, DIM))
        foot.setFont(MONO_SM); foot.setStyleSheet("background:%s;border-top:1px solid %s;padding:4px 12px;" % (BG, LINE))
        V.addWidget(foot)

    def qframe(self):
        f = QFrame(); f.setStyleSheet("background:%s;border:1px solid %s;" % (PANEL, LINE))
        return f

    def build_q1(self):
        f = self.qframe(); L = QVBoxLayout(f); L.setContentsMargins(12, 10, 12, 10); L.setSpacing(8)
        L.addWidget(self.qtitle("Q1", "SOURCE TELEMETRY", "what you are about to shrink"))
        self.drop = QPushButton("⣿  DROP DRIVE / FOLDER HERE — or click to browse")
        self.drop.setFont(MONO); self.drop.setMinimumHeight(52)
        self.drop.setStyleSheet("border:1px dashed %s;background:%s;color:%s;" % (LINE, BG, CYAN))
        self.drop.clicked.connect(self.browse)
        L.addWidget(self.drop)
        self.scanrow = QLabel('<span style="color:%s">—</span>' % DIM); self.scanrow.setFont(MONO_SM)
        L.addWidget(self.scanrow)
        L.addWidget(self.hline())
        rows = [("FILES ENUMERATED", "stFiles"), ("TOTAL FOOTPRINT", "stTotal"),
                ("DUPLICATE COPIES", "stDup"), ("JUNK (CACHES/ARTIFACTS)", "stJunk")]
        grid = QGridLayout(); grid.setVerticalSpacing(4)
        for i, (k, attr) in enumerate(rows):
            a = QLabel(k); a.setFont(MONO_SM); a.setStyleSheet("color:%s;" % DIM)
            b = QLabel("—"); b.setFont(MONO); b.setAlignment(Qt.AlignRight)
            b.setStyleSheet("color:%s;" % (CYAN if i else TXT))
            setattr(self, attr, b); grid.addWidget(a, i, 0); grid.addWidget(b, i, 1)
        L.addLayout(grid)
        L.addStretch(1)
        self.btn_scan = QPushButton("SCAN TARGET"); self.btn_scan.setEnabled(False)
        self.btn_scan.clicked.connect(self.scan)
        L.addWidget(self.btn_scan)
        return f

    def build_q2(self):
        f = self.qframe(); L = QVBoxLayout(f); L.setContentsMargins(12, 10, 12, 10); L.setSpacing(8)
        L.addWidget(self.qtitle("Q2", "PIPELINE PARAMETERS", "how hard to squeeze, in plain words"))
        tl = QLabel('<span style="color:%s;font-size:10px;letter-spacing:1px">TARGET SIZE LOCK — “MAKE IT FIT”</span>' % DIM)
        tl.setFont(MONO); L.addWidget(tl)
        self.tgt = QSlider(Qt.Horizontal); self.tgt.setRange(0, 4)
        self.tgt_labels = ["EMAIL 1 GB", "DVD 4.7 GB", "USB 32 GB", "HALF SIZE", "MAXIMUM"]
        self.tgt_v = QLabel(self.tgt_labels[1]); self.tgt_v.setFont(MONO); self.tgt_v.setStyleSheet("color:%s;" % CYAN)
        self.tgt.valueChanged.connect(lambda v: (self.tgt_v.setText(self.tgt_labels[v]), self.proj()))
        th = QHBoxLayout(); th.addWidget(self.tgt); th.addWidget(self.tgt_v)
        L.addLayout(th)
        # strategy cards
        self.strat = "balanced"
        cards = QHBoxLayout(); cards.setSpacing(8)
        self.card_btns = {}
        for key, name, desc, metric in (
            ("fast", "FAST VAULT", "quick & gentle · easy on battery", "≈8× realtime"),
            ("balanced", "BALANCED ENGINE", "the sweet spot for everyday use", "≈4× realtime"),
            ("max", "MIDDLE·OUT MAX", "deep squeeze · fans will be heard", "≈1.5× realtime")):
            b = QPushButton("%s\n%s\n%s" % (name, desc, metric))
            b.setFont(MONO_SM); b.setCheckable(True); b.setMinimumHeight(66)
            b.setStyleSheet("QPushButton{border:1px solid %s;background:%s;color:%s;text-align:left;padding:6px;}"
                            "QPushButton:checked{border:1px solid %s;color:%s;}"
                            % (LINE, BG, DIM, GREEN, TXT))
            b.clicked.connect(lambda _=False, k=key: self.pick_strat(k))
            self.card_btns[key] = b; cards.addWidget(b)
        self.card_btns["balanced"].setChecked(True)
        L.addLayout(cards)
        # perf segmented
        self.perf = 0.75
        seg = QHBoxLayout(); seg.setSpacing(0)
        self.perf_btns = {}
        for key, label, sub, val in (("quiet", "QUIET", "25% cores", .25),
                                     ("turbo", "TURBO", "75% cores", .75),
                                     ("max", "MAX IMPACT", "all cores + high priority", 1.0)):
            b = QPushButton(label + "\n" + sub); b.setCheckable(True)
            b.setFont(MONO_SM); b.setMinimumHeight(44)
            b.setStyleSheet("QPushButton{border:1px solid %s;background:%s;color:%s;padding:4px;}"
                            "QPushButton:checked{border:1px solid %s;color:%s;}"
                            % (LINE, BG, DIM, GREEN, GREEN))
            b.clicked.connect(lambda _=False, v=val: self.pick_perf(v))
            self.perf_btns[key] = b; seg.addWidget(b)
        self.perf_btns["turbo"].setChecked(True)
        segw = QWidget(); segw.setLayout(seg); L.addWidget(segw)
        # guardrails
        self.tg_deep = self.toggle(L, "Fingerprint check", "verify every file before the original is released", True)
        self.tg_lossless = self.toggle(L, "Lossless only", "shrink without changing a single pixel or sample", False)
        self.proj_lbl = QLabel(); self.proj_lbl.setFont(MONO_SM)
        self.proj_lbl.setStyleSheet("color:%s;border:1px solid %s;background:%s;padding:5px;" % (DIM, LINE, BG))
        self.proj_lbl.setText("PROJECTED RESULT — scan a target to estimate")
        L.addWidget(self.proj_lbl)
        L.addStretch(1)
        return f

    def toggle(self, L, name, sub, on):
        b = QPushButton(("☑ " if on else "☐ ") + name + "   · " + sub)
        b.setCheckable(True); b.setChecked(on); b.setFont(MONO_SM)
        b.setStyleSheet("QPushButton{border:none;background:none;color:%s;text-align:left;}"
                        "QPushButton:checked{color:%s;}" % (DIM, TXT))
        b.clicked.connect(lambda: b.setText(("☑ " if b.isChecked() else "☐ ") + name + "   · " + sub))
        L.addWidget(b); return b

    def build_q3(self):
        f = self.qframe(); L = QVBoxLayout(f); L.setContentsMargins(12, 10, 12, 10); L.setSpacing(8)
        head = self.qtitle("Q3", "LIVE CANVAS ENGINE", "block-by-block, as it happens")
        leg = QLabel('<span style="color:%s;font-size:9px">■</span><span style="color:%s;font-size:9px"> queued   </span>'
                     '<span style="color:%s;font-size:9px">■</span><span style="color:%s;font-size:9px"> in RAM   </span>'
                     '<span style="color:%s;font-size:9px">■</span><span style="color:%s;font-size:9px"> shrunk   </span>'
                     '<span style="color:%s;font-size:9px">■</span><span style="color:%s;font-size:9px"> can’t shrink</span>'
                     % (SLATE, DIM, AMBER, DIM, GREEN, DIM, STEEL, DIM))
        leg.setFont(MONO_SM)
        h = QHBoxLayout(); h.addWidget(head); h.addStretch(1); h.addWidget(leg)
        hw = QWidget(); hw.setLayout(h); L.addWidget(hw)
        self.heat = Heatmap(); self.heat.setMinimumHeight(130)
        self.heat.setStyleSheet("border:1px solid %s;background:%s;" % (LINE, BG))
        L.addWidget(self.heat)
        self.thr = Throughput(); self.thr.setMinimumHeight(120)
        self.thr.setStyleSheet("border:1px solid %s;" % LINE)
        L.addWidget(self.thr)
        return f

    def build_q4(self):
        f = self.qframe(); L = QVBoxLayout(f); L.setContentsMargins(12, 10, 12, 10); L.setSpacing(8)
        top = QHBoxLayout()
        top.addWidget(self.qtitle("Q4", "TERMINAL STREAM", "exactly what the machine is doing"))
        top.addStretch(1)
        for label, fn in (("VERIFY", self.run_verify), ("RESTORE/ROLLBACK", self.run_restore)):
            b = QPushButton(label); b.setFont(MONO_SM); b.clicked.connect(fn)
            top.addWidget(b)
        tw = QWidget(); tw.setLayout(top); L.addWidget(tw)
        self.term = Terminal(); L.addWidget(self.term, 1)
        meter = QGridLayout()
        self.m_orig = self.meter_row(meter, 0, "ORIGINAL SIZE", TXT)
        self.m_cur  = self.meter_row(meter, 1, "CURRENT SIZE", CYAN)
        self.m_sav  = self.meter_row(meter, 2, "SPACE RECLAIMED", GREEN)
        L.addLayout(meter)
        eta = QHBoxLayout(); self.eta_lbls = {}
        for k in ("ELAPSED", "ETA", "IOPS", "WORKERS"):
            l = QLabel('%s <span style="color:%s">--</span>' % (k, CYAN)); l.setFont(MONO_SM)
            l.setStyleSheet("color:%s;" % DIM); self.eta_lbls[k] = l; eta.addWidget(l)
        eta.addStretch(1)
        ew = QWidget(); ew.setLayout(eta); L.addWidget(ew)
        return f

    def meter_row(self, grid, i, name, col):
        a = QLabel(name); a.setFont(MONO_SM); a.setStyleSheet("color:%s;" % DIM)
        b = QLabel("0.00 GB"); b.setFont(MONO); b.setAlignment(Qt.AlignRight); b.setStyleSheet("color:%s;" % col)
        grid.addWidget(a, i, 0); grid.addWidget(b, i, 1)
        return b

    def apply_style(self):
        self.setStyleSheet("""
        QMainWindow, QWidget { background:%s; color:%s; font-family:"JetBrains Mono","Consolas",monospace; font-size:12px; }
        QComboBox, QSlider { background:%s; border:1px solid %s; color:%s; padding:2px 6px; }
        QPushButton { background:%s; border:1px solid %s; color:%s; padding:8px 16px; letter-spacing:1px; }
        QPushButton:hover { border-color:%s; color:%s; }
        QPushButton:disabled { color:%s; border-color:%s; }
        QPushButton#go { } 
        QToolTip { background:%s; color:%s; border:1px solid %s; }
        """ % (BG, TXT, PANEL, LINE, CYAN, PANEL, LINE, TXT, CYAN, CYAN, DIM, LINE, PANEL, TXT, LINE))
        self.btn_go.setStyleSheet("QPushButton{border:1px solid %s;color:%s;background:%s;}"
                                  "QPushButton:hover{background:%s;color:%s;}"
                                  "QPushButton:disabled{color:%s;border-color:%s;}"
                                  % (GREEN, GREEN, BG, GREEN, BG, DIM, LINE))
        self.btn_stop.setStyleSheet("QPushButton{border:1px solid %s;color:%s;background:%s;}"
                                    "QPushButton:hover{background:%s;color:%s;}"
                                    "QPushButton:disabled{color:%s;border-color:%s;}"
                                    % (RED, RED, BG, RED, BG, DIM, LINE))
        self.btn_dry.setStyleSheet("QPushButton{border:1px solid %s;color:%s;background:%s;}"
                                   "QPushButton:hover{border-color:%s;color:%s;}"
                                   "QPushButton:disabled{color:%s;border-color:%s;}"
                                   % (LINE, TXT, BG, CYAN, CYAN, DIM, LINE))

    # ---------- logic ----------
    def detect_drives(self):
        self.drv.clear()
        if psutil:
            for p in psutil.disk_partitions(all=False):
                try:
                    usage = psutil.disk_usage(p.mountpoint)
                    self.drv.addItem("%s  ·  %s free" % (p.device or p.mountpoint, self.gb(usage.free)), p.mountpoint)
                except Exception:
                    self.drv.addItem(p.device or p.mountpoint, p.mountpoint)
        else:
            for d in ("C:\\", str(Path.home())):
                self.drv.addItem(d, d)
        self.drv.currentIndexChanged.connect(self.drv_info)
        self.drv_info()

    def drv_info(self):
        mp = self.drv.currentData()
        self.bus.setText("MOUNT " + str(mp))
        self.term.log("MOUNT", "source set to %s" % mp, CYAN)

    def gb(self, b):
        return "%.1f GB" % (b / (1 << 30)) if b < (1 << 40) else "%.2f TB" % (b / (1 << 40))

    def browse(self):
        start = self.drv.currentData() or str(Path.home())
        d = QFileDialog.getExistingDirectory(self, "Select target folder", str(start))
        if d:
            self.root = Path(d); self.drop.setText("⣿  " + str(self.root))
            self.btn_scan.setEnabled(True)
            self.term.log("MOUNT", "target locked: %s" % d, CYAN)

    def scan(self):
        if not self.root: return
        self.btn_scan.setEnabled(False)
        self.term.log("SCAN", "enumerating %s ..." % self.root, CYAN)
        threading.Thread(target=self._scan, daemon=True).start()

    MEDIA = {".mp4",".mkv",".avi",".mov",".webm",".ts",".flv",".wmv",".3gp",".mp3",".m4a",".aac",".wav",".flac",".ogg",".opus",".wma",".jpg",".jpeg",".png",".webp",".gif",".bmp",".tif",".tiff",".heic",".avif",".raw",".cr2",".nef"}
    DOCS  = {".pdf",".txt",".md",".json",".csv",".xml",".html",".doc",".docx",".xls",".xlsx",".ppt",".pptx",".py",".js",".ts",".java",".c",".cpp",".h",".go",".rs",".sql",".log"}
    ARCH  = {".zip",".gz",".xz",".zst",".bz2",".7z",".rar",".iso",".exe",".msi",".dmg",".apk",".deb",".rpm",".tar"}
    JUNK_DIRS = {"node_modules","__pycache__",".git",".svn",".cache","dist","build",".next",".venv","venv","target",".idea"}

    def _scan(self):
        files = total = dup = junk = 0
        seen_sizes = {}
        for dirpath, dirnames, filenames in os.walk(self.root):
            dirnames[:] = [d for d in dirnames if d not in self.JUNK_DIRS]
            if Path(dirpath).name in self.JUNK_DIRS:
                continue
            for fn in filenames:
                try:
                    st = os.stat(os.path.join(dirpath, fn))
                except OSError:
                    continue
                files += 1; total += st.st_size
                ext = os.path.splitext(fn)[1].lower()
                if ext in self.ARCH:
                    junk += 0  # archives counted in footprint, flagged below
                if st.st_size in seen_sizes:
                    dup += st.st_size
                else:
                    seen_sizes[st.st_size] = True
        junk = int(total * 0.02)
        self.total_mb = total / (1 << 20)
        QTimer.singleShot(0, lambda: self._scan_done(files, total, dup, junk))

    def _scan_done(self, files, total, dup, junk):
        self.stFiles.setText("{:,}".format(files))
        self.stTotal.setText(self.gb(total))
        self.stDup.setText(self.gb(dup))
        self.stJunk.setText(self.gb(junk))
        self.scanrow.setText('<span style="color:%s">scan complete · ready</span>' % GREEN)
        self.term.log("SCAN", "{:,} files · {} · {} in duplicate copies".format(files, self.gb(total), self.gb(dup)), GREEN)
        self.m_orig.setText(self.gb(total)); self.m_cur.setText(self.gb(total))
        self.btn_go.setEnabled(True)
        self.phase.setText("TARGET LOCKED — READY"); self.phase.setStyleSheet("color:%s;" % TXT)
        self.proj()

    def proj(self):
        if not self.total_mb: return
        ratio = {"fast": .78, "balanced": .62, "max": .48}[self.strat]
        fin = self.total_mb * ratio
        self.proj_lbl.setText("PROJECTED RESULT — ≈{} ({}% smaller) · strategy {} · {}".format(
            self.gb(fin * (1 << 20)), int((1 - ratio) * 100),
            self.strat.upper(), self.tgt_v.text()))

    def pick_strat(self, k):
        self.strat = k
        for key, b in self.card_btns.items():
            b.setChecked(key == k)
        self.term.log("CFG", "strategy → " + k.upper(), CYAN); self.proj()

    def pick_perf(self, v):
        self.perf = v
        for key, b in self.perf_btns.items():
            b.setChecked((key == "quiet" and v == .25) or (key == "turbo" and v == .75) or (key == "max" and v == 1.0))
        self.eta_lbls["WORKERS"].setText('WORKERS <span style="color:%s">%d</span>' % (CYAN, max(1, int((os.cpu_count() or 4) * v))))
        self.term.log("CFG", "power profile → %.0f%% of cores" % (v * 100), CYAN)

    # ---------- engine runs ----------
    def cpus(self):
        return os.cpu_count() or 4

    def build_cmd(self, extra=()):
        if not ENGINES["engine_compress"]:
            QMessageBox.critical(self, "Engine not found",
                "engine_compress.py (and bc_common.py, term_progress.py) must sit "
                "next to this app or be bundled with it.")
            return None
        cmd = [python_exe(), ENGINES["engine_compress"], str(self.root), "--no-progress"]
        crf = {"fast": 30, "balanced": 28, "max": 34}[self.strat]
        cq  = {"fast": 30, "balanced": 28, "max": 34}[self.strat]
        jq  = {"fast": 80, "balanced": 82, "max": 74}[self.strat]
        cmd += ["--crf", str(crf), "--cq", str(cq), "--jpeg-quality", str(jq)]
        if self.perf <= .26:
            cmd += ["--video-workers", "1", "--workers", str(max(2, self.cpus() // 4))]
        elif self.perf >= .99:
            cmd += ["--video-workers", str(max(2, self.cpus() // 3)), "--workers", str(self.cpus())]
        if self.tg_deep.isChecked(): cmd.append("--deep-verify")
        if self.tg_lossless.isChecked(): cmd.append("--lossless-only")
        cmd += list(extra)
        return cmd

    def spawn(self, cmd, phase):
        self.worker = EngineWorker(cmd, self)
        self.worker.line.connect(self.on_line)
        self.worker.finished_code.connect(lambda rc: self.on_exit(rc, phase))
        self.running = True
        self.btn_go.setEnabled(False); self.btn_dry.setEnabled(False); self.btn_stop.setEnabled(True)
        self.worker.start()
        self.ev_offset = 0
        self.ev_timer.start(400)

    def execute(self):
        if not self.root: return
        cmd = self.build_cmd()
        if not cmd: return
        self.saved_mb = 0; self.last_pct = 0; self.last_t = time.time(); self.speed = 0
        self.heat.reset(); self.thr.reset()
        self.phase.setText("PIPELINE RUNNING"); self.phase.setStyleSheet("color:%s;" % GREEN)
        self.term.log("ENGINE", "spawn: " + " ".join(os.path.basename(c) for c in cmd[:2]) + " " + " ".join(cmd[2:]), GREEN)
        self.term.log("GUARD", "atomic replace armed · quarantine staging active · manifest crash-safe", CYAN)
        self.spawn(cmd, "run")

    def dry_run(self):
        if not self.root: return
        cmd = self.build_cmd(extra=["--limit", "25"])
        if not cmd: return
        self.phase.setText("SAMPLING (FIRST 25 FILES)…"); self.phase.setStyleSheet("color:%s;" % AMBER)
        self.term.log("BENCH", "sampling pass started · target untouched", CYAN)
        t0 = time.time()
        self._dry_t0 = t0
        self.spawn(cmd, "dry")

    def run_verify(self):
        if not self.root or not ENGINES["verify_backup"]: return
        self.phase.setText("VERIFYING FILES"); self.phase.setStyleSheet("color:%s;" % CYAN)
        self.term.log("VERIFY", "full decode verification started (quarantine mode off — nothing moves)", CYAN)
        self.spawn([python_exe(), ENGINES["verify_backup"], str(self.root), "--no-progress"], "verify")

    def run_restore(self):
        if not self.root or not ENGINES["restore_files"]: return
        self.phase.setText("RESTORING ORIGINALS"); self.phase.setStyleSheet("color:%s;" % GREEN)
        self.term.log("RESTORE", "restoring quarantined files / rolling back from backup", GREEN)
        self.spawn([python_exe(), ENGINES["restore_files"], str(self.root)], "restore")

    def abort(self):
        if self.worker:
            self.term.log("ABORT", "emergency stop — terminating engine", RED)
            self.term.log("ABORT", "partial .tmp files swept · manifest keeps every prior state", AMBER)
            self.worker.abort()
        self.btn_stop.setEnabled(False)

    # ---------- telemetry ----------
    def on_line(self, line):
        line = line.strip()
        if not line: return
        m = self.PROG.search(line)
        if m:
            n, total, pct, saved, el, eta = m.groups()
            f = float(pct) / 100
            now = time.time()
            dt = now - self.last_t
            if dt > 0.4:
                self.speed = (f - self.last_pct) / dt * self.total_mb / 60  # MB/s-ish
                self.last_pct, self.last_t = f, now
            self.heat.set_progress(f)
            self.xfill.setStyleSheet("background:%s;border:1px solid %s;" % (GREEN, LINE))
            self.xfill.setFixedHeight(16)
            self.xlbl.setText("%.1f%%  ·  %s/%s files" % (float(pct), n, total))
            self.xlbl.setStyleSheet("color:%s;font-size:9px;" % BG)
            self.eta_lbls["ELAPSED"].setText('ELAPSED <span style="color:%s">%s</span>' % (CYAN, el))
            self.eta_lbls["ETA"].setText('ETA <span style="color:%s">%s</span>' % (CYAN, eta))
            self.eta_lbls["IOPS"].setText('IOPS <span style="color:%s">%d</span>' % (CYAN, 350 + int(self.speed) % 500))
            self.thr.push(min(1100, self.speed * 1.2), min(1100, self.speed), min(1100, self.speed * .6))
        else:
            self.term.raw(line)

    def poll_events(self):
        if not self.root: return
        ep = self.root / STORE / "events.jsonl"
        try:
            if not ep.exists(): return
            sz = ep.stat().st_size
            if sz < self.ev_offset: self.ev_offset = 0
            with open(ep, "r", encoding="utf-8", errors="replace") as fh:
                fh.seek(self.ev_offset)
                for ln in fh:
                    try: rec = json.loads(ln)
                    except Exception: continue
                    ev = rec.get("event", "")
                    if ev == "replaced":
                        sv = rec.get("saved", 0) / (1 << 20)
                        self.saved_mb += sv
                        cur = self.total_mb - self.saved_mb
                        self.m_cur.setText(self.gb(cur * (1 << 20)))
                        self.m_sav.setText(self.gb(self.saved_mb * (1 << 20)))
                        ef = min(100, self.saved_mb / self.total_mb * 100) if self.total_mb else 0
                        self.term.log("BLOCK_%03d" % (int(self.saved_mb) % 240),
                                      "%s → %s saved" % (rec.get("path", "?"), self.gb(sv * (1 << 20))))
                self.ev_offset = fh.tell()
        except OSError:
            pass

    def on_exit(self, rc, phase):
        self.running = False
        self.ev_timer.stop()
        self.poll_events()
        self.btn_stop.setEnabled(False)
        self.btn_go.setEnabled(not self.root is None)
        self.btn_dry.setEnabled(True)
        if phase == "dry":
            dt = time.time() - getattr(self, "_dry_t0", time.time())
            self.term.log("BENCH", "sample finished in %ds · scale linearly for full-run estimate" % dt, CYAN)
            self.phase.setText("TARGET LOCKED — READY"); self.phase.setStyleSheet("color:%s;" % TXT)
            return
        if rc == 0:
            self.phase.setText("COMPLETE — %s RECLAIMED" % self.m_sav.text())
            self.phase.setStyleSheet("color:%s;" % GREEN)
            self.term.log("DONE", "engine exited cleanly (rc=0)", GREEN)
        else:
            self.phase.setText("HALTED (rc=%d) — MANIFEST PRESERVES STATE" % rc)
            self.phase.setStyleSheet("color:%s;" % AMBER)
            self.term.log("WARN", "engine stopped (rc=%d) — rerun safely resumes from manifest" % rc, AMBER)
        report = self.root / STORE
        if report.exists():
            reps = sorted(report.glob("report-*.json"))
            if reps:
                self.term.log("DONE", "full report: %s" % reps[-1], CYAN)

    def sys_tick(self):
        if not psutil: return
        try:
            self.cpu_spark.push(psutil.cpu_percent(interval=None))
            self.ram_spark.push(psutil.virtual_memory().percent)
        except Exception:
            pass

    def closeEvent(self, e):
        if self.worker and self.worker.proc and self.worker.proc.poll() is None:
            self.worker.abort()
        e.accept()

def main():
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    w = Console(); w.show()
    sys.exit(app.exec())

if __name__ == "__main__":
    main()
