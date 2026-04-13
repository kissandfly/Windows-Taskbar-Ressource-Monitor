__version__ = "1.4.1.0"

import ctypes
import os
import shutil
import sys
import subprocess
import winreg

from datetime import datetime

try:
    import distutils.spawn
except ModuleNotFoundError:
    import types

    distutils_fake = types.ModuleType('distutils')
    distutils_fake.spawn = types.ModuleType('spawn')

    def find_executable(exe):
        return shutil.which(exe)

    distutils_fake.spawn.find_executable = find_executable

    sys.modules['distutils'] = distutils_fake
    sys.modules['distutils.spawn'] = distutils_fake.spawn

if os.name == 'nt':
    CREATE_NO_WINDOW = 0x08000000
    _orig_popen = subprocess.Popen

    def _no_console_popen(*args, **kwargs):
        if "creationflags" not in kwargs:
            kwargs["creationflags"] = CREATE_NO_WINDOW
        return _orig_popen(*args, **kwargs)

    subprocess.Popen = _no_console_popen

import GPUtil
import psutil

from PyQt5.QtCore import (
    QDir,
    QSettings,
    QTimer,
    Qt,
)

from PyQt5.QtGui import (
    QColor,
    QFontMetrics,
    QIcon,
    QPainter,
)

from PyQt5.QtWidgets import (
    QAction,
    QApplication,
    QGraphicsOpacityEffect,
    QLabel,
    QInputDialog,
    QMenu,
    QSystemTrayIcon,
    QWidget,
)

JOURS_FR = {
    "Mon": "lun", "Tue": "mar", "Wed": "mer", "Thu": "jeu",
    "Fri": "ven", "Sat": "sam", "Sun": "dim",
}

MOIS_FR = {
    "January": "janvier", "February": "février", "March": "mars",
    "April": "avril", "May": "mai", "June": "juin",
    "July": "juillet", "August": "août", "September": "septembre",
    "October": "octobre", "November": "novembre", "December": "décembre",
}


class FILETIME(ctypes.Structure):
    """Windows FILETIME structure for GetSystemTimes API."""
    _fields_ = [
        ("dwLowDateTime", ctypes.c_uint32),
        ("dwHighDateTime", ctypes.c_uint32),
    ]


class MEMORYSTATUSEX(ctypes.Structure):
    """Windows MEMORYSTATUSEX structure for GlobalMemoryStatusEx API."""
    _fields_ = [
        ("dwLength", ctypes.c_uint32),
        ("dwMemoryLoad", ctypes.c_uint32),
        ("ullTotalPhys", ctypes.c_uint64),
        ("ullAvailPhys", ctypes.c_uint64),
        ("ullTotalPageFile", ctypes.c_uint64),
        ("ullAvailPageFile", ctypes.c_uint64),
        ("ullTotalVirtual", ctypes.c_uint64),
        ("ullAvailVirtual", ctypes.c_uint64),
        ("ullAvailExtendedVirtual", ctypes.c_uint64),
    ]


GetSystemTimes = ctypes.windll.kernel32.GetSystemTimes
GlobalMemoryStatusEx = ctypes.windll.kernel32.GlobalMemoryStatusEx

REG_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
REG_VALUE_NAME = "ResourceMonitor"


def _startup_folder():
    return os.path.join(
        os.getenv('APPDATA'),
        'Microsoft', 'Windows', 'Start Menu', 'Programs', 'Startup'
    )


def _startup_bat_path():
    return os.path.join(_startup_folder(), "ResourceMonitor.bat")


def add_to_startup(file_path=""):
    if not file_path:
        file_path = os.path.realpath(sys.argv[0])
    os.makedirs(_startup_folder(), exist_ok=True)
    bat_contents = f'@echo off\r\nstart "" "{file_path}"\r\n'
    try:
        with open(_startup_bat_path(), "w", encoding="utf-8") as bat:
            bat.write(bat_contents)
        print("Autostart configured via Startup .bat")
    except Exception as e:
        print("Failed to write Startup .bat:", e)


