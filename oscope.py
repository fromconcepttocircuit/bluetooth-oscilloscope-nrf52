import sys, re, asyncio, logging
from dataclasses import dataclass, field
from typing import Dict, List
from PyQt6 import QtWidgets, QtCore
import pyqtgraph as pg
import numpy as np
from bleak import BleakScanner, BleakClient
import qasync

logger = logging.getLogger("ble_text_viewer_scope")
logging.basicConfig(level=logging.INFO,
                    format="[%(asctime)s] %(levelname)s: %(message)s",
                    datefmt="%H:%M:%S")

NUS_CHAR_TX_UUID = "6e400003-b5a3-f393-e0a9-e50e24dcca9e"
NUS_CHAR_RX_UUID = "6e400002-b5a3-f393-e0a9-e50e24dcca9e"

VREF             = 3.6
VOLTS_PER_CODE   = VREF / 4096.0
AIN_LABELS       = {
    0: "A0 (P0.02/AIN0)",
    1: "A1 (P0.03/AIN1)",
    2: "A2 (P0.04/AIN2)",
    3: "A3 (P0.05/AIN3)",
}
WINDOW_SEC       = 5.0

SR_MIN_HZ        = 10
SR_MAX_HZ        = 1000
SR_DEFAULT_HZ    = SR_MAX_HZ

line_re = re.compile(r"(?:^|,)\s*ch(?P<ch>\d+)\s*:\s*(?P<val>-?\d+)")
sr_re   = re.compile(r"(?:^|,)\s*sr\s*:\s*(?P<sr>\d+)")
ack_sr  = re.compile(r"(?:^|,)?ack\s*:\s*set_sr\s*:\s*(?P<sr>\d+)")
err_re  = re.compile(r"(?:^|,)?err\s*:\s*(?P<msg>[a-zA-Z0-9_]+)")

@dataclass
class ChannelState:
    sr:        int   = SR_DEFAULT_HZ
    window_sec:float = WINDOW_SEC
    dt:        float = field(init=False)
    N:         int   = field(init=False)
    times: np.ndarray = field(init=False, repr=False)
    values: np.ndarray = field(init=False, repr=False)
    widx:   int = field(default=0, init=False)
    filled: bool = field(default=False, init=False)

    def __post_init__(self):
        self._alloc(self.sr)

    def _alloc(self, sr:int):
        sr = max(1, int(sr))
        self.sr = sr
        self.dt = 1.0 / sr
        n = max(100, int(self.window_sec * sr))
        self.N = n
        self.times = np.arange(n, dtype=np.float64) * self.dt
        self.values = np.full(n, np.nan, dtype=np.float64)
        self.widx = 0
        self.filled = False

    def set_sr(self, sr:int):
        self._alloc(sr)

    def append(self, code:int):
        self.values[self.widx] = float(code)
        self.widx += 1
        if self.widx >= self.N:
            self.widx = 0
            self.filled = True

    def snapshot(self):
        if self.filled:
            y = np.concatenate((self.values[self.widx:], self.values[:self.widx]))
        else:
            y = np.copy(self.values)
        return self.times, y

    def clear(self):
        self.values[:] = np.nan
        self.widx = 0
        self.filled = False

