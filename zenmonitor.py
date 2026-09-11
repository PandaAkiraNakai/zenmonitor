#!/usr/bin/env python3
"""ZenMonitor - monitor del ASUS Zenbook 14 UM3406GA: ventilador, carga y energia.

Medido en este equipo el 2026-09-11: el EC no deja fijar la velocidad.
No existe el nodo pwm1, y pwm1_enable=0 (el "full speed" del driver asus-wmi)
no fuerza nada -- con el equipo frio el ventilador se para igual y el valor se
revierte solo a 2. asusd tampoco publica xyz.ljones.FanCurves para esta placa.
La unica palanca real es el perfil de plataforma, que si cambia la curva del EC:
con carga fija se midieron 3787 RPM (performance), 2875 (balanced) y 1975 (quiet).
Se cambia por power-profiles-daemon para no pelearse con el widget de KDE.
"""

import glob
import os
import struct
import subprocess
import sys
from collections import deque

from PyQt6.QtCore import QPointF, QRectF, Qt, QTimer
from PyQt6.QtGui import (QAction, QColor, QFont, QIcon, QPainter, QPainterPath,
                         QPen, QPixmap)
from PyQt6.QtNetwork import QLocalServer, QLocalSocket
from PyQt6.QtWidgets import (QApplication, QButtonGroup, QFrame, QGridLayout,
                             QHBoxLayout, QLabel, QMenu, QPushButton,
                             QSystemTrayIcon, QVBoxLayout, QWidget)

RPM_MAX = 6000
POLL_MS = 1500
HISTORY = 80

PROFILES = [
    ("power-saver", "Silencioso",
     "Medido con carga fija: 1975 RPM, APU a 44 \u00b0C con solo 6,7 W.\n"
     "Silencio real, pero estrangula la APU a un quinto de su potencia."),
    ("balanced", "Equilibrado",
     "Medido con carga fija: 2875 RPM, APU a 72 \u00b0C con 31,6 W.\n"
     "900 RPM menos que Rendimiento por 3 \u00b0C mas y un 11 % menos de potencia."),
    ("performance", "Rendimiento",
     "Medido con carga fija: 3787 RPM, APU a 69 \u00b0C con 35,4 W.\n"
     "La curva mas agresiva: mas ruido para mantener la APU mas fria."),
]


def read(path, cast=str, default=None):
    try:
        with open(path) as fh:
            return cast(fh.read().strip())
    except (OSError, ValueError, TypeError):
        return default


def dpm_levels(path):
    """Nivel activo (marcado con '*') y maximo de un fichero pp_dpm_*, en MHz."""
    actual, todos = None, []
    for linea in (read(path) or "").splitlines():
        try:
            mhz = int(linea.split(":", 1)[1].strip().rstrip("* ").lower()
                      .replace("mhz", ""))
        except (IndexError, ValueError):
            continue
        todos.append(mhz)
        if linea.rstrip().endswith("*"):
            actual = mhz
    return actual, (max(todos) if todos else None)


# Campos del struct gpu_metrics_v3_0 que publica el amdgpu de esta APU (264 B).
# Los desplazamientos salen de la alineacion natural del struct del kernel; el
# tamano declarado en la cabecera (264) confirma el calculo. Da dos cosas que
# sysfs no ofrece: el UCLK siempre presente (el '*' de pp_dpm_mclk falta a ratos)
# y el ancho de banda real de la DRAM.
METRICS_V3 = {"gfx_act": ("H", 42), "dram_rd": ("H", 94), "dram_wr": ("H", 96),
              "gfxclk": ("H", 174), "uclk": ("H", 186), "core_max": ("H", 222)}

# Tope medido en este equipo con cuatro copias de memoria en paralelo: 75 GB/s.
DRAM_ESCALA = 75000        # MB/s, referencia para la barra


def gpu_metrics(path):
    """Lee gpu_metrics si la revision es la 3.0 esperada; si no, no devuelve nada."""
    try:
        with open(path, "rb") as fh:
            raw = fh.read()
    except OSError:
        return {}
    if len(raw) < 264 or struct.unpack_from("<HBB", raw, 0)[1:] != (3, 0):
        return {}
    return {k: struct.unpack_from("<" + f, raw, off)[0]
            for k, (f, off) in METRICS_V3.items()}