def remove_from_startup():
    bat_path = _startup_bat_path()
    try:
        if os.path.exists(bat_path):
            os.remove(bat_path)
            print("Autostart .bat removed.")
        else:
            print("Autostart .bat not found.")
    except Exception as e:
        print("Failed to remove Startup .bat:", e)
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, REG_RUN_KEY, 0, winreg.KEY_SET_VALUE
        ) as key:
            try:
                winreg.DeleteValue(key, REG_VALUE_NAME)
                print("Legacy registry autostart removed.")
            except FileNotFoundError:
                pass
    except Exception as e:
        print("Registry cleanup skipped/error:", e)


def is_autostart_enabled():
    bat_path = _startup_bat_path()
    if os.path.exists(bat_path):
        try:
            with open(bat_path, "r", encoding="utf-8") as bat:
                content = bat.read()
            current_exe = os.path.realpath(sys.argv[0])
            if f'"{current_exe}"' in content:
                return True
            return True
        except Exception:
            return True
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, REG_RUN_KEY, 0, winreg.KEY_READ
        ) as key:
            try:
                value, _ = winreg.QueryValueEx(key, REG_VALUE_NAME)
                if value:
                    return True
            except FileNotFoundError:
                pass
    except Exception:
        pass
    return False


class ResourceMonitor(QWidget):
    """Widget always-on-top affichant CPU, GPU, RAM, réseau et l'heure/date."""

    def __init__(self):
        super().__init__()
        self.COLOR_MODES = ["System", "Colored"]
        self.prev_idle = 0
        self.prev_kernel = 0
        self.prev_user = 0

        self.load_settings()
        self.tray_icon = None
        self.init_ui()
        self.old_pos = None
        self.check_gpu()
        self.prev_idle, self.prev_kernel, self.prev_user = self.get_system_times()

        # Init compteurs réseau
        _net = psutil.net_io_counters(pernic=False)
        self.prev_bytes_sent = _net.bytes_sent
        self.prev_bytes_recv = _net.bytes_recv

        # Timer horloge (1 seconde)
        self.clock_timer = QTimer(self)
        self.clock_timer.timeout.connect(self.update_datetime)
        self.clock_timer.start(1000)
        self.update_datetime()

    # -------------------------------------------------------------------------
    # Date / heure
    # -------------------------------------------------------------------------

    def update_datetime(self):
        """Mise à jour de l'affichage date/heure."""
        if not self.show_datetime:
            self.datetime_label.setVisible(False)
            return
        self.datetime_label.setVisible(True)
        now = datetime.now()
        day_abbr = JOURS_FR[now.strftime("%a")] + "."
        month = MOIS_FR[now.strftime("%B")]
        display = f"{now.strftime('%H.%M.%S')} {day_abbr} {now.day} {month}"
        self.datetime_label.setText(display)
        self.datetime_label.setStyleSheet(
            f"font-size: {self.font_size}px; color: #00BFFF;"
        )

    # -------------------------------------------------------------------------
    # UI
    # -------------------------------------------------------------------------

    def init_ui(self):
        self.setWindowFlags(
            Qt.FramelessWindowHint
            | Qt.WindowStaysOnTopHint
            | Qt.Tool
            | Qt.X11BypassWindowManagerHint
        )

        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setGeometry(self.window_x, self.window_y, self.window_width, self.window_height)

        icon_path = os.path.join(QDir.currentPath(), 'icon.png')
        self.setWindowIcon(QIcon(icon_path))

        # Label date/heure — à gauche
        self.datetime_label = QLabel('', self)
        self.datetime_label.setAlignment(Qt.AlignVCenter | Qt.AlignLeft)
        self.datetime_label.setStyleSheet(f"font-size: {self.font_size}px;")
        self.datetime_label.setVisible(self.show_datetime)

        self.cpu_label = QLabel('CPU: 0%', self)
        self.gpu_label = QLabel('GPU: 0%', self)
        self.ram_label = QLabel('RAM: 0%', self)
        self.vram_label = QLabel('VRAM: 0%', self)

        # Label réseau — à droite
        self.net_label = QLabel('UP: 0K/s  DN: 0K/s', self)

        self.update_font_size()
        self.update_text_opacity()
        self.update_colors()

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.update_metrics)
        self.timer.start(self.update_interval)

        self.setContextMenuPolicy(Qt.CustomContextMenu)
        self.customContextMenuRequested.connect(self.show_context_menu)

        self.tray_icon = QSystemTrayIcon(self)
        self.tray_icon.setIcon(QIcon(icon_path))
        self.tray_icon.activated.connect(self.restore_from_tray)

        tray_menu = QMenu(self)
        restore_action = QAction("Restore", self)
        restore_action.triggered.connect(self.show_widget)
        tray_menu.addAction(restore_action)
        quit_action = QAction("Quit", self)
        quit_action.triggered.connect(QApplication.instance().quit)
        tray_menu.addAction(quit_action)
        self.tray_icon.setContextMenu(tray_menu)

    # -------------------------------------------------------------------------
    # CPU / RAM helpers
    # -------------------------------------------------------------------------

    def get_system_times(self):
        idle_time = FILETIME()
        kernel_time = FILETIME()
        user_time = FILETIME()
        success = GetSystemTimes(
            ctypes.byref(idle_time),
            ctypes.byref(kernel_time),
            ctypes.byref(user_time),
        )
        if not success:
            return 0, 0, 0
        idle = (idle_time.dwHighDateTime << 32) | idle_time.dwLowDateTime
        kernel = (kernel_time.dwHighDateTime << 32) | kernel_time.dwLowDateTime
        user = (user_time.dwHighDateTime << 32) | user_time.dwLowDateTime
        return idle, kernel, user

    def get_cpu_usage(self):
        idle, kernel, user = self.get_system_times()
        idle_diff = idle - self.prev_idle
        kernel_diff = kernel - self.prev_kernel
        user_diff = user - self.prev_user
        total_diff = kernel_diff + user_diff
        self.prev_idle = idle
        self.prev_kernel = kernel
        self.prev_user = user
        if total_diff == 0:
            return 0.0
        return 100.0 * (1.0 - (idle_diff / float(total_diff)))

    def get_ram_usage(self):
        mem_status = MEMORYSTATUSEX()
        mem_status.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
        if not GlobalMemoryStatusEx(ctypes.byref(mem_status)):
            return 0.0
        return float(mem_status.dwMemoryLoad)

    @staticmethod
    def _fmt_rate(bps):
        """Formatte un débit en B/s, K/s ou M/s."""
        if bps >= 1_000_000:
            return f"{bps / 1_000_000:.1f}M"
        elif bps >= 1_000:
            return f"{bps / 1_000:.0f}K"
        return f"{bps:.0f}B"

    # -------------------------------------------------------------------------
    # Mise à jour des métriques
    # -------------------------------------------------------------------------

    def update_metrics(self):
        if self.show_cpu:
            cpu_usage = self.get_cpu_usage()
            self.cpu_label.setText(f'CPU: {cpu_usage:.0f}%')
            self.update_label_color(self.cpu_label, cpu_usage)
        else:
            self.cpu_label.setText("")

        if self.show_gpu or self.show_vram:
            gpus = GPUtil.getGPUs()
            if gpus:
                if self.show_gpu:
                    gpu_usage = gpus[0].load * 100
                    self.gpu_label.setText(f'GPU: {gpu_usage:.0f}%')
                    self.update_label_color(self.gpu_label, gpu_usage)
                else:
                    self.gpu_label.setText("")
                if self.show_vram:
                    vram_pct = gpus[0].memoryUtil * 100
                    self.vram_label.setText(f'VRAM: {vram_pct:.0f}%')
                    self.update_label_color(self.vram_label, vram_pct)
                else:
                    self.vram_label.setText("")
            else:
                self.gpu_label.setText("GPU: N/A" if self.show_gpu else "")
                self.vram_label.setText("VRAM: N/A" if self.show_vram else "")
        else:
            self.gpu_label.setText("")
            self.vram_label.setText("")

        if self.show_ram:
            ram_usage = self.get_ram_usage()
            self.ram_label.setText(f'RAM: {ram_usage:.0f}%')
            self.update_label_color(self.ram_label, ram_usage)
        else:
            self.ram_label.setText("")

        # Débit Ethernet Upload / Download
        if self.show_net:
            net = psutil.net_io_counters(pernic=False)
            elapsed = self.update_interval / 1000.0
            up_rate = (net.bytes_sent - self.prev_bytes_sent) / elapsed
            dn_rate = (net.bytes_recv - self.prev_bytes_recv) / elapsed
            self.prev_bytes_sent = net.bytes_sent
            self.prev_bytes_recv = net.bytes_recv
            self.net_label.setText(
                f"UP: {self._fmt_rate(up_rate)}/s  DN: {self._fmt_rate(dn_rate)}/s"
            )
            self.net_label.setStyleSheet(
                f"color: #00FF88; font-size: {self.font_size}px;"
            )
        else:
            self.net_label.setText("")

        self.raise_()
        self.activateWindow()

    def update_label_color(self, label, usage):
        if self.color_mode == "Colored":
            color = self.get_smooth_color_by_usage(usage)
            label.setStyleSheet(f"color: {color}; font-size: {self.font_size}px;")
        else:
            label.setStyleSheet(f"font-size: {self.font_size}px;")

    def get_smooth_color_by_usage(self, usage):
        green = (0, 255, 0)
        yellow = (255, 255, 0)
        red = (255, 0, 0)
        if usage < 50:
            return self.interpolate_color(green, yellow, usage / 50.0)
        return self.interpolate_color(yellow, red, (usage - 50) / 50.0)

    def interpolate_color(self, color1, color2, t):
        r = int(color1[0] + (color2[0] - color1[0]) * t)
        g = int(color1[1] + (color2[1] - color1[1]) * t)
        b = int(color1[2] + (color2[2] - color1[2]) * t)
        return f"rgb({r}, {g}, {b})"

    # -------------------------------------------------------------------------
    # Événements souris
    # -------------------------------------------------------------------------

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.old_pos = event.globalPos()

    def mouseMoveEvent(self, event):
        if event.buttons() == Qt.LeftButton:
            delta = event.globalPos() - self.old_pos
            self.move(self.x() + delta.x(), self.y() + delta.y())
            self.old_pos = event.globalPos()

    def closeEvent(self, event):
        self.save_settings()

    # -------------------------------------------------------------------------
    # Menu contextuel (clic droit)
    # -------------------------------------------------------------------------

    def show_context_menu(self, pos):
        context_menu = QMenu(self)

        # --- Toggles visibilité ---
        cpu_action = QAction(f"{'Hide' if self.show_cpu else 'Show'} CPU", self)
        cpu_action.triggered.connect(self.toggle_cpu)
        context_menu.addAction(cpu_action)

        gpu_action = QAction(f"{'Hide' if self.show_gpu else 'Show'} GPU", self)
        gpu_action.triggered.connect(self.toggle_gpu)
        context_menu.addAction(gpu_action)

        ram_action = QAction(f"{'Hide' if self.show_ram else 'Show'} RAM", self)
        ram_action.triggered.connect(self.toggle_ram)
        context_menu.addAction(ram_action)

        vram_action = QAction(f"{'Hide' if self.show_vram else 'Show'} VRAM", self)
        vram_action.triggered.connect(self.toggle_vram)
        context_menu.addAction(vram_action)

        net_action = QAction(f"{'Hide' if self.show_net else 'Show'} Ethernet", self)
        net_action.triggered.connect(self.toggle_net)
        context_menu.addAction(net_action)

        datetime_action = QAction(f"{'Hide' if self.show_datetime else 'Show'} Time-Date", self)
        datetime_action.triggered.connect(self.toggle_datetime)
        context_menu.addAction(datetime_action)

        context_menu.addSeparator()

        # --- Intervalle ---
        interval_menu = context_menu.addMenu("Update Interval")
        for interval in [1000, 2000, 5000]:
            action_interval = QAction(f"{interval // 1000} sec", self)
            action_interval.triggered.connect(lambda _, i=interval: self.change_update_interval(i))
            interval_menu.addAction(action_interval)

        # --- Police ---
        font_menu = context_menu.addMenu("Font Size")
        action_font = QAction("Custom Font Size", self)
        action_font.triggered.connect(self.change_font_size)
        font_menu.addAction(action_font)

        # --- Taille fenêtre ---
        size_menu = context_menu.addMenu("Size Settings")
        action_width = QAction("Change Width", self)
        action_width.triggered.connect(self.change_width)
        size_menu.addAction(action_width)
        action_height = QAction("Change Height", self)
        action_height.triggered.connect(self.change_height)
        size_menu.addAction(action_height)

        # --- Couleurs ---
        color_menu = context_menu.addMenu("Color Mode")
        for mode in self.COLOR_MODES:
            action_color_mode = QAction(mode, self)
            action_color_mode.triggered.connect(lambda _, m=mode: self.change_color_mode(m))
            color_menu.addAction(action_color_mode)

        # --- Opacité ---
        opacity_menu = context_menu.addMenu("Transparency Settings")
        action_bg_opacity = QAction("Change Background Opacity", self)
        action_bg_opacity.triggered.connect(self.change_background_opacity)
        opacity_menu.addAction(action_bg_opacity)
        action_txt_opacity = QAction("Change Text Opacity", self)
        action_txt_opacity.triggered.connect(self.change_text_opacity)
        opacity_menu.addAction(action_txt_opacity)

        context_menu.addSeparator()

        # --- Démarrage auto ---
        if self.is_in_startup():
            toggle_start = QAction("Disable Autostart", self)
            toggle_start.triggered.connect(self.disable_autostart)
        else:
            toggle_start = QAction("Enable Autostart", self)
            toggle_start.triggered.connect(self.enable_autostart)
        context_menu.addAction(toggle_start)

        # --- Quitter ---
        quit_action = QAction("Quit", self)
        quit_action.triggered.connect(QApplication.instance().quit)
        context_menu.addAction(quit_action)

        context_menu.exec_(self.mapToGlobal(pos))

    # -------------------------------------------------------------------------
    # Toggles
    # -------------------------------------------------------------------------

    def toggle_cpu(self):
        self.show_cpu = not self.show_cpu
        self.update_metrics()
        self.save_settings()

    def toggle_gpu(self):
        self.show_gpu = not self.show_gpu
        self.update_metrics()
        self.save_settings()

    def toggle_ram(self):
        self.show_ram = not self.show_ram
        self.update_metrics()
        self.save_settings()

    def toggle_vram(self):
        self.show_vram = not self.show_vram
        self.update_metrics()
        self.save_settings()

    def toggle_net(self):
        self.show_net = not self.show_net
        self.update_metrics()
        self.save_settings()

    def toggle_datetime(self):
        self.show_datetime = not self.show_datetime
        self.update_datetime()
        self.update()          # force repaint / center_metrics
        self.save_settings()

    # -------------------------------------------------------------------------
    # Paramètres
    # -------------------------------------------------------------------------

    def change_update_interval(self, interval):
        self.update_interval = interval
        self.timer.start(self.update_interval)
        self.save_settings()

    def change_font_size(self):
        font_size, ok = QInputDialog.getInt(
            self, "Change Font Size", "Enter new font size:", self.font_size, 8
        )
        if ok:
            self.font_size = font_size
            self.save_settings()
            self.update_font_size()

    def update_font_size(self):
        style = f"font-size: {self.font_size}px;"
        self.datetime_label.setStyleSheet(style)
        self.cpu_label.setStyleSheet(style)
        self.gpu_label.setStyleSheet(style)
        self.ram_label.setStyleSheet(style)
        self.vram_label.setStyleSheet(style)
        self.net_label.setStyleSheet(style)

    def update_text_opacity(self):
        for label in (
            self.datetime_label, self.cpu_label,
            self.gpu_label, self.ram_label, self.vram_label, self.net_label
        ):
            effect = QGraphicsOpacityEffect()
            effect.setOpacity(self.text_opacity / 100)
            label.setGraphicsEffect(effect)

    # -------------------------------------------------------------------------
    # Dessin
    # -------------------------------------------------------------------------

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        background_color = QColor(0, 0, 0, int(self.background_opacity * 2.55))
        painter.setBrush(background_color)
        painter.setPen(Qt.NoPen)
        painter.drawRoundedRect(self.rect(), 15, 15)
        self.center_metrics()

    def center_metrics(self):
        """
        Layout : [Date/Heure]  [CPU] [GPU] [RAM]  [UP/DN Ethernet]
        Chaque bloc peut être masqué indépendamment.
        """
        h = self.height()
        padding = 8

        # --- Gauche : Date/Heure ---
        if self.show_datetime and self.datetime_label.text():
            fm_dt = QFontMetrics(self.datetime_label.font())
            dt_w = fm_dt.width(self.datetime_label.text()) + 4
            dt_y = (h - self.datetime_label.height()) // 2
            self.datetime_label.setGeometry(padding, dt_y, dt_w, self.datetime_label.height())
            self.datetime_label.setVisible(True)
            left_edge = padding + dt_w + padding
        else:
            self.datetime_label.setVisible(False)
            left_edge = padding

        # --- Droite : Ethernet UP/DN ---
        net_text = self.net_label.text()
        if net_text:
            fm_net = QFontMetrics(self.net_label.font())
            net_w = fm_net.width(net_text) + 4
            net_y = (h - self.net_label.height()) // 2
            net_x = self.width() - net_w - padding
            self.net_label.setGeometry(net_x, net_y, net_w, self.net_label.height())
            right_edge = self.width() - net_w - padding * 2
        else:
            right_edge = self.width() - padding

        # --- Centre : CPU / GPU / RAM ---
        metrics = [self.cpu_label, self.gpu_label, self.ram_label, self.vram_label]
        active_metrics = [lbl for lbl in metrics if lbl.text() != ""]
        num_metrics = len(active_metrics)
        if num_metrics == 0:
            return

        remaining_width = right_edge - left_edge
        width_per_metric = remaining_width // num_metrics

        for i, label in enumerate(active_metrics):
            fm = QFontMetrics(label.font())
            label_width = fm.width(label.text())
            label_x = left_edge + width_per_metric * i + (width_per_metric - label_width) // 2
            label_y = (h - label.height()) // 2
            label.setGeometry(label_x, label_y, label_width, label.height())

    # -------------------------------------------------------------------------
    # Taille fenêtre
    # -------------------------------------------------------------------------

    def change_width(self):
        width, ok = QInputDialog.getInt(
            self, "Change Width", "Enter new width:", self.window_width, 1
        )
        if ok:
            self.window_width = width
            self.setFixedSize(self.window_width, self.window_height)
            self.save_settings()

    def change_height(self):
        height, ok = QInputDialog.getInt(
            self, "Change Height", "Enter new height:", self.window_height, 1
        )
        if ok:
            self.window_height = height
            self.setFixedSize(self.window_width, self.window_height)
            self.save_settings()

    def change_background_opacity(self):
        opacity, ok = QInputDialog.getInt(
            self, "Change Background Opacity",
            "Enter background opacity (0-100):", self.background_opacity, 0, 100
        )
        if ok:
            self.background_opacity = opacity
            self.update()
            self.save_settings()

    def change_text_opacity(self):
        opacity, ok = QInputDialog.getInt(
            self, "Change Text Opacity",
            "Enter text opacity (0-100):", self.text_opacity, 0, 100
        )
        if ok:
            self.text_opacity = opacity
            self.update_text_opacity()
            self.save_settings()

    def change_color_mode(self, mode):
        self.color_mode = mode
        self.update_colors()
        self.save_settings()

    def update_colors(self):
        self.update()

    # -------------------------------------------------------------------------
    # Autostart
    # -------------------------------------------------------------------------

    def is_in_startup(self):
        return is_autostart_enabled()

    def enable_autostart(self):
        add_to_startup()
        print("Autostart enabled.")

    def disable_autostart(self):
        remove_from_startup()
        print("Autostart disabled.")

    # -------------------------------------------------------------------------
    # Tray
    # -------------------------------------------------------------------------

    def restore_from_tray(self, reason):
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self.show_widget()

    def show_widget(self):
        self.show()
        self.setWindowFlags(self.windowFlags() | Qt.WindowStaysOnTopHint)
        self.showNormal()
        self.tray_icon.hide()

    # -------------------------------------------------------------------------
    # Persistance des réglages
    # -------------------------------------------------------------------------

    def save_settings(self):
        settings = QSettings("ResourceMonitor", "Settings")
        settings.setValue("show_cpu", self.show_cpu)
        settings.setValue("show_gpu", self.show_gpu)
        settings.setValue("show_ram", self.show_ram)
        settings.setValue("show_vram", self.show_vram)
        settings.setValue("show_net", self.show_net)
        settings.setValue("show_datetime", self.show_datetime)
        settings.setValue("update_interval", self.update_interval)
        settings.setValue("font_size", self.font_size)
        settings.setValue("window_width", self.window_width)
        settings.setValue("window_height", self.window_height)
        settings.setValue("background_opacity", self.background_opacity)
        settings.setValue("text_opacity", self.text_opacity)
        settings.setValue("color_mode", self.color_mode)
        settings.setValue("window_x", self.x())
        settings.setValue("window_y", self.y())

    def load_settings(self):
        settings = QSettings("ResourceMonitor", "Settings")
        self.show_cpu = settings.value("show_cpu", True, type=bool)
        self.show_gpu = settings.value("show_gpu", True, type=bool)
        self.show_ram = settings.value("show_ram", True, type=bool)
        self.show_vram = settings.value("show_vram", True, type=bool)
        self.show_net = settings.value("show_net", True, type=bool)
        self.show_datetime = settings.value("show_datetime", True, type=bool)
        self.update_interval = settings.value("update_interval", 2000, type=int)
        self.font_size = settings.value("font_size", 26, type=int)
        self.window_width = settings.value("window_width", 1100, type=int)
        self.window_height = settings.value("window_height", 32, type=int)
        self.background_opacity = settings.value("background_opacity", 20, type=int)
        self.text_opacity = settings.value("text_opacity", 100, type=int)
        self.color_mode = settings.value("color_mode", "Colored")
        self.window_x = settings.value("window_x", 100, type=int)
        self.window_y = settings.value("window_y", 100, type=int)

    # -------------------------------------------------------------------------
    # GPU check
    # -------------------------------------------------------------------------

    def check_gpu(self):
        try:
            gpus = GPUtil.getGPUs()
            if not gpus:
                self.show_gpu = False
                self.gpu_label.setText("GPU: N/A")
                self.save_settings()
        except ValueError as e:
            print("Failed to query GPU info:", e)
            self.show_gpu = False
            self.gpu_label.setText("GPU: N/A")
            self.save_settings()


if __name__ == '__main__':
    app = QApplication(sys.argv)
    monitor = ResourceMonitor()
    monitor.show()
    sys.exit(app.exec_())