class PlotPane(QtWidgets.QWidget):
    def __init__(self, channels: Dict[int, ChannelState]):
        super().__init__()
        self.channels = channels
        self.sel = 0
        v = QtWidgets.QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(4)

        h = QtWidgets.QHBoxLayout()
        v.addLayout(h)
        self.combo = QtWidgets.QComboBox()
        for ch in range(4):
            self.combo.addItem(f"CH{ch}  {AIN_LABELS[ch]}", userData=ch)
        h.addWidget(QtWidgets.QLabel("Source:"))
        h.addWidget(self.combo)
        h.addStretch(1)
        self.combo.currentIndexChanged.connect(self._on_combo)

        self.plot = pg.PlotWidget()
        self.plot.setYRange(0.0, VREF)
        self.plot.showGrid(x=True, y=True)
        self.plot.setMouseEnabled(x=True, y=True)
        self.plot.getViewBox().setMouseMode(pg.ViewBox.RectMode)
        self.curve = self.plot.plot(pen=pg.mkPen(width=3))
        v.addWidget(self.plot, 1)

        self._x_fixed = False

        self.lbl_paused = pg.TextItem(text="", anchor=(1, 0))
        self.plot.addItem(self.lbl_paused)
        self.lbl_paused.setPos(0, VREF)

        self.marker_x: List[float] = []
        self.marker_y: List[float] = []
        self.marker_scatter = self.plot.plot(
            [], [],
            pen=None,
            symbol='o',
            symbolBrush='r',
            symbolSize=8
        )
        self.marker_labels: List[pg.TextItem] = []

        self.plot.scene().sigMouseClicked.connect(self._on_mouse_click)

    def _on_combo(self, _):
        d = self.combo.currentData()
        if d is not None:
            self.sel = int(d)

    def set_paused(self, paused: bool):
        self.lbl_paused.setText("PAUSED" if paused else "")

    def reset_view(self):
        st = self.channels[self.sel]
        try:
            if getattr(st, "times", None) is not None and st.times.size > 0:
                self.plot.setXRange(float(st.times[0]), float(st.times[-1]), padding=0.0)
            else:
                self.plot.setXRange(0.0, WINDOW_SEC, padding=0.0)
        except Exception:
            self.plot.setXRange(0.0, WINDOW_SEC, padding=0.0)
        self.plot.setYRange(0.0, VREF)
        self._x_fixed = True

    def clear_markers(self):
        self.marker_x.clear()
        self.marker_y.clear()
        self.marker_scatter.setData([], [])
        for label in self.marker_labels:
            self.plot.removeItem(label)
        self.marker_labels.clear()

    def _on_mouse_click(self, event):
        if event.button() != QtCore.Qt.MouseButton.LeftButton:
            return
        pos = event.scenePos()
        if not self.plot.sceneBoundingRect().contains(pos):
            return

        vb = self.plot.getViewBox()
        mouse_point = vb.mapSceneToView(pos)
        x = mouse_point.x()

        st = self.channels[self.sel]
        times, codes = st.snapshot()
        if times is None or times.size == 0:
            return
        if codes is None or np.all(np.isnan(codes)):
            return

        idx = int(np.argmin(np.abs(times - x)))
        code_val = float(codes[idx])
        if np.isnan(code_val):
            return

        t_val = float(times[idx])
        v_val = code_val * VOLTS_PER_CODE

        self.marker_x.append(t_val)
        self.marker_y.append(v_val)
        self.marker_scatter.setData(self.marker_x, self.marker_y)

        label = pg.TextItem(text=f"t={t_val:.3f} s, V={v_val:.3f} V",
                            anchor=(0, 1))
        self.plot.addItem(label)
        label.setPos(t_val, v_val + 0.03 * VREF)
        self.marker_labels.append(label)

    def refresh(self):
        st = self.channels[self.sel]
        times, codes = st.snapshot()
        if codes is None or np.all(np.isnan(codes)):
            self.curve.setData([], [])
            return
        volts = codes * VOLTS_PER_CODE
        self.curve.setData(times, volts)

        if not self._x_fixed and len(times):
            self.plot.setXRange(0.0, times[-1], padding=0.0)
            self._x_fixed = True

        last_idx = np.where(~np.isnan(codes))[0]
        if last_idx.size:
            code = int(codes[last_idx[-1]])
            v = code * VOLTS_PER_CODE
            self.plot.setTitle(
                f"{AIN_LABELS[self.sel]}: {v:.3f} V (code {code})"
            )

        vr = self.plot.viewRange()
        if vr:
            x2 = vr[0][1]
            y2 = vr[1][1]
            self.lbl_paused.setPos(x2, y2)