def gib(n):
    return "--" if n is None else f"{n / 2 ** 30:.1f} GiB"


def mib(n):
    return "--" if n is None else f"{n / 2 ** 20:.0f} MiB"


def pct(n):
    return "--" if n is None else f"{n:.0f} %"


def find_hwmon(name):
    """Localiza un hwmon por su nombre; el numero cambia entre arranques."""
    for d in sorted(glob.glob("/sys/class/hwmon/hwmon*")):
        if read(os.path.join(d, "name")) == name:
            return d
    return None


class Hardware:
    """Toda la conversacion con sysfs y con power-profiles-daemon."""

    def __init__(self):
        self.prev_cpu = None
        self.last_mem_mhz = None
        self.refresh_paths()

    def refresh_paths(self):
        self.asus = find_hwmon("asus")
        self.gpu = find_hwmon("amdgpu")
        self.nvme = find_hwmon("nvme")
        self.acpi = find_hwmon("acpitz")
        self.bat = find_hwmon("BAT0")
        # el hwmon cuelga del dispositivo PCI, que es donde estan uso,
        # relojes y memoria de la GPU
        self.gpu_dev = self._p(self.gpu, "device")

    def _p(self, base, leaf):
        return os.path.join(base, leaf) if base else None

    def rpm(self):
        v = read(self._p(self.asus, "fan1_input"), int)
        if v is None:
            alt = find_hwmon("acpi_fan")
            v = read(self._p(alt, "fan1_input"), int)
        return v

    def temps(self):
        return {
            "APU": read(self._p(self.gpu, "temp1_input"), int),
            "Sistema": read(self._p(self.acpi, "temp1_input"), int),
            "SSD": read(self._p(self.nvme, "temp1_input"), int),
        }

    def watts(self):
        return {
            "APU": read(self._p(self.gpu, "power1_average"), int),
            "Equipo": read(self._p(self.bat, "power1_input"), int),
        }

    def cpu(self):
        """Uso agregado (hacen falta dos lecturas) y frecuencia media de los hilos."""
        uso = None
        linea = read("/proc/stat", lambda t: t.split("\n", 1)[0])
        if linea:
            v = [int(x) for x in linea.split()[1:]]
            parado, total = v[3] + v[4], sum(v)      # idle + iowait
            if self.prev_cpu:
                d_parado, d_total = parado - self.prev_cpu[0], total - self.prev_cpu[1]
                if d_total > 0:
                    uso = 100.0 * (1.0 - d_parado / d_total)
            self.prev_cpu = (parado, total)
        khz = [k for k in (read(f, int) for f in glob.glob(
            "/sys/devices/system/cpu/cpu*/cpufreq/scaling_cur_freq")) if k]
        return {"uso": uso,
                "mhz": sum(khz) / len(khz) / 1000 if khz else None,
                "hilos": len(khz) or os.cpu_count(),
                "gobernador": read(
                    "/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor")}

    def ram(self):
        """Memoria del sistema en bytes. 'usada' = lo que no esta disponible."""
        info = {}
        try:
            with open("/proc/meminfo") as fh:
                for linea in fh:
                    clave, _, resto = linea.partition(":")
                    try:
                        info[clave] = int(resto.split()[0]) * 1024
                    except (IndexError, ValueError):
                        pass
        except OSError:
            return {}
        total, disp = info.get("MemTotal"), info.get("MemAvailable")
        swap_total = info.get("SwapTotal", 0)
        return {
            "total": total,
            "usada": None if None in (total, disp) else total - disp,
            "disponible": disp,
            "cache": info.get("Cached"),
            "swap": swap_total - info.get("SwapFree", 0) if swap_total else None,
        }

    def gpu_info(self):
        """Uso, relojes y memoria de la Radeon integrada (amdgpu)."""
        d = self.gpu_dev
        sclk = read(self._p(self.gpu, "freq1_input"), int)
        # amdgpu no siempre marca con '*' el nivel activo de mclk: en bastantes
        # lecturas no viene ninguno. Se conserva el ultimo conocido para no parpadear.
        mem_mhz, mem_max = dpm_levels(self._p(d, "pp_dpm_mclk"))
        met = gpu_metrics(self._p(d, "gpu_metrics"))
        # el UCLK del SMU es fiable; el '*' de pp_dpm_mclk solo se usa de reserva
        mem_mhz = met.get("uclk") or mem_mhz or self.last_mem_mhz
        if mem_mhz:
            self.last_mem_mhz = mem_mhz
        _, sclk_max = dpm_levels(self._p(d, "pp_dpm_sclk"))
        return {
            "uso": read(self._p(d, "gpu_busy_percent"), int),
            "mhz": None if sclk is None else sclk / 1e6,
            "mhz_max": sclk_max,
            "mem_mhz": mem_mhz,
            "mem_mhz_max": mem_max,
            "vram": read(self._p(d, "mem_info_vram_used"), int),
            "vram_total": read(self._p(d, "mem_info_vram_total"), int),
            "gtt": read(self._p(d, "mem_info_gtt_used"), int),
            "gtt_total": read(self._p(d, "mem_info_gtt_total"), int),
            # trafico de la RAM del sistema: lo mide el SMU, no hay otra fuente
            "dram_rd": met.get("dram_rd"),
            "dram_wr": met.get("dram_wr"),
        }

    def profile(self):
        try:
            out = subprocess.run(["powerprofilesctl", "get"], capture_output=True,
                                 text=True, timeout=4)
            return out.stdout.strip()
        except (OSError, subprocess.SubprocessError):
            return read("/sys/firmware/acpi/platform_profile")

    def set_profile(self, name):
        try:
            r = subprocess.run(["powerprofilesctl", "set", name],
                               capture_output=True, text=True, timeout=8)
            return r.returncode == 0
        except (OSError, subprocess.SubprocessError):
            return False