class App(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(
            "BLE ADC Text-Stream Viewer — Oscilloscope Mode"
        )
        central = QtWidgets.QWidget()
        self.setCentralWidget(central)
        root = QtWidgets.QVBoxLayout(central)

        top = QtWidgets.QHBoxLayout()
        root.addLayout(top)
        self.btn_scan = QtWidgets.QPushButton("Scan")
        self.cmb = QtWidgets.QComboBox()
        self.btn_conn = QtWidgets.QPushButton("Connect")
        self.btn_disc = QtWidgets.QPushButton("Disconnect")
        self.btn_disc.setEnabled(False)
        top.addWidget(self.btn_scan)
        top.addWidget(self.cmb, 1)
        top.addWidget(self.btn_conn)
        top.addWidget(self.btn_disc)

        ctrl = QtWidgets.QHBoxLayout()
        root.addLayout(ctrl)
        self.btn_add = QtWidgets.QPushButton("Add Plot")
        ctrl.addWidget(self.btn_add)
        ctrl.addSpacing(16)
        ctrl.addWidget(QtWidgets.QLabel("Sample Rate (Hz):"))
        self.spin_sr = QtWidgets.QSpinBox()
        self.spin_sr.setRange(SR_MIN_HZ, SR_MAX_HZ)
        self.spin_sr.setValue(SR_MAX_HZ)
        ctrl.addWidget(self.spin_sr)
        self.btn_set_sr = QtWidgets.QPushButton("Set SR")
        self.btn_set_sr.setEnabled(False)
        ctrl.addWidget(self.btn_set_sr)
        ctrl.addSpacing(16)
        self.btn_pause = QtWidgets.QPushButton("Pause")
        self.btn_pause.setEnabled(True)
        ctrl.addWidget(self.btn_pause)
        self.btn_zoom_reset = QtWidgets.QPushButton("Reset Zoom")
        ctrl.addWidget(self.btn_zoom_reset)
        self.btn_clear_markers = QtWidgets.QPushButton("Clear Markers")
        ctrl.addWidget(self.btn_clear_markers)
        self.btn_clear_plots = QtWidgets.QPushButton("Clear Plot")
        ctrl.addWidget(self.btn_clear_plots)
        ctrl.addStretch(1)

        info = QtWidgets.QHBoxLayout()
        root.addLayout(info)
        self.lbl_sr = QtWidgets.QLabel("SR: - Hz")
        self.lbl_rate = QtWidgets.QLabel("RX: 0.0 kB/s")
        info.addWidget(self.lbl_sr)
        info.addWidget(self.lbl_rate, 1, QtCore.Qt.AlignmentFlag.AlignRight)

        self.container = QtWidgets.QWidget()
        root.addWidget(self.container, 1)
        self.v = QtWidgets.QVBoxLayout(self.container)
        self.v.setContentsMargins(0, 0, 0, 0)
        self.v.setSpacing(6)
        self.panes: List[PlotPane] = []

        self.status = QtWidgets.QStatusBar()
        self.setStatusBar(self.status)

        self.client: BleakClient | None = None
        self.buf = bytearray()
        self.channels: Dict[int, ChannelState] = {
            i: ChannelState() for i in range(4)
        }
        self.rx_bytes = 0
        self.paused = False
        self._sr_pending: int | None = None

        self.btn_scan.clicked.connect(self._scan_clicked)
        self.btn_conn.clicked.connect(self._conn_clicked)
        self.btn_disc.clicked.connect(self._disc_clicked)
        self.btn_add.clicked.connect(self._add_plot)
        self.btn_set_sr.clicked.connect(self._set_sr_clicked)
        self.btn_pause.clicked.connect(self._toggle_pause)
        self.btn_zoom_reset.clicked.connect(self._reset_zoom_clicked)
        self.btn_clear_markers.clicked.connect(self._clear_markers_clicked)
        self.btn_clear_plots.clicked.connect(self._clear_plots_clicked)

        self.t_plot = QtCore.QTimer()
        self.t_plot.timeout.connect(self._refresh)
        self.t_plot.start(30)
        self.t_rate = QtCore.QTimer()
        self.t_rate.timeout.connect(self._tick_rate)
        self.t_rate.start(1000)

        self._add_plot()
        self._add_plot()

    def _msg(self, s):
        self.status.showMessage(s, 2500)

    def _add_plot(self):
        if len(self.panes) >= 4:
            QtWidgets.QMessageBox.information(
                self, "Limit", "Max 4 plots."
            )
            return
        p = PlotPane(self.channels)
        p.set_paused(self.paused)
        self.panes.append(p)
        self.v.addWidget(p)

    def _refresh(self):
        for p in self.panes:
            p.refresh()

    @QtCore.pyqtSlot()
    def _toggle_pause(self):
        self.paused = not self.paused
        self.btn_pause.setText("Resume" if self.paused else "Pause")
        for p in self.panes:
            p.set_paused(self.paused)
        if (not self.paused) and (self._sr_pending is not None):
            for st in self.channels.values():
                st.set_sr(self._sr_pending)
            for p in self.panes:
                p._x_fixed = False
            self._sr_pending = None
        self._msg("Paused." if self.paused else "Resumed.")

    @QtCore.pyqtSlot()
    def _reset_zoom_clicked(self):
        for p in self.panes:
            p.reset_view()
        self._msg("Zoom reset.")

    @QtCore.pyqtSlot()
    def _clear_markers_clicked(self):
        for p in self.panes:
            p.clear_markers()
        self._msg("Markers cleared.")

    @QtCore.pyqtSlot()
    def _clear_plots_clicked(self):
        for st in self.channels.values():
            st.clear()
        for p in self.panes:
            p.clear_markers()
            p._x_fixed = False
            p.curve.setData([], [])
        self._msg("Plots cleared; new samples will start from the beginning.")

    async def _scan(self):
        self.cmb.clear()
        self._msg("Scanning (5s)...")
        devs = await BleakScanner.discover(timeout=5.0)
        feather_count = 0
        for d in devs:
            name = d.name or ""
            if "feather" in name.lower():
                self.cmb.addItem(f"{name} [{d.address}]", userData=d)
                feather_count += 1
        self._msg(f"Found {feather_count} Feather device(s).")

    @QtCore.pyqtSlot()
    def _scan_clicked(self):
        asyncio.create_task(self._scan())

    async def _connect(self):
        d = self.cmb.currentData()
        if not d:
            return
        try:
            self.client = BleakClient(d, timeout=20.0)
            await self.client.connect()
            await self.client.start_notify(NUS_CHAR_TX_UUID, self._notify)
            self.btn_conn.setEnabled(False)
            self.btn_disc.setEnabled(True)
            self.btn_set_sr.setEnabled(True)

            for st in self.channels.values():
                st.set_sr(SR_DEFAULT_HZ)
            for p in self.panes:
                p._x_fixed = False

            self.spin_sr.setValue(SR_MAX_HZ)
            self.lbl_sr.setText(f"SR: {SR_MAX_HZ} Hz (requested)")
            cmd = f"set_sr:{SR_MAX_HZ}\n".encode("ascii")
            await self._async_write(NUS_CHAR_RX_UUID, cmd)

            self._msg("Connected. Streaming...")
        except Exception as e:
            QtWidgets.QMessageBox.critical(self, "Connect error", str(e))
            try:
                if self.client:
                    await self.client.disconnect()
            except Exception:
                pass
            self.client = None

    @QtCore.pyqtSlot()
    def _conn_clicked(self):
        asyncio.create_task(self._connect())

    async def _disconnect(self):
        try:
            if self.client:
                await self.client.stop_notify(NUS_CHAR_TX_UUID)
                await self.client.disconnect()
        except Exception:
            pass
        self.client = None
        self.btn_conn.setEnabled(True)
        self.btn_disc.setEnabled(False)
        self.btn_set_sr.setEnabled(False)
        self._msg("Disconnected.")

    @QtCore.pyqtSlot()
    def _disc_clicked(self):
        asyncio.create_task(self._disconnect())

    @QtCore.pyqtSlot()
    def _set_sr_clicked(self):
        if not self.client:
            QtWidgets.QMessageBox.information(
                self, "Not connected", "Connect to a device first."
            )
            return
        hz = self.spin_sr.value()
        cmd = f"set_sr:{hz}\n".encode("ascii")
        self.lbl_sr.setText(f"SR: {hz} Hz (requested)")
        asyncio.create_task(self._async_write(NUS_CHAR_RX_UUID, cmd))

    async def _async_write(self, uuid, data: bytes):
        try:
            await self.client.write_gatt_char(uuid, data, response=False)
            self._msg("SR command sent.")
        except Exception as e:
            QtWidgets.QMessageBox.critical(self, "Write failed", str(e))

    def _notify(self, _h: int, data: bytes):
        self.buf.extend(data)
        self.rx_bytes += len(data)
        while True:
            i = self.buf.find(b"\n")
            if i < 0:
                break
            line = self.buf[:i].decode("utf-8", errors="ignore").strip()
            del self.buf[:i + 1]
            if not line:
                continue

            m_ack = ack_sr.search(line)
            if m_ack:
                self._msg(f"ACK set_sr -> {m_ack.group('sr')} Hz")
            m_err = err_re.search(line)
            if m_err:
                self._msg(f"Device error: {m_err.group('msg')}")

            m_sr = sr_re.search(line)
            if m_sr:
                sr = int(m_sr.group("sr"))
                self.lbl_sr.setText(f"SR: {sr} Hz")
                if self.spin_sr.value() != sr:
                    self.spin_sr.setValue(sr)
                any_diff = any(st.sr != sr for st in self.channels.values())
                if any_diff:
                    if self.paused:
                        self._sr_pending = sr
                    else:
                        for st in self.channels.values():
                            st.set_sr(sr)
                        for p in self.panes:
                            p._x_fixed = False

            if not self.paused:
                for m in line_re.finditer(line):
                    ch = int(m.group("ch"))
                    if 0 <= ch <= 3:
                        val = int(m.group("val"))
                        self.channels[ch].append(val)

    def _tick_rate(self):
        kbps = self.rx_bytes / 1024.0
        self.lbl_rate.setText(f"RX: {kbps:.1f} kB/s")
        self.rx_bytes = 0

def main():
    app = QtWidgets.QApplication(sys.argv)
    loop = qasync.QEventLoop(app)
    asyncio.set_event_loop(loop)
    win = App()
    win.resize(1080, 840)
    win.show()
    with loop:
        loop.run_forever()

if __name__ == "__main__":
    main()