class Gauge(QWidget):
    """Arco con las RPM actuales."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(150)
        self.rpm = None
        self.accent = QColor("#3daee9")
        self.caption = ""

    def set_values(self, rpm, accent, caption):
        self.rpm, self.accent, self.caption = rpm, accent, caption
        self.update()

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        side = min(self.width() * 0.82, self.height() * 1.18)
        box = QRectF(0, 0, side, side)
        box.moveCenter(QPointF(self.width() / 2, self.height() / 2 + side * 0.06))

        start, span = 225.0, 270.0
        track = QColor(self.palette().text().color())
        track.setAlpha(28)
        pen = QPen(track, 14)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        p.setPen(pen)
        p.drawArc(box, int(start * 16), int(-span * 16))

        frac = 0.0 if not self.rpm else max(0.0, min(1.0, self.rpm / RPM_MAX))
        if frac > 0.001:
            pen = QPen(self.accent, 14)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            p.setPen(pen)
            p.drawArc(box, int(start * 16), int(-span * frac * 16))

        txt = self.palette().text().color()
        big = QFont(self.font())
        big.setPointSizeF(max(24.0, side * 0.20))
        big.setWeight(QFont.Weight.Light)
        p.setFont(big)
        p.setPen(txt)
        value = "--" if self.rpm is None else str(self.rpm)
        vbox = QRectF(box)
        vbox.translate(0, -side * 0.045)
        p.drawText(vbox, Qt.AlignmentFlag.AlignCenter, value)

        small = QFont(self.font())
        small.setPointSizeF(max(8.0, side * 0.058))
        small.setCapitalization(QFont.Capitalization.AllUppercase)
        small.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 1.4)
        p.setFont(small)
        dim = QColor(txt)
        dim.setAlpha(150)
        p.setPen(dim)
        lbox = QRectF(box)
        lbox.translate(0, side * 0.135)
        p.drawText(lbox, Qt.AlignmentFlag.AlignCenter, "rpm")

        if self.caption:
            cbox = QRectF(box)
            cbox.translate(0, side * 0.315)
            cap = QFont(self.font())
            cap.setPointSizeF(max(8.0, side * 0.052))
            p.setFont(cap)
            p.setPen(self.accent)
            p.drawText(cbox, Qt.AlignmentFlag.AlignCenter, self.caption)


class Spark(QWidget):
    """Historico corto de RPM."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(36)
        self.data = deque(maxlen=HISTORY)
        self.accent = QColor("#3daee9")

    def push(self, rpm, accent):
        self.data.append(rpm or 0)
        self.accent = accent
        self.update()

    def paintEvent(self, _event):
        if len(self.data) < 2:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        step = w / (HISTORY - 1)
        offset = w - step * (len(self.data) - 1)

        pts = []
        for i, v in enumerate(self.data):
            frac = max(0.0, min(1.0, v / RPM_MAX))
            pts.append(QPointF(offset + i * step, h - 3 - frac * (h - 8)))

        line = QPainterPath(pts[0])
        for pt in pts[1:]:
            line.lineTo(pt)

        fill = QPainterPath(line)
        fill.lineTo(pts[-1].x(), h)
        fill.lineTo(pts[0].x(), h)
        fill.closeSubpath()
        shade = QColor(self.accent)
        shade.setAlpha(48)
        p.fillPath(fill, shade)

        pen = QPen(self.accent, 1.8)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        p.setPen(pen)
        p.drawPath(line)


IPC = f"zenmonitor-{os.getuid()}"

PLATFORM_TO_PPD = {"quiet": "power-saver", "balanced": "balanced",
                   "performance": "performance"}

BLUE = QColor("#3daee9")
AMBER = QColor("#e8a33d")


# Monograma "A" de ASUS, el mismo que pinta el firmware al arrancar. Los
# contornos estan vectorizados del logo de la BGRT (/sys/firmware/acpi/bgrt/image,
# 116x114 px) para que escale sin pixelarse: perfil exterior y los dos huecos.
ASUS_OUTLINE = ((55, 0), (62, 1), (115, 103), (115, 109), (111, 113), (104, 113),
                (58, 85), (10, 113), (4, 113), (0, 109), (0, 103), (8, 87), (52, 3))
ASUS_HOLES = (((57, 15), (81, 59), (14, 99)),
              ((85, 69), (101, 99), (69, 80)))


def asus_icon(color, size=128):
    """Icono con el monograma de ASUS, dibujado a mano y sin depender del tema."""
    pm = QPixmap(size, size)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    k = size / 116.0 * 0.86            # deja aire alrededor, la bandeja recorta
    p.translate((size - 116 * k) / 2, (size - 114 * k) / 2)
    p.scale(k, k)
    path = QPainterPath()
    path.setFillRule(Qt.FillRule.OddEvenFill)
    for poly in (ASUS_OUTLINE,) + ASUS_HOLES:
        path.moveTo(float(poly[0][0]), float(poly[0][1]))
        for x, y in poly[1:]:
            path.lineTo(float(x), float(y))
        path.closeSubpath()
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(color)
    p.drawPath(path)
    p.end()
    return QIcon(pm)


class Stat(QFrame):
    """Celda con una medida."""

    def __init__(self, title, parent=None):
        super().__init__(parent)
        self.setObjectName("stat")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 8, 10, 9)
        lay.setSpacing(1)
        self.key = QLabel(title)
        self.key.setObjectName("statKey")
        self.val = QLabel("--")
        self.val.setObjectName("statVal")
        lay.addWidget(self.key)
        lay.addWidget(self.val)

    def set(self, text):
        self.val.setText(text)


class Bar(QWidget):
    """Barra de progreso fina, pintada a mano para seguir el color de acento."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(5)
        self.frac = 0.0
        self.accent = BLUE

    def set_frac(self, frac, accent):
        self.frac = max(0.0, min(1.0, frac or 0.0))
        self.accent = accent
        self.update()

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = self.height() / 2
        track = QColor(self.palette().text().color())
        track.setAlpha(30)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(track)
        p.drawRoundedRect(QRectF(0, 0, self.width(), self.height()), r, r)
        if self.frac > 0.004:
            p.setBrush(self.accent)
            p.drawRoundedRect(QRectF(0, 0, self.width() * self.frac,
                                     self.height()), r, r)


class Meter(QFrame):
    """Celda con etiqueta, lectura y barra de ocupacion."""

    def __init__(self, title, parent=None):
        super().__init__(parent)
        self.setObjectName("stat")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 7, 10, 8)
        lay.setSpacing(5)
        top = QHBoxLayout()
        top.setSpacing(6)
        self.key = QLabel(title)
        self.key.setObjectName("statKey")
        self.sub = QLabel("")                 # dato de contexto: reloj o total
        self.sub.setObjectName("meterSub")
        self.val = QLabel("--")
        self.val.setObjectName("meterVal")
        top.addWidget(self.key)
        top.addWidget(self.sub)
        top.addStretch(1)
        top.addWidget(self.val)
        lay.addLayout(top)
        self.bar = Bar()
        lay.addWidget(self.bar)

    def set(self, text, frac, accent=BLUE, tip=None, sub=""):
        self.val.setText(text)
        self.sub.setText(sub)
        self.bar.set_frac(frac, accent)
        if tip is not None:
            self.setToolTip(tip)


def load_accent(frac):
    """Ambar cuando algo se acerca al limite; el resto del tiempo, azul."""
    return AMBER if (frac or 0) >= 0.85 else BLUE


class ZenMonitor(QWidget):
    def __init__(self):
        super().__init__()
        self.hw = Hardware()
        self.tray_accent = BLUE
        self.setWindowTitle("ZenMonitor")
        self.setMinimumWidth(360)
        self.build()
        self.build_tray()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.poll)
        self.timer.start(POLL_MS)
        self.poll()

    # ---------------------------------------------------------------- interfaz
    def build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 14, 16, 16)
        root.setSpacing(10)

        self.gauge = Gauge()
        root.addWidget(self.gauge)
        self.spark = Spark()
        root.addWidget(self.spark)

        root.addWidget(self.section("Carga"))
        self.m_cpu, self.m_gpu = Meter("CPU"), Meter("GPU")
        self.m_ram, self.m_vram = Meter("RAM"), Meter("VRAM")
        meters = QGridLayout()
        meters.setSpacing(8)
        for i, m in enumerate((self.m_cpu, self.m_gpu, self.m_ram, self.m_vram)):
            m.setMinimumWidth(160)
            meters.addWidget(m, i // 2, i % 2)
        self.m_dram = Meter("DRAM")
        meters.addWidget(self.m_dram, 2, 0, 1, 2)
        root.addLayout(meters)

        root.addWidget(self.section("Temperaturas"))
        self.t_apu, self.t_sys, self.t_ssd = Stat("APU"), Stat("Sistema"), Stat("SSD")
        row = QHBoxLayout()
        row.setSpacing(8)
        for s in (self.t_apu, self.t_sys, self.t_ssd):
            row.addWidget(s)
        root.addLayout(row)

        root.addWidget(self.section("Energía"))
        self.w_apu, self.w_all = Stat("Potencia APU"), Stat("Consumo total")
        row2 = QHBoxLayout()
        row2.setSpacing(8)
        row2.addWidget(self.w_apu)
        row2.addWidget(self.w_all)
        root.addLayout(row2)

        perfil = self.section("Perfil")
        perfil.setToolTip(
            "Este equipo no admite velocidad manual de ventilador: no hay nodo pwm1 "
            "y el EC ignora pwm1_enable=0.\nLa curva la fija el perfil.")
        root.addWidget(perfil)

        self.group = QButtonGroup(self)
        self.group.setExclusive(True)
        prow = QHBoxLayout()
        prow.setSpacing(6)
        for idx, (pid, label, tip) in enumerate(PROFILES):
            b = QPushButton(label)
            b.setCheckable(True)
            b.setToolTip(tip)
            b.setProperty("pid", pid)
            b.setObjectName("profile")
            b.clicked.connect(lambda _c, p=pid: self.apply_profile(p))
            self.group.addButton(b, idx)
            prow.addWidget(b)
        root.addLayout(prow)

        # Solo aparece si algo falla; el resto del tiempo no ocupa sitio.
        self.note = QLabel()
        self.note.setObjectName("note")
        self.note.setWordWrap(True)
        self.note.hide()
        root.addWidget(self.note)

        self.apply_style()

    def section(self, text):
        lab = QLabel(text)
        lab.setObjectName("section")
        return lab

    def apply_style(self):
        dark = self.palette().window().color().lightness() < 128
        card = "rgba(255,255,255,0.055)" if dark else "rgba(0,0,0,0.045)"
        edge = "rgba(255,255,255,0.09)" if dark else "rgba(0,0,0,0.08)"
        dim = "rgba(255,255,255,0.55)" if dark else "rgba(0,0,0,0.52)"
        hover = "rgba(255,255,255,0.10)" if dark else "rgba(0,0,0,0.08)"
        self.setStyleSheet(f"""
            QFrame#stat {{ background: {card}; border: 1px solid {edge};
                           border-radius: 9px; }}
            QLabel#statKey {{ color: {dim}; font-size: 10px;
                              text-transform: uppercase; letter-spacing: .6px; }}
            QLabel#statVal {{ font-size: 15px; font-weight: 600; }}
            QLabel#meterVal {{ font-size: 13px; font-weight: 600; }}
            QLabel#meterSub {{ color: {dim}; font-size: 11px; }}
            QLabel#section {{ color: {dim}; font-size: 10px; font-weight: 700;
                              text-transform: uppercase; letter-spacing: 1.1px;
                              margin-top: 2px; }}
            QLabel#note {{ color: {dim}; font-size: 10px; }}
            QPushButton#profile {{
                background: {card}; border: 1px solid {edge}; border-radius: 8px;
                padding: 8px 6px; font-size: 12px; }}
            QPushButton#profile:hover {{ background: {hover}; }}
            QPushButton#profile:checked {{
                background: {BLUE.name()}; border-color: {BLUE.name()};
                color: white; font-weight: 600; }}
        """)

    def build_tray(self):
        self.tray = QSystemTrayIcon(asus_icon(BLUE), self)
        menu = QMenu()
        self.tray_profiles = {}
        for pid, label, _tip in PROFILES:
            act = QAction(label, self, checkable=True)
            act.triggered.connect(lambda _c, p=pid: self.apply_profile(p))
            menu.addAction(act)
            self.tray_profiles[pid] = act
        menu.addSeparator()
        show = QAction("Mostrar ventana", self)
        show.triggered.connect(self.show_window)
        menu.addAction(show)
        quit_act = QAction("Salir", self)
        quit_act.triggered.connect(QApplication.quit)
        menu.addAction(quit_act)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self.on_tray_click)
        self.tray.show()

    # ---------------------------------------------------------------- acciones
    def current_profile(self):
        raw = read("/sys/firmware/acpi/platform_profile")
        return PLATFORM_TO_PPD.get(raw) or self.hw.profile()

    def apply_profile(self, pid):
        if not self.hw.set_profile(pid):
            self.note.setText("No se pudo cambiar el perfil (power-profiles-daemon).")
            self.note.show()
        self.poll()

    def on_tray_click(self, reason):
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self.hide() if self.isVisible() else self.show_window()

    def on_ipc(self, server):
        """Otra invocacion (la tecla F12 o el lanzador) pide la ventana."""
        conn = server.nextPendingConnection()
        if conn is not None:
            conn.disconnected.connect(conn.deleteLater)
        if self.isVisible() and self.isActiveWindow():
            self.hide()
        else:
            self.show_window()

    def show_window(self):
        self.show()
        self.raise_()
        self.activateWindow()

    def closeEvent(self, event):
        if self.tray.isVisible():
            event.ignore()
            self.hide()
        else:
            event.accept()

    # ------------------------------------------------------------------ lectura
    def poll(self):
        if self.hw.rpm() is None:
            self.hw.refresh_paths()

        rpm = self.hw.rpm()
        loud = (rpm or 0) >= 3400
        accent = AMBER if loud else BLUE
        caption = "parado" if rpm == 0 else ("ruidoso" if loud else "silencioso")

        self.gauge.set_values(rpm, accent, caption)
        self.spark.push(rpm, accent)

        temps = self.hw.temps()
        for stat, key in ((self.t_apu, "APU"), (self.t_sys, "Sistema"),
                          (self.t_ssd, "SSD")):
            v = temps.get(key)
            stat.set("--" if v is None else f"{v / 1000:.0f} °C")

        w = self.hw.watts()
        self.w_apu.set("--" if w["APU"] is None else f"{w['APU'] / 1e6:.1f} W")
        self.w_all.set("--" if not w["Equipo"] else f"{w['Equipo'] / 1e6:.1f} W")

        cpu = self.hw.cpu()
        ghz = "--" if cpu["mhz"] is None else f"{cpu['mhz'] / 1000:.2f} GHz"
        f_cpu = (cpu["uso"] or 0) / 100
        self.m_cpu.set(pct(cpu["uso"]), f_cpu, load_accent(f_cpu),
                       f"Media de los {cpu['hilos']} hilos\n"
                       f"Gobernador: {cpu['gobernador']}", sub=ghz)

        g = self.hw.gpu_info()
        gmhz = "--" if g["mhz"] is None else f"{g['mhz']:.0f} MHz"
        f_gpu = (g["uso"] or 0) / 100
        self.m_gpu.set(pct(g["uso"]), f_gpu, load_accent(f_gpu),
                       f"Radeon 840M integrada (amdgpu)\n"
                       f"Reloj: {gmhz} de {g['mhz_max']} MHz máximo", sub=gmhz)

        r = self.hw.ram()
        f_ram = (r.get("usada") or 0) / r["total"] if r.get("total") else 0
        self.m_ram.set(gib(r.get("usada")), f_ram, load_accent(f_ram),
                       f"Disponible: {gib(r.get('disponible'))}\n"
                       f"En caché: {gib(r.get('cache'))}\n"
                       f"zram en uso: {mib(r.get('swap'))}",
                       sub=f"de {gib(r.get('total'))}")

        f_vram = (g["vram"] or 0) / g["vram_total"] if g["vram_total"] else 0
        mmhz = "--" if g["mem_mhz"] is None else f"{g['mem_mhz']} MHz"
        self.m_vram.set(mib(g["vram"]), f_vram, load_accent(f_vram),
                        f"Dedicada a la GPU: {mib(g['vram'])} de {mib(g['vram_total'])}\n"
                        f"Compartida con la RAM (GTT): {mib(g['gtt'])} de "
                        f"{gib(g['gtt_total'])}\n"
                        f"Reloj de memoria: {mmhz} de {g['mem_mhz_max']} MHz máximo",
                        sub=f"de {mib(g['vram_total'])}")

        rd, wr = g["dram_rd"], g["dram_wr"]
        bw = None if None in (rd, wr) else rd + wr
        f_bw = (bw or 0) / DRAM_ESCALA
        self.m_dram.set("--" if bw is None else f"{bw / 1000:.1f} GB/s",
                        f_bw, load_accent(f_bw),
                        ("Trafico de la memoria del sistema\n"
                         if bw is None else
                         f"Lectura {rd / 1000:.1f} GB/s · escritura {wr / 1000:.1f} GB/s\n")
                        + f"La barra usa {DRAM_ESCALA / 1000:.0f} GB/s de referencia, "
                        "el máximo medido en este equipo\n"
                        f"Reloj (UCLK): {mmhz} de {g['mem_mhz_max']} MHz máximo",
                        sub=f"a {mmhz}")

        cur = self.current_profile()
        for btn in self.group.buttons():
            btn.setChecked(btn.property("pid") == cur)
        for pid, act in self.tray_profiles.items():
            act.setChecked(pid == cur)


        if accent != self.tray_accent:
            self.tray_accent = accent
            self.tray.setIcon(asus_icon(accent))
        label = dict((p, l) for p, l, _ in PROFILES).get(cur, cur or "?")
        apu = temps.get("APU")
        apu_txt = "" if apu is None else f"  ·  APU {apu / 1000:.0f} °C"
        self.tray.setToolTip(f"{'--' if rpm is None else rpm} RPM{apu_txt}\n{label}")


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("ZenMonitor")
    app.setApplicationDisplayName("ZenMonitor")
    app.setDesktopFileName("zenmonitor")
    app.setWindowIcon(asus_icon(BLUE))
    app.setQuitOnLastWindowClosed(False)

    # Instancia unica: la tecla F12 y el lanzador vuelven a ejecutar este mismo
    # script, asi que si ya hay una copia corriendo le pedimos la ventana y salimos.
    ping = QLocalSocket()
    ping.connectToServer(IPC)
    if ping.waitForConnected(400):
        if "--tray" in sys.argv:      # el autoarranque no debe sacar la ventana
            return 0
        ping.write(b"toggle")
        ping.waitForBytesWritten(400)
        ping.disconnectFromServer()
        return 0

    QLocalServer.removeServer(IPC)      # socket huerfano de un cierre brusco
    server = QLocalServer()
    server.listen(IPC)

    win = ZenMonitor()
    server.newConnection.connect(lambda: win.on_ipc(server))
    if "--tray" not in sys.argv:
        win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
