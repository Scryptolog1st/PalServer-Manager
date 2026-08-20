from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path
from datetime import datetime

from . import APP_NAME, __version__
from .config import load_config, save_config, FleetHostConfig
from .remote import manager_from_config
from .self_update import SelfUpdater
from .tools import TOOLS
from .settings import display_name_for, description_for, allowed_values_for, CHOICES, SECRET_KEYS
from .player_identity import platform_from_user_id
from .provisioning import LinuxHostBootstrapper, find_free_local_port
from .mod_catalog import ModCatalogService, ModCatalogError, build_managed_package


def _require_qt():
    try:
        import PySide6  # noqa: F401
    except ImportError as exc:
        raise SystemExit("GUI dependencies are not installed. Run: pip install 'palserver-manager[gui]'") from exc


_require_qt()

from PySide6.QtCore import QTimer, Qt, QRect, QRectF, QSize, QObject, Signal, QRunnable, QThreadPool
from PySide6.QtGui import QAction, QFont, QColor, QLinearGradient, QPainter, QPainterPath, QPen, QPixmap, QIcon
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
    QInputDialog,
    QFileDialog,
)


def pretty(data) -> str:
    return json.dumps(data, indent=2, default=str)


def version_key(value: str) -> tuple[int, int, int]:
    parts = [int(part) for part in re.findall(r"\d+", str(value or ""))[:3]]
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts[:3])


def human_bytes(value) -> str:
    if value in (None, ""):
        return "-"
    size = float(value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if size < 1024 or unit == "TiB":
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TiB"


class AsyncTaskSignals(QObject):
    result = Signal(object)
    error = Signal(str)
    finished = Signal()


class ProvisioningLogSignals(QObject):
    """Thread-safe bridge from SSH provisioning workers to the Qt console."""

    line = Signal(str)


class AsyncTask(QRunnable):
    """Run a blocking manager/API call without blocking Qt's event loop."""

    def __init__(self, fn):
        super().__init__()
        self.fn = fn
        self.signals = AsyncTaskSignals()

    def run(self):
        try:
            result = self.fn()
        except Exception as exc:
            self.signals.error.emit(str(exc))
        else:
            self.signals.result.emit(result)
        finally:
            self.signals.finished.emit()


class JsonDialog(QDialog):
    def __init__(self, title: str, data, parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(850, 650)
        layout = QVBoxLayout(self)
        editor = QPlainTextEdit(pretty(data))
        editor.setReadOnly(True)
        editor.setLineWrapMode(QPlainTextEdit.NoWrap)
        layout.addWidget(editor)
        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)


class SettingsDialog(QDialog):
    def __init__(self, manager, parent=None):
        super().__init__(parent)
        self.manager = manager
        self.rows = []
        self.setWindowTitle("Palworld Settings Manager")
        self.resize(1150, 720)
        layout = QVBoxLayout(self)
        top = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search setting name or description...")
        refresh = QPushButton("Search / Refresh")
        refresh.clicked.connect(self.reload)
        top.addWidget(self.search)
        top.addWidget(refresh)
        layout.addLayout(top)
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["Setting", "Value", "Category", "Description"])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.Stretch)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.doubleClicked.connect(self.edit_selected)
        layout.addWidget(self.table)
        bottom = QHBoxLayout()
        edit = QPushButton("Edit Selected")
        edit.clicked.connect(self.edit_selected)
        compare = QPushButton("Compare Defaults")
        compare.clicked.connect(self.compare_defaults)
        reset_selected = QPushButton("Reset Selected")
        reset_selected.clicked.connect(self.reset_selected)
        reset_all = QPushButton("Reset All to Defaults")
        reset_all.clicked.connect(self.reset_all)
        close = QPushButton("Close")
        close.clicked.connect(self.accept)
        bottom.addWidget(edit)
        bottom.addWidget(compare)
        bottom.addWidget(reset_selected)
        bottom.addWidget(reset_all)
        bottom.addStretch(1)
        bottom.addWidget(close)
        layout.addLayout(bottom)
        self.reload()

    def reload(self):
        try:
            self.rows = self.manager.settings(self.search.text().strip())
        except Exception as exc:
            QMessageBox.critical(self, "Settings", str(exc))
            return
        self.table.setRowCount(len(self.rows))
        for r, row in enumerate(self.rows):
            for c, value in enumerate((row["key"], row["display_value"], row["category"], row["description"])):
                self.table.setItem(r, c, QTableWidgetItem(str(value)))

    def edit_selected(self):
        row_index = self.table.currentRow()
        if row_index < 0 or row_index >= len(self.rows):
            return
        row = self.rows[row_index]
        value, ok = QInputDialog.getText(self, "Edit Setting", f"New value for {row['key']}:", QLineEdit.Normal, "")
        if not ok or value == "":
            return
        if QMessageBox.question(self, "Confirm", f"Save {row['key']} = {value}?\n\nA server restart may be required.") != QMessageBox.Yes:
            return
        try:
            result = self.manager.set_setting(row["key"], value)
            QMessageBox.information(self, "Saved", f"Saved and verified.\n\n{pretty(result)}")
            self.reload()
        except Exception as exc:
            QMessageBox.critical(self, "Save failed", str(exc))

    def compare_defaults(self):
        try:
            JsonDialog("Non-default Settings", self.manager.compare_defaults(), self).exec()
        except Exception as exc:
            QMessageBox.critical(self, "Compare Defaults", str(exc))

    def reset_selected(self):
        row_index = self.table.currentRow()
        if row_index < 0 or row_index >= len(self.rows):
            return
        key = self.rows[row_index]["key"]
        if QMessageBox.warning(self, "Reset Setting", f"Reset {key} to the Palworld default?", QMessageBox.Yes | QMessageBox.No) != QMessageBox.Yes:
            return
        try:
            result = self.manager.reset_defaults([key])
            QMessageBox.information(self, "Reset Setting", pretty(result))
            self.reload()
        except Exception as exc:
            QMessageBox.critical(self, "Reset Setting", str(exc))

    def reset_all(self):
        if QMessageBox.warning(self, "Reset All", "Reset every setting that exists in DefaultPalWorldSettings.ini? A backup is created first.", QMessageBox.Yes | QMessageBox.No) != QMessageBox.Yes:
            return
        try:
            result = self.manager.reset_defaults(None)
            QMessageBox.information(self, "Reset All", f"Reset {len(result.get('changes', []))} settings. A restart may be required.")
            self.reload()
        except Exception as exc:
            QMessageBox.critical(self, "Reset All", str(exc))


class BackupDialog(QDialog):
    def __init__(self, manager, parent=None):
        super().__init__(parent)
        self.manager = manager
        self.rows = []
        self.setWindowTitle("Backup Manager")
        self.resize(850, 560)
        layout = QVBoxLayout(self)
        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["Backup", "Size", "Created"])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        layout.addWidget(self.table)
        buttons = QHBoxLayout()
        for label, handler in (("Create Backup", self.create), ("Restore", self.restore), ("Delete", self.delete), ("Refresh", self.reload)):
            button = QPushButton(label)
            button.clicked.connect(handler)
            buttons.addWidget(button)
        buttons.addStretch(1)
        close = QPushButton("Close")
        close.clicked.connect(self.accept)
        buttons.addWidget(close)
        layout.addLayout(buttons)
        self.reload()

    def reload(self):
        try:
            self.rows = self.manager.backup_list()
        except Exception as exc:
            QMessageBox.critical(self, "Backups", str(exc))
            return
        self.table.setRowCount(len(self.rows))
        for index, row in enumerate(self.rows):
            created = datetime.fromtimestamp(float(row["created"])).strftime("%Y-%m-%d %I:%M:%S %p")
            for col, value in enumerate((row["name"], human_bytes(row["size"]), created)):
                self.table.setItem(index, col, QTableWidgetItem(str(value)))

    def create(self):
        try:
            self.manager.backup_create("manual")
            self.reload()
        except Exception as exc:
            QMessageBox.critical(self, "Create Backup", str(exc))

    def selected(self):
        row = self.table.currentRow()
        return self.rows[row] if 0 <= row < len(self.rows) else None

    def restore(self):
        row = self.selected()
        if not row:
            return
        if QMessageBox.warning(self, "Restore Backup", "Restoring will stop the server and replace current server data. Continue?", QMessageBox.Yes | QMessageBox.No) != QMessageBox.Yes:
            return
        try:
            self.manager.backup_restore(row["name"])
            QMessageBox.information(self, "Restore", "Backup restored successfully.")
        except Exception as exc:
            QMessageBox.critical(self, "Restore", str(exc))

    def delete(self):
        row = self.selected()
        if not row:
            return
        if QMessageBox.question(self, "Delete Backup", f"Delete {row['name']}?") != QMessageBox.Yes:
            return
        try:
            self.manager.backup_delete(row["name"])
            self.reload()
        except Exception as exc:
            QMessageBox.critical(self, "Delete", str(exc))


class PlayerDialog(QDialog):
    def __init__(self, manager, parent=None):
        super().__init__(parent)
        self.manager = manager
        self.rows = []
        self.setWindowTitle("Player Manager")
        self.resize(1000, 600)
        layout = QVBoxLayout(self)
        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(["Name", "User ID", "Level", "Ping", "IP", "Buildings"])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(5, QHeaderView.ResizeToContents)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        layout.addWidget(self.table)
        buttons = QHBoxLayout()
        for label, action in (("Refresh", self.reload), ("Kick", lambda: self.act("kick")), ("Ban", lambda: self.act("ban")), ("Unban by ID", self.unban), ("Broadcast", self.broadcast)):
            b = QPushButton(label)
            b.clicked.connect(action)
            buttons.addWidget(b)
        buttons.addStretch(1)
        close = QPushButton("Close")
        close.clicked.connect(self.accept)
        buttons.addWidget(close)
        layout.addLayout(buttons)
        self.reload()

    def reload(self):
        try:
            self.rows = self.manager.players()
        except Exception as exc:
            QMessageBox.critical(self, "Players", f"Palworld REST API unavailable or authentication failed.\n\n{exc}")
            return
        self.table.setRowCount(len(self.rows))
        fields = ("name", "userId", "level", "ping", "ip", "building_count")
        for r, row in enumerate(self.rows):
            for c, key in enumerate(fields):
                self.table.setItem(r, c, QTableWidgetItem(str(row.get(key, ""))))

    def selected(self):
        index = self.table.currentRow()
        return self.rows[index] if 0 <= index < len(self.rows) else None

    def act(self, action: str):
        row = self.selected()
        if not row:
            return
        user_id = str(row.get("userId", ""))
        default_message = f"{action.title()}ed by administrator"
        message, ok = QInputDialog.getText(self, action.title(), "Message:", QLineEdit.Normal, default_message)
        if not ok:
            return
        if QMessageBox.question(self, action.title(), f"{action.title()} {row.get('name')}?") != QMessageBox.Yes:
            return
        try:
            self.manager.player_action(action, user_id, message)
            self.reload()
        except Exception as exc:
            QMessageBox.critical(self, action.title(), str(exc))

    def unban(self):
        user_id, ok = QInputDialog.getText(self, "Unban", "User ID:")
        if ok and user_id:
            try:
                self.manager.player_action("unban", user_id)
            except Exception as exc:
                QMessageBox.critical(self, "Unban", str(exc))

    def broadcast(self):
        message, ok = QInputDialog.getText(self, "Broadcast", "Message:")
        if ok and message:
            try:
                self.manager.announce(message)
            except Exception as exc:
                QMessageBox.critical(self, "Broadcast", str(exc))


class SchedulerDialog(QDialog):
    def __init__(self, manager, parent=None):
        super().__init__(parent)
        self.manager = manager
        self.setWindowTitle("Automation / Scheduling")
        self.resize(620, 430)
        layout = QVBoxLayout(self)
        try:
            current = manager.scheduler_config()
        except Exception as exc:
            QMessageBox.critical(self, "Automation", str(exc))
            current = {"backup": {}, "updates": {}}
        backup = current.get("backup", {})
        updates = current.get("updates", {})
        form = QFormLayout()
        self.backup_enabled = QCheckBox(); self.backup_enabled.setChecked(bool(backup.get("enabled", True)))
        self.backup_interval = QSpinBox(); self.backup_interval.setRange(5, 10080); self.backup_interval.setValue(int(backup.get("interval_minutes", 120)))
        self.retention = QSpinBox(); self.retention.setRange(1, 1000); self.retention.setValue(int(backup.get("retention_count", 30)))
        self.auto_check = QCheckBox(); self.auto_check.setChecked(bool(updates.get("auto_check", True)))
        self.check_interval = QSpinBox(); self.check_interval.setRange(10, 10080); self.check_interval.setValue(int(updates.get("check_interval_minutes", 60)))
        self.auto_install = QCheckBox(); self.auto_install.setChecked(bool(updates.get("auto_install", False)))
        self.only_empty = QCheckBox(); self.only_empty.setChecked(bool(updates.get("only_when_empty", True)))
        self.window_start = QLineEdit(str(updates.get("maintenance_start", "04:00")))
        self.window_end = QLineEdit(str(updates.get("maintenance_end", "05:00")))
        form.addRow("Automatic backups", self.backup_enabled)
        form.addRow("Backup interval (minutes)", self.backup_interval)
        form.addRow("Backup retention count", self.retention)
        form.addRow("Automatic update checks", self.auto_check)
        form.addRow("Update check interval (minutes)", self.check_interval)
        form.addRow("Automatically install updates", self.auto_install)
        form.addRow("Only install when player count is zero", self.only_empty)
        form.addRow("Maintenance window start (HH:MM)", self.window_start)
        form.addRow("Maintenance window end (HH:MM)", self.window_end)
        layout.addLayout(form)
        note = QLabel("Scheduled jobs run inside the always-on PalServer Manager Agent. If you only run the desktop/CLI locally and the agent is not running, scheduled tasks will not execute.")
        note.setWordWrap(True); layout.addWidget(note)
        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.save); buttons.rejected.connect(self.reject); layout.addWidget(buttons)

    def save(self):
        payload = {
            "backup": {
                "enabled": self.backup_enabled.isChecked(),
                "interval_minutes": self.backup_interval.value(),
                "retention_count": self.retention.value(),
            },
            "updates": {
                "auto_check": self.auto_check.isChecked(),
                "check_interval_minutes": self.check_interval.value(),
                "auto_install": self.auto_install.isChecked(),
                "only_when_empty": self.only_empty.isChecked(),
                "maintenance_start": self.window_start.text().strip(),
                "maintenance_end": self.window_end.text().strip(),
            },
        }
        try:
            self.manager.scheduler_update(payload)
            self.accept()
        except Exception as exc:
            QMessageBox.critical(self, "Automation", str(exc))


class WorldDialog(QDialog):
    def __init__(self, manager, parent=None):
        super().__init__(parent)
        self.manager = manager
        self.rows = []
        self.setWindowTitle("World Manager")
        self.resize(900, 560)
        layout = QVBoxLayout(self)
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["World GUID", "Size", "Modified", "WorldOption.sav"])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeToContents)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        layout.addWidget(self.table)
        buttons = QHBoxLayout()
        for label, handler in (("Refresh", self.reload), ("Archive", self.archive), ("Delete", self.delete), ("Create Fresh World", self.new_world)):
            b = QPushButton(label); b.clicked.connect(handler); buttons.addWidget(b)
        buttons.addStretch(1)
        close = QPushButton("Close"); close.clicked.connect(self.accept); buttons.addWidget(close)
        layout.addLayout(buttons)
        self.reload()

    def reload(self):
        try:
            self.rows = self.manager.world_list()
        except Exception as exc:
            QMessageBox.critical(self, "Worlds", str(exc)); return
        self.table.setRowCount(len(self.rows))
        for i, row in enumerate(self.rows):
            values = (row["guid"], human_bytes(row["size"]), datetime.fromtimestamp(float(row["modified"])).strftime("%Y-%m-%d %I:%M:%S %p"), "Yes" if row.get("has_world_option") else "No")
            for c, value in enumerate(values): self.table.setItem(i, c, QTableWidgetItem(str(value)))

    def selected(self):
        idx = self.table.currentRow()
        return self.rows[idx] if 0 <= idx < len(self.rows) else None

    def archive(self):
        row = self.selected()
        if not row: return
        try:
            result = self.manager.world_archive(row["guid"]); QMessageBox.information(self, "World Archive", pretty(result))
        except Exception as exc: QMessageBox.critical(self, "World Archive", str(exc))

    def delete(self):
        row = self.selected()
        if not row: return
        if QMessageBox.warning(self, "Delete World", f"Delete {row['guid']}? A safety archive is created first.", QMessageBox.Yes | QMessageBox.No) != QMessageBox.Yes: return
        try:
            self.manager.world_delete(row["guid"]); self.reload()
        except Exception as exc: QMessageBox.critical(self, "Delete World", str(exc))

    def new_world(self):
        text = "Archive every current world, clear the world directory, and let Palworld create a fresh world on startup?"
        if QMessageBox.warning(self, "Create Fresh World", text, QMessageBox.Yes | QMessageBox.No) != QMessageBox.Yes: return
        try:
            result = self.manager.world_new(); QMessageBox.information(self, "Fresh World", pretty(result)); self.reload()
        except Exception as exc: QMessageBox.critical(self, "Fresh World", str(exc))


class WatchdogDialog(QDialog):
    def __init__(self, manager, parent=None):
        super().__init__(parent)
        self.manager = manager
        self.setWindowTitle("Live Server Watchdog")
        self.resize(1000, 760)
        layout = QVBoxLayout(self)
        self.summary = QLabel("Loading...")
        self.summary.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.summary.setFont(QFont("Consolas" if sys.platform == "win32" else "Monospace", 10))
        layout.addWidget(self.summary)
        self.logs = QPlainTextEdit()
        self.logs.setReadOnly(True)
        self.logs.setLineWrapMode(QPlainTextEdit.NoWrap)
        layout.addWidget(self.logs, 1)
        close = QPushButton("Close")
        close.clicked.connect(self.accept)
        layout.addWidget(close)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(2000)
        self.refresh()

    def refresh(self):
        try:
            status = self.manager.status(False)
            health = self.manager.health()
            process = status.get("process", {})
            service = status.get("service", {})
            lines = [
                f"Server      : {status.get('server_name', '-')}",
                f"Service     : {service.get('state', '-')}   PID: {service.get('pid', '-')}",
                f"Endpoint    : {status.get('lan_ip', '-')}:{status.get('game_port', '-')}/UDP   Socket: {'Listening' if status.get('udp_listening') else 'Not listening'}",
                f"Players     : {status.get('current_players', '-')}/{status.get('max_players', '-')}   FPS: {status.get('server_fps', '-')}",
                f"Health      : {str(health.get('overall', '-')).upper()}",
                f"VM CPU      : {health.get('cpu_percent', 0):.1f}%",
                f"VM RAM      : {human_bytes(health.get('memory_used'))} / {human_bytes(health.get('memory_total'))} ({health.get('memory_percent', 0):.1f}%)",
                f"Storage     : {human_bytes(health.get('disk_used'))} / {human_bytes(health.get('disk_total'))} ({health.get('disk_percent', 0):.1f}%)",
                f"PalServer   : CPU {process.get('cpu_percent', 0):.1f}%   RAM {human_bytes(process.get('rss'))}",
                f"Server Time : {datetime.now().astimezone().strftime('%Y-%m-%d %I:%M:%S %p %Z')}",
            ]
            self.summary.setText("\n".join(lines))
            self.logs.setPlainText("\n".join(self.manager.logs_tail(100)))
            self.logs.verticalScrollBar().setValue(self.logs.verticalScrollBar().maximum())
        except Exception as exc:
            self.summary.setText(f"Watchdog refresh failed: {exc}")


class ServerSetupDialog(QDialog):
    def __init__(self, manager, parent=None):
        super().__init__(parent)
        self.manager = manager
        self.setWindowTitle("Server Setup")
        self.resize(700, 560)
        layout = QVBoxLayout(self)
        try:
            current = manager.server_config()
        except Exception as exc:
            QMessageBox.critical(self, "Server Setup", str(exc))
            current = {}
        form = QFormLayout()
        self.install_dir = QLineEdit(str(current.get("install_dir", "")))
        self.steamcmd = QLineEdit(str(current.get("steamcmd_path", "")))
        self.steam_user = QLineEdit(str(current.get("steam_user", "palworld")))
        self.service_name = QLineEdit(str(current.get("service_name", "")))
        self.game_port = QSpinBox(); self.game_port.setRange(1,65535); self.game_port.setValue(int(current.get("game_port", 8211)))
        self.rest_host = QLineEdit(str(current.get("rest_api_host", "127.0.0.1")))
        self.rest_port = QSpinBox(); self.rest_port.setRange(1,65535); self.rest_port.setValue(int(current.get("rest_api_port", 8212)))
        self.rest_user = QLineEdit(str(current.get("rest_api_username", "admin")))
        self.admin_password = QLineEdit(); self.admin_password.setEchoMode(QLineEdit.Password); self.admin_password.setPlaceholderText("Leave blank to keep current password")
        self.sync_rest = QCheckBox("Enable/sync Palworld REST API settings in PalWorldSettings.ini")
        self.sync_rest.setChecked(True)
        form.addRow("Palworld install directory", self.install_dir)
        form.addRow("SteamCMD path", self.steamcmd)
        form.addRow("Linux Steam/Palworld user", self.steam_user)
        form.addRow("OS service name", self.service_name)
        form.addRow("Game listen port", self.game_port)
        form.addRow("Palworld REST host", self.rest_host)
        form.addRow("Palworld REST port", self.rest_port)
        form.addRow("REST username", self.rest_user)
        form.addRow("Admin password", self.admin_password)
        form.addRow("", self.sync_rest)
        layout.addLayout(form)
        note = QLabel("This edits the actual server host configuration even when you are connected remotely through the manager agent. If no matching Windows Service exists, Windows falls back to PalServer.exe process control. Linux prefers systemd when available.")
        note.setWordWrap(True); layout.addWidget(note)
        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.save); buttons.rejected.connect(self.reject); layout.addWidget(buttons)

    def save(self):
        payload = {
            "install_dir": self.install_dir.text().strip(),
            "steamcmd_path": self.steamcmd.text().strip(),
            "steam_user": self.steam_user.text().strip(),
            "service_name": self.service_name.text().strip(),
            "game_port": self.game_port.value(),
            "rest_api_host": self.rest_host.text().strip(),
            "rest_api_port": self.rest_port.value(),
            "rest_api_username": self.rest_user.text().strip(),
            "sync_palworld_rest": self.sync_rest.isChecked(),
        }
        if self.admin_password.text():
            payload["admin_password"] = self.admin_password.text()
        try:
            self.manager.update_server_config(payload)
            self.accept()
        except Exception as exc:
            QMessageBox.critical(self, "Server Setup", str(exc))


class ConnectionDialog(QDialog):
    def __init__(self, cfg, parent=None):
        super().__init__(parent)
        self.cfg = cfg
        self.setWindowTitle("Connection Manager")
        self.resize(700, 560)
        layout = QVBoxLayout(self)

        heading = QLabel("Remote Connection")
        heading.setObjectName("panelTitle")
        description = QLabel(
            "Use SSH Tunnel for secure off-network administration. Direct Agent URL is only used in Direct HTTPS mode."
        )
        description.setObjectName("mutedText")
        description.setWordWrap(True)
        layout.addWidget(heading)
        layout.addWidget(description)

        form = QFormLayout()
        form.setVerticalSpacing(11)
        self.mode = QComboBox()
        self.mode.addItems(["local", "direct", "ssh"])
        self.mode.setCurrentText(cfg.connection.mode)
        self.remote_url = QLineEdit(cfg.connection.remote_url)
        self.remote_url.setPlaceholderText("https://server.example.com:8765")
        self.token = QLineEdit(cfg.connection.remote_token)
        self.token.setEchoMode(QLineEdit.Password)
        self.token.setPlaceholderText("Agent token from the server host")
        self.verify_tls = QCheckBox("Verify TLS certificate")
        self.verify_tls.setChecked(cfg.connection.verify_tls)
        self.ssh_host = QLineEdit(cfg.connection.ssh_host)
        self.ssh_user = QLineEdit(cfg.connection.ssh_user)
        self.ssh_port = QSpinBox()
        self.ssh_port.setRange(1, 65535)
        self.ssh_port.setValue(cfg.connection.ssh_port)
        self.ssh_key = QLineEdit(cfg.connection.ssh_key_file)
        self.ssh_key.setPlaceholderText(r"C:\Users\You\.ssh\id_ed25519_palservermanager")
        self.ssh_local_port = QSpinBox()
        self.ssh_local_port.setRange(1, 65535)
        self.ssh_local_port.setValue(cfg.connection.ssh_local_port)
        self.ssh_remote_port = QSpinBox()
        self.ssh_remote_port.setRange(1, 65535)
        self.ssh_remote_port.setValue(cfg.connection.ssh_remote_agent_port)

        form.addRow("Connection mode", self.mode)
        form.addRow("Direct Agent URL", self.remote_url)
        form.addRow("Agent token", self.token)
        form.addRow("", self.verify_tls)
        form.addRow("SSH host", self.ssh_host)
        form.addRow("SSH user", self.ssh_user)
        form.addRow("SSH port", self.ssh_port)
        form.addRow("SSH private key", self.ssh_key)
        form.addRow("Local tunnel port", self.ssh_local_port)
        form.addRow("Remote agent port", self.ssh_remote_port)
        layout.addLayout(form)

        self.mode_hint = QLabel()
        self.mode_hint.setObjectName("sidebarStatus")
        self.mode_hint.setWordWrap(True)
        layout.addWidget(self.mode_hint)

        note = QLabel(
            "Security: keep the server agent on 127.0.0.1 whenever possible. SSH mode forwards a local Windows port "
            "to the server's loopback-only agent. Never expose Palworld's built-in REST API directly to the Internet."
        )
        note.setObjectName("mutedText")
        note.setWordWrap(True)
        layout.addWidget(note)

        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.mode.currentTextChanged.connect(self._sync_mode_ui)
        self._sync_mode_ui(self.mode.currentText())

    def _sync_mode_ui(self, mode):
        direct = mode == "direct"
        ssh = mode == "ssh"
        self.remote_url.setEnabled(direct)
        self.verify_tls.setEnabled(direct)
        self.token.setEnabled(direct or ssh)
        for widget in (self.ssh_host, self.ssh_user, self.ssh_port, self.ssh_key, self.ssh_local_port, self.ssh_remote_port):
            widget.setEnabled(ssh)
        if mode == "ssh":
            self.mode_hint.setText("SSH Tunnel selected — Direct Agent URL is ignored. Enter the Ubuntu agent token and SSH connection details below.")
        elif mode == "direct":
            self.mode_hint.setText("Direct HTTPS selected — configure a TLS-protected public agent URL. This mode should not use plain HTTP.")
        else:
            self.mode_hint.setText("Local selected — this computer is treated as the Palworld server host.")

    def apply(self):
        c = self.cfg.connection
        c.mode = self.mode.currentText()
        c.remote_url = self.remote_url.text().strip()
        c.remote_token = self.token.text()
        c.verify_tls = self.verify_tls.isChecked()
        c.ssh_host = self.ssh_host.text().strip()
        c.ssh_user = self.ssh_user.text().strip()
        c.ssh_port = self.ssh_port.value()
        c.ssh_key_file = self.ssh_key.text().strip()
        c.ssh_local_port = self.ssh_local_port.value()
        c.ssh_remote_agent_port = self.ssh_remote_port.value()



APP_QSS = r"""
QDialog#palworldInstallDialog { background: #070d18; color: #eef5ff; }
QMainWindow, QWidget#appRoot, QWidget#bodyRoot, QWidget#contentRoot,
QWidget#dashboardPage, QWidget#toolsPage, QWidget#logsPage, QWidget#aboutPage,
QWidget#playersPage, QWidget#worldsPage, QWidget#settingsPage, QWidget#modsPage, QWidget#backupsPage,
QWidget#automationPage, QWidget#healthPage, QWidget#diagnosticsPage,
QWidget#connectionPage, QWidget#reportPage, QWidget#watchdogPage, QWidget#serverSetupPage {
    background: #070d18;
    color: #eef5ff;
}
QWidget {
    font-family: "Segoe UI";
    font-size: 12px;
    color: #dce8f8;
}
QFrame#sidebar {
    background: qlineargradient(x1:0,y1:0,x2:1,y2:1, stop:0 #08101d, stop:1 #060c16);
    border-right: 1px solid #1a2a43;
}
QPushButton#navButton {
    background: transparent;
    color: #b7c7dd;
    border: 1px solid transparent;
    border-radius: 11px;
    padding: 10px 13px;
    text-align: left;
    font-size: 13px;
    font-weight: 650;
}
QPushButton#navButton:hover {
    background: #101f35;
    color: #ffffff;
    border-color: #1e3d67;
}
QPushButton#navButton:checked {
    background: #12366b;
    color: #ffffff;
    border: 1px solid #238df2;
}
QLabel#sidebarStatus {
    border-radius: 10px;
    padding: 9px 10px;
    font-size: 11px;
    font-weight: 700;
}
QLabel#sidebarStatus[connected="true"] { background: #09271c; color: #58ec9b; border: 1px solid #1c5d3d; }
QLabel#sidebarStatus[connected="false"] { background: #35171c; color: #ff8a94; border: 1px solid #76313a; }
QLabel#sideFinePrint { color: #71839c; font-size: 10px; }
QFrame#heroBanner { border-bottom: 1px solid #21385a; }
QLabel#heroTitle { color: #f8fbff; font-size: 40px; font-weight: 800; }
QLabel#heroSubtitle { color: #cbd8ee; font-size: 15px; font-weight: 500; }
QLabel#connectionInline {
    background: transparent;
    padding: 5px 5px;
    font-size: 12px;
    font-weight: 800;
}
QLabel#connectionInline[connected="true"] { color: #60ee9f; }
QLabel#connectionInline[connected="false"] { color: #ff6f79; }
QLabel#playerCountInline {
    background: transparent;
    color: #f4f8ff;
    border: 0;
    padding: 5px 7px;
    font-size: 12px;
    font-weight: 750;
}
QPushButton#heroButton {
    background: rgba(8, 20, 42, 220);
    color: #eaf3ff;
    border: 1px solid #2c4e7e;
    border-radius: 11px;
    padding: 9px 14px;
    min-height: 24px;
    font-size: 12px;
    font-weight: 650;
}
QPushButton#heroButton:hover { background: rgba(19, 41, 75, 235); border-color: #4385cf; }
QLabel#noticeBar {
    border-radius: 9px;
    padding: 9px 12px;
    font-weight: 700;
    margin-bottom: 8px;
}
QLabel#noticeBar[tone="info"] { background: #102744; color: #b9dcff; border: 1px solid #285d94; }
QLabel#noticeBar[tone="success"] { background: #0b3021; color: #73f2ad; border: 1px solid #23764d; }
QLabel#noticeBar[tone="warning"] { background: #443015; color: #ffd37a; border: 1px solid #886029; }
QLabel#noticeBar[tone="error"] { background: #421e24; color: #ff9ba4; border: 1px solid #873a45; }
QFrame#serverOverview, QFrame#panel, QFrame#metricCard, QFrame#detailCard {
    background: qlineargradient(x1:0,y1:0,x2:1,y2:1, stop:0 #0c1830, stop:1 #091426);
    border: 1px solid #223c67;
    border-radius: 16px;
}
QFrame#metricCard:hover, QFrame#detailCard:hover { border-color: #2a81d8; background: #0f1d35; }
QFrame#iconTile { background: #192653; border: 1px solid #2b4b85; border-radius: 15px; }
QLabel#iconGlyph { font-size: 30px; font-weight: 900; }
QLabel#serverName { color: #f6f9ff; font-size: 25px; font-weight: 800; }
QLabel#serverSubtitle { color: #aabbd3; font-size: 13px; }
QLabel#pageTitle { color: #f6f9ff; font-size: 23px; font-weight: 800; }
QLabel#pageSubtitle { color: #8fa3bf; font-size: 12px; }
QLabel#sectionTitle { color: #edf5ff; font-size: 15px; font-weight: 800; }
QLabel#pill { border-radius: 15px; padding: 7px 13px; font-size: 12px; font-weight: 800; }
QLabel#pill[tone="good"] { background: #0c3a27; color: #59efa0; border: 1px solid #1c7148; }
QLabel#pill[tone="warning"] { background: #4a3414; color: #ffcf69; border: 1px solid #8b6326; }
QLabel#pill[tone="bad"] { background: #482023; color: #ff929a; border: 1px solid #8b3b42; }
QLabel#summaryTitle { color: #8ea4c5; font-size: 11px; }
QLabel#summaryValue { color: #f4f8ff; font-size: 16px; font-weight: 800; }
QLabel#summaryDetail { color: #8fa3bf; font-size: 10px; }
QLabel#metricTitle { color: #a9bddb; font-size: 12px; font-weight: 700; }
QLabel#metricValue { color: #f6f9ff; font-size: 25px; font-weight: 800; }
QLabel#metricDetail { color: #b2c3dd; font-size: 11px; }
QLabel#metricBadgeGood { background: #0c3a27; color: #54ec99; border: 1px solid #1d7149; border-radius: 11px; padding: 3px 8px; font-size: 10px; font-weight: 800; }
QLabel#metricBadgeWarn { background: #4a3414; color: #ffd36e; border: 1px solid #8b6326; border-radius: 11px; padding: 3px 8px; font-size: 10px; font-weight: 800; }
QProgressBar#miniProgress { background: #1a2740; border: 0; border-radius: 4px; }
QProgressBar#miniProgress::chunk { background: qlineargradient(x1:0,y1:0,x2:1,y2:0, stop:0 #63ed98, stop:1 #2ec9ff); border-radius: 4px; }
QProgressBar#bigProgress { background: #1a2740; border: 0; border-radius: 6px; min-height: 12px; }
QProgressBar#bigProgress::chunk { background: qlineargradient(x1:0,y1:0,x2:1,y2:0, stop:0 #55ea96, stop:1 #31a9ff); border-radius: 6px; }
QLabel#panelTitle { color: #eef5ff; font-size: 13px; font-weight: 750; }
QLabel#panelMuted { color: #8499b7; font-size: 10px; }
QLabel#healthBig { color: #55ee9a; font-size: 20px; font-weight: 800; }
QLabel#healthDotGood { color: #26da79; font-size: 14px; }
QLabel#healthDotWarn { color: #ffc34d; font-size: 14px; }
QLabel#healthDotBad { color: #ff6670; font-size: 14px; }
QLabel#rowLabel { color: #9fb2cf; font-size: 11px; }
QLabel#rowValue { color: #edf4ff; font-size: 11px; font-weight: 650; }
QLabel#detailTitle { color: #f5f9ff; font-size: 18px; font-weight: 800; }
QLabel#detailKey { color: #7189aa; font-size: 10px; font-family: Consolas, monospace; }
QLabel#detailDescription { color: #b9cbe2; font-size: 12px; line-height: 1.35; }
QLabel#cardStatusGood { color:#58ed9c; font-size:11px; font-weight:800; }
QLabel#cardStatusWarn { color:#ffd16b; font-size:11px; font-weight:800; }
QLabel#cardStatusBad { color:#ff8e98; font-size:11px; font-weight:800; }
QLabel#reportMetricValue { color:#f6f9ff; font-size:30px; font-weight:850; }
QLabel#reportMetricLabel { color:#9db3d1; font-size:12px; font-weight:700; }
QLabel#allowedValues { background: #091525; color: #9fd2ff; border: 1px solid #21436b; border-radius: 9px; padding: 10px; font-size: 11px; }
QPushButton {
    min-height: 36px;
    border-radius: 10px;
    padding: 8px 13px;
    font-size: 12px;
    font-weight: 700;
}
QPushButton[compactAction="true"] { min-width: 130px; max-width: 220px; }
QPushButton#successButton { background: #0e754b; color: #effff6; border: 1px solid #20c77d; }
QPushButton#successButton:hover { background: #11905b; }
QPushButton#dangerButton { background: #8d2630; color: #fff1f2; border: 1px solid #e3515d; }
QPushButton#dangerButton:hover { background: #aa303c; }
QPushButton#warningButton { background: #98601a; color: #fff4dd; border: 1px solid #e9a33b; }
QPushButton#warningButton:hover { background: #b97620; }
QPushButton#primaryButton { background: #1358bf; color: #edf6ff; border: 1px solid #3d88ed; }
QPushButton#primaryButton:hover { background: #196bdc; }
QPushButton#purpleButton { background: #592ba1; color: #f6eeff; border: 1px solid #8e5bd0; }
QPushButton#purpleButton:hover { background: #6d36c0; }
QPushButton#tealButton { background: #126173; color: #edfeff; border: 1px solid #2c9db3; }
QPushButton#tealButton:hover { background: #17778b; }
QPushButton#smallGhostButton, QPushButton#ghostButton {
    background: #101e34; color: #dbe9fb; border: 1px solid #2a4770; border-radius: 10px;
    padding: 6px 11px; min-height: 28px; font-size: 11px;
}
QPushButton#smallGhostButton:hover, QPushButton#ghostButton:hover { background: #172b49; border-color: #3d73ad; }
QFrame#toolCard { background: #0c182c; border: 1px solid #223f6a; border-radius: 14px; }
QFrame#toolCard:hover { background: #10213b; border-color: #2b91e8; }
QLabel#toolCardIcon { color: #9cc8ff; font-size: 30px; font-weight: 900; }
QLabel#toolCardTitle { color: #f1f6ff; font-size: 13px; font-weight: 800; }
QLabel#toolCardDescription { color: #b4c6df; font-size: 11px; }
QPlainTextEdit#dashboardLog, QPlainTextEdit#fullLog, QPlainTextEdit { background: #060d18; color: #d9e6f6; border: 1px solid #1e385c; border-radius: 10px; padding: 8px; font-family: Consolas, "Cascadia Mono", monospace; font-size: 11px; selection-background-color: #1c5b95; }
QTableWidget { background: #08111e; alternate-background-color: #0b1728; color: #dfeafb; border: 1px solid #1f3b63; border-radius: 10px; gridline-color: #18304f; selection-background-color: #164c80; }
QHeaderView::section { background: #111f35; color: #a9bdd8; padding: 8px; border: 0; border-bottom: 1px solid #27476f; font-size: 10px; font-weight: 700; }
QLineEdit, QComboBox, QSpinBox { background: #091525; color: #edf5ff; border: 1px solid #274365; border-radius: 8px; padding: 8px; min-height: 24px; }
QLineEdit:focus, QComboBox:focus, QSpinBox:focus { border-color: #3499e9; }
QCheckBox { spacing: 8px; color: #c8d7ea; }
QStackedWidget, QScrollArea, QAbstractScrollArea, QAbstractScrollArea::viewport { background: #070d18; border: 0; }
QScrollArea > QWidget > QWidget { background: #070d18; }
QWidget#toolsBody, QWidget#diagnosticsBody, QWidget#reportBody { background: #070d18; }
QScrollArea { background: #070d18; border: 0; }
QScrollBar:vertical { background: #070f1b; width: 10px; margin: 0; }
QScrollBar::handle:vertical { background: #29486f; min-height: 28px; border-radius: 5px; }
QScrollBar::handle:vertical:hover { background: #3c6c9f; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
"""


class HeroBanner(QFrame):
    def __init__(self, image_path: str, parent=None):
        super().__init__(parent)
        self.setObjectName("heroBanner")
        self.setFixedHeight(155)
        self._pixmap = QPixmap(image_path)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        rect = self.rect()
        painter.fillRect(rect, QColor("#081321"))
        if not self._pixmap.isNull():
            scaled = self._pixmap.scaled(rect.size(), Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
            x = max(0, (scaled.width() - rect.width()) // 2)
            y = max(0, (scaled.height() - rect.height()) // 2)
            painter.drawPixmap(rect, scaled, QRect(x, y, rect.width(), rect.height()))
        horizontal = QLinearGradient(0, 0, rect.width(), 0)
        horizontal.setColorAt(0.0, QColor(4, 10, 21, 238))
        horizontal.setColorAt(0.28, QColor(5, 13, 26, 170))
        horizontal.setColorAt(0.58, QColor(6, 14, 28, 72))
        horizontal.setColorAt(0.82, QColor(5, 13, 26, 145))
        horizontal.setColorAt(1.0, QColor(3, 9, 20, 235))
        painter.fillRect(rect, horizontal)
        vertical = QLinearGradient(0, 0, 0, rect.height())
        vertical.setColorAt(0.0, QColor(4, 10, 20, 28))
        vertical.setColorAt(1.0, QColor(4, 10, 20, 150))
        painter.fillRect(rect, vertical)
        super().paintEvent(event)


class PillBadge(QLabel):
    def __init__(self, text="-", tone="good", parent=None):
        super().__init__(text, parent)
        self.setObjectName("pill")
        self.setAlignment(Qt.AlignCenter)
        self.set_tone(tone)

    def set_tone(self, tone: str):
        self.setProperty("tone", tone)
        self.style().unpolish(self)
        self.style().polish(self)

    def set_state(self, text: str, tone: str):
        self.setText(text)
        self.set_tone(tone)


class MetricCard(QFrame):
    def __init__(self, title: str, icon: str, icon_color: str, parent=None):
        super().__init__(parent)
        self.setObjectName("metricCard")
        self.setMinimumWidth(145)
        self.setFixedHeight(140)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(5)
        top = QHBoxLayout()
        top.setSpacing(10)
        self.icon = QLabel(icon)
        self.icon.setObjectName("iconGlyph")
        self.icon.setStyleSheet(f"color: {icon_color}; font-size: 29px;")
        self.icon.setFixedWidth(36)
        top.addWidget(self.icon)
        self.title = QLabel(title)
        self.title.setObjectName("metricTitle")
        top.addWidget(self.title)
        top.addStretch(1)
        layout.addLayout(top)
        self.value = QLabel("-")
        self.value.setObjectName("metricValue")
        self.value.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout.addWidget(self.value)
        self.detail = QLabel("")
        self.detail.setObjectName("metricDetail")
        self.detail.setWordWrap(True)
        layout.addWidget(self.detail)
        self.badge = QLabel("")
        self.badge.hide()
        layout.addWidget(self.badge, 0, Qt.AlignLeft)
        self.progress = QProgressBar()
        self.progress.setObjectName("miniProgress")
        self.progress.setRange(0, 100)
        self.progress.setTextVisible(False)
        self.progress.setFixedHeight(7)
        self.progress.hide()
        layout.addWidget(self.progress)
        layout.addStretch(1)

    def set_data(self, value, detail="", progress=None, badge_text="", badge_good=True):
        self.value.setText(str(value))
        self.detail.setText(str(detail))
        if progress is None:
            self.progress.hide()
        else:
            self.progress.setValue(max(0, min(100, int(float(progress)))))
            self.progress.show()
        if badge_text:
            self.badge.setObjectName("metricBadgeGood" if badge_good else "metricBadgeWarn")
            self.badge.setText(str(badge_text))
            self.badge.style().unpolish(self.badge)
            self.badge.style().polish(self.badge)
            self.badge.show()
        else:
            self.badge.hide()


class ToolNavCard(QFrame):
    """Responsive navigation card used by the Tools page.

    Tool cards intentionally navigate to a page/report only. Direct server
    actions are never rendered in the Tools grid.
    """
    def __init__(self, title: str, description: str, symbol: str, callback, parent=None):
        super().__init__(parent)
        self.setObjectName("toolCard")
        self.setCursor(Qt.PointingHandCursor)
        self.setMinimumHeight(64)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self._callback = callback

        layout = QHBoxLayout(self)
        layout.setContentsMargins(13, 9, 13, 9)
        layout.setSpacing(11)

        icon = QLabel(symbol)
        icon.setObjectName("toolCardIcon")
        icon.setAlignment(Qt.AlignTop | Qt.AlignHCenter)
        icon.setFixedWidth(36)
        layout.addWidget(icon)

        copy = QVBoxLayout()
        copy.setSpacing(2)
        title_label = QLabel(title)
        title_label.setObjectName("toolCardTitle")
        title_label.setWordWrap(True)
        desc_label = QLabel(description)
        desc_label.setObjectName("toolCardDescription")
        desc_label.setWordWrap(True)
        copy.addWidget(title_label)
        copy.addWidget(desc_label)
        copy.addStretch(1)
        layout.addLayout(copy, 1)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton and self.rect().contains(event.position().toPoint()):
            if self._callback:
                self._callback()
        super().mouseReleaseEvent(event)


class MainWindow(QMainWindow):
    # Tiles in Tools must be navigation/report/configuration only. These IDs
    # are intentionally excluded because clicking them performs an immediate
    # server/manager action instead of opening a dedicated page.
    DIRECT_ACTION_TOOL_IDS = {
        "start", "stop", "restart", "save", "update-check", "update",
        "broadcast", "self-update",
    }
    # These capabilities remain available inside the Settings page, but they
    # are intentionally not duplicated as standalone Tools tiles.
    HIDDEN_TOOL_IDS = {"search", "profiles"}

    PAGE_ORDER = [
        "dashboard", "instances", "hosts", "tools", "players", "worlds", "settings", "mods", "backups",
        "automation", "logs", "health", "diagnostics", "connection", "about",
        "report", "watchdog", "server_setup",
    ]

    SERVER_CONTEXT_PAGES = {
        "dashboard", "players", "worlds", "settings", "mods", "backups", "automation",
        "logs", "health", "diagnostics", "watchdog", "server_setup",
    }

    ICONS = {
        "dashboard": "\u2302", "instances": "\u25a3", "hosts": "\u26c1", "tools": "\u25a6", "players": "\u265f", "worlds": "\u25ce",
        "settings": "\u2699", "mods": "\u2692", "backups": "\u25a4", "automation": "\u25f7", "logs": "\u25a7",
        "health": "\u2665", "diagnostics": "\u2692", "connection": "\u2197", "about": "i",
        "report": "\u2630", "watchdog": "\u25c9", "server_setup": "\u2699",
        "refresh": "\u21bb", "search": "\u2315", "save": "\u25a3", "delete": "x",
        "play": ">", "stop": "\u25a0", "restart": "\u21bb", "update": "\u2193",
        "broadcast": "\u2709", "edit": "\u270e", "archive": "\u25a5",
    }

    def __init__(self):
        super().__init__()
        self.cfg = load_config()
        self.manager = manager_from_config(self.cfg)
        self.tool_page = 0
        self._last_tool_columns = None
        self.last_update_result = None
        self.last_update_check = 0.0
        self.nav_buttons = {}
        self.pages = {}
        self.auto_scroll_logs = True
        self.pending_confirmation = None
        self.pending_confirmation_until = 0.0
        self.settings_rows = []
        self.filtered_settings_rows = []
        self.player_rows = []
        self.backup_rows = []
        self.world_rows = []
        self.ban_rows = []
        self.instance_rows = []
        self.visible_instance_rows = []
        self._loading_node_selector = False
        self._loading_instance_selector = False
        self._async_busy = set()
        self._async_tasks = {}
        self._overview_cache = None
        self._overview_cache_at = 0.0
        self.thread_pool = QThreadPool(self)
        self.thread_pool.setMaxThreadCount(3)
        self.setWindowTitle(f"{APP_NAME} v{__version__}")
        self.resize(1536, 1024)
        self.setMinimumSize(1000, 700)
        self._build_ui()
        # Remote/server calls never run on the Qt event loop.  This keeps
        # navigation instant even when the SSH tunnel or Palworld REST API is
        # briefly slow.
        QTimer.singleShot(0, lambda: self.refresh_instances(silent=True, refresh_after=True))
        self.refresh_timer = QTimer(self)
        self.refresh_timer.timeout.connect(self._periodic_refresh)
        self.refresh_timer.start(30000)
        # Watchdog telemetry is still live, but three seconds is frequent
        # enough for server administration and avoids hammering the agent.
        self.watchdog_timer = QTimer(self)
        self.watchdog_timer.timeout.connect(self._watchdog_tick)
        self.watchdog_timer.start(3000)

    def closeEvent(self, event):
        try:
            self.refresh_timer.stop()
            self.watchdog_timer.stop()
        except Exception:
            pass
        if hasattr(self, "thread_pool"):
            self.thread_pool.clear()
            self.thread_pool.waitForDone(1200)
        if hasattr(self.manager, "close"):
            try:
                self.manager.close()
            except Exception:
                pass
        event.accept()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        QTimer.singleShot(0, self._apply_responsive_layout)

    def _apply_responsive_layout(self):
        if not hasattr(self, "stack"):
            return
        width = self.width()
        if hasattr(self, "sidebar"):
            self.sidebar.setFixedWidth(230 if width >= 1250 else 190)
        if hasattr(self, "hero_title"):
            title_size = 40 if width >= 1350 else (34 if width >= 1120 else 29)
            self.hero_title.setStyleSheet(f"color:#f8fbff; font-size:{title_size}px; font-weight:800;")
        if hasattr(self, "hero_subtitle_label"):
            self.hero_subtitle_label.setVisible(width >= 1080)
        if hasattr(self, "tools_grid"):
            columns = self._tool_columns()
            if columns != self._last_tool_columns:
                self.tool_page = 0
                self.render_tools()
        # Keep platform/player identity visible while progressively hiding
        # lower-priority columns on narrow windows.
        content_width = self.stack.width() if hasattr(self, "stack") else width
        for table_name in ("players_table", "players_page_table"):
            table = getattr(self, table_name, None)
            if table is not None and table.columnCount() >= 8:
                table.setColumnHidden(4, content_width < 1120)  # account name
                table.setColumnHidden(6, content_width < 930)   # IP address
                table.setColumnHidden(7, content_width < 820)   # map location
        if hasattr(self, "bans_table"):
            self.bans_table.setColumnHidden(2, content_width < 980)  # account
            self.bans_table.setColumnHidden(4, content_width < 820)  # reason
        if hasattr(self, "instances_table") and self.instances_table.columnCount() >= 8:
            self.instances_table.setColumnHidden(7, content_width < 1050)  # install directory
            self.instances_table.setColumnHidden(2, content_width < 900)   # host name
        self._relayout_dashboard_metrics()

    def _asset_path(self, name: str) -> str:
        return str(Path(__file__).resolve().parent / "assets" / name)

    def _symbol_icon(self, symbol: str, color="#9cc8ff", size=28):
        pixmap = QPixmap(size, size)
        pixmap.fill(Qt.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(QColor(color))
        font = QFont("Segoe UI Symbol", max(12, int(size * 0.62)), QFont.Bold)
        painter.setFont(font)
        painter.drawText(pixmap.rect(), Qt.AlignCenter, symbol)
        painter.end()
        return QIcon(pixmap)

    def _set_button_icon(self, button, symbol, color="#9cc8ff", size=24):
        button.setIcon(self._symbol_icon(symbol, color, size + 6))
        button.setIconSize(QSize(size, size))

    def _build_ui(self):
        root = QWidget()
        root.setObjectName("appRoot")
        self.setCentralWidget(root)
        root_layout = QVBoxLayout(root)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)

        self.hero = HeroBanner(self._asset_path("hero_clean.png"))
        hero_layout = QHBoxLayout(self.hero)
        hero_layout.setContentsMargins(26, 12, 20, 12)
        hero_layout.setSpacing(16)

        logo = QLabel()
        self.hero_logo = logo
        logo_pix = QPixmap(self._asset_path("brand_logo.png"))
        if not logo_pix.isNull():
            logo.setPixmap(logo_pix.scaled(108, 98, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        logo.setFixedSize(114, 104)
        logo.setAlignment(Qt.AlignCenter)
        hero_layout.addWidget(logo)

        title_stack = QVBoxLayout()
        title_stack.setSpacing(2)
        title_stack.addStretch(1)
        title = QLabel("PalServer Manager")
        self.hero_title = title
        title.setObjectName("heroTitle")
        subtitle = QLabel("Palworld Dedicated Server Management")
        self.hero_subtitle_label = subtitle
        subtitle.setObjectName("heroSubtitle")
        title_stack.addWidget(title)
        title_stack.addWidget(subtitle)
        title_stack.addStretch(1)
        hero_layout.addLayout(title_stack, 1)

        hero_controls = QHBoxLayout()
        hero_controls.setSpacing(8)
        self.node_selector = QComboBox()
        self.node_selector.setObjectName("nodeSelector")
        self.node_selector.setMinimumWidth(145)
        self.node_selector.setMaximumWidth(220)
        self.node_selector.setToolTip("Select the Linux node/agent host to manage")
        self.node_selector.currentIndexChanged.connect(self._node_selector_changed)
        self.node_selector.setVisible(bool(getattr(self.cfg, "fleet_hosts", [])))
        hero_controls.addWidget(self.node_selector)
        self.instance_selector = QComboBox()
        self.instance_selector.setObjectName("instanceSelector")
        self.instance_selector.setMinimumWidth(150)
        self.instance_selector.setMaximumWidth(230)
        self.instance_selector.setToolTip("Select a Palworld server on the selected node")
        self.instance_selector.currentIndexChanged.connect(self._instance_selector_changed)
        hero_controls.addWidget(self.instance_selector)
        self.connection_label = QLabel("\u25cf SSH")
        self.connection_label.setObjectName("connectionInline")
        self.connection_label.setProperty("connected", True)
        self.connection_label.setAlignment(Qt.AlignCenter)
        hero_controls.addWidget(self.connection_label)
        self.player_count_header = QLabel("Players  - / -")
        self.player_count_header.setObjectName("playerCountInline")
        self.player_count_header.setAlignment(Qt.AlignCenter)
        hero_controls.addWidget(self.player_count_header)
        for text, symbol, page in (
            ("Connection", self.ICONS["connection"], "connection"),
            ("Settings", self.ICONS["settings"], "settings"),
        ):
            button = QPushButton(text)
            button.setObjectName("heroButton")
            self._set_button_icon(button, symbol, size=21)
            button.clicked.connect(lambda checked=False, p=page: self.show_named_page(p))
            hero_controls.addWidget(button)
        refresh = QPushButton("Refresh")
        refresh.setObjectName("heroButton")
        self._set_button_icon(refresh, self.ICONS["refresh"], size=21)
        refresh.clicked.connect(self.refresh_current_page)
        hero_controls.addWidget(refresh)
        hero_layout.addLayout(hero_controls)
        root_layout.addWidget(self.hero)

        body = QWidget()
        body.setObjectName("bodyRoot")
        body_layout = QHBoxLayout(body)
        body_layout.setContentsMargins(0, 0, 0, 0)
        body_layout.setSpacing(0)

        sidebar = QFrame()
        self.sidebar = sidebar
        sidebar.setObjectName("sidebar")
        sidebar.setFixedWidth(230)
        side = QVBoxLayout(sidebar)
        side.setContentsMargins(12, 18, 12, 16)
        side.setSpacing(5)
        nav_defs = [
            ("dashboard", "Dashboard"), ("instances", "Servers"), ("hosts", "Remote Hosts"), ("tools", "Tools"), ("players", "Players"),
            ("worlds", "Worlds"), ("settings", "Settings"), ("mods", "Mods"), ("backups", "Backups"),
            ("automation", "Automation"), ("logs", "Logs"), ("watchdog", "Live Watchdog"),
            ("health", "Health"), ("diagnostics", "Diagnostics"),
            ("connection", "Connection"), ("about", "About"),
        ]
        for page, text in nav_defs:
            self._add_nav(side, page, text, self.ICONS[page])
        side.addStretch(1)
        self.sidebar_status = QLabel("\u25cf Connected")
        self.sidebar_status.setObjectName("sidebarStatus")
        self.sidebar_status.setProperty("connected", True)
        self.sidebar_status.setWordWrap(True)
        side.addWidget(self.sidebar_status)
        fine = QLabel("Created and developed by Supr Solutions LLC")
        fine.setObjectName("sideFinePrint")
        fine.setWordWrap(True)
        side.addWidget(fine)
        body_layout.addWidget(sidebar)

        content = QWidget()
        content.setObjectName("contentRoot")
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(18, 12, 16, 16)
        content_layout.setSpacing(0)
        self.notice_bar = QLabel("")
        self.notice_bar.setObjectName("noticeBar")
        self.notice_bar.setProperty("tone", "info")
        self.notice_bar.setWordWrap(True)
        self.notice_bar.hide()
        content_layout.addWidget(self.notice_bar)
        self.stack = QStackedWidget()
        self.tabs = self.stack
        content_layout.addWidget(self.stack, 1)
        body_layout.addWidget(content, 1)
        root_layout.addWidget(body, 1)

        self._build_dashboard_page()
        self._build_instances_page()
        self._build_hosts_page()
        self._build_tools_page()
        self._build_players_page()
        self._build_worlds_page()
        self._build_settings_page()
        self._build_mods_page()
        self._build_backups_page()
        self._build_automation_page()
        self._build_logs_page()
        self._build_health_page()
        self._build_diagnostics_page()
        self._build_connection_page()
        self._build_about_page()
        self._build_report_page()
        self._build_watchdog_page()
        self._build_server_setup_page()
        self.setStyleSheet(APP_QSS)
        self.show_named_page("dashboard")

    def _register_page(self, name, widget):
        self.pages[name] = self.stack.addWidget(widget)

    def _add_nav(self, layout, page, text, symbol):
        button = QPushButton(text)
        button.setObjectName("navButton")
        button.setCheckable(True)
        self._set_button_icon(button, symbol, size=29)
        button.clicked.connect(lambda checked=False, p=page: self.show_named_page(p))
        layout.addWidget(button)
        self.nav_buttons[page] = button

    def show_named_page(self, name):
        if name not in self.pages:
            return
        if name in self.SERVER_CONTEXT_PAGES and self._fleet_mode() and not self._server_context_available():
            node = self._active_host_config()
            node_name = str(getattr(node, "name", None) or self._active_host_id() or "selected node")
            self.show_notice(
                f"{node_name} has no selected Palworld server. Discover, link, or install a server on this node first.",
                "warning", 7000,
            )
            name = "hosts"
        # Page switching itself is intentionally synchronous and contains no
        # network I/O. The page becomes visible immediately, then fresh data
        # is requested in a worker thread.
        self.stack.setCurrentIndex(self.pages[name])
        for page, button in self.nav_buttons.items():
            button.setChecked(page == name)
        QTimer.singleShot(0, lambda n=name: self._schedule_page_refresh(n))

    def _schedule_page_refresh(self, name):
        if name == "dashboard": self.refresh_dashboard(silent=True)
        elif name == "instances": self.refresh_instances(silent=True)
        elif name == "hosts": self.refresh_hosts_page()
        elif name == "players": self.refresh_players_page(silent=True)
        elif name == "worlds": self.refresh_worlds_page(silent=True)
        elif name == "settings": self.refresh_settings_page(silent=True)
        elif name == "mods": self.refresh_mods_page(silent=True)
        elif name == "backups": self.refresh_backups_page(silent=True)
        elif name == "automation": self.refresh_automation_page(silent=True)
        elif name == "logs": self.refresh_logs(False, silent=True)
        elif name == "health": self.refresh_health_page(silent=True)
        elif name == "diagnostics": self.refresh_diagnostics_page(silent=True)
        elif name == "connection": self.refresh_connection_page()
        elif name == "watchdog": self.refresh_watchdog_page(silent=True)
        elif name == "server_setup": self.refresh_server_setup_page(silent=True)

    def current_page_name(self):
        current = self.stack.currentIndex()
        for name, index in self.pages.items():
            if index == current:
                return name
        return "dashboard"

    def refresh_current_page(self):
        page = self.current_page_name()
        if page == "dashboard": self.refresh_dashboard()
        elif page == "instances": self.refresh_instances()
        elif page == "hosts": self.refresh_hosts_page()
        elif page == "players": self.refresh_players_page()
        elif page == "worlds": self.refresh_worlds_page()
        elif page == "settings": self.refresh_settings_page()
        elif page == "mods": self.refresh_mods_page()
        elif page == "backups": self.refresh_backups_page()
        elif page == "automation": self.refresh_automation_page()
        elif page == "logs": self.refresh_logs(False)
        elif page == "health": self.refresh_health_page()
        elif page == "diagnostics": self.refresh_diagnostics_page()
        elif page == "connection": self.refresh_connection_page()
        elif page == "watchdog": self.refresh_watchdog_page()
        elif page == "server_setup": self.refresh_server_setup_page()
        else: self.refresh_dashboard()

    def _periodic_refresh(self):
        page = self.current_page_name()
        # Only the Dashboard needs the full overview payload. Other pages get
        # a lightweight header-status refresh so navigation remains quiet and
        # the agent is not repeatedly asked for logs/backups/settings.
        if page == "dashboard":
            self.refresh_dashboard(silent=True)
        elif page != "watchdog":
            self.refresh_header_status(silent=True)

    def _watchdog_tick(self):
        if self.current_page_name() == "watchdog":
            self.refresh_watchdog_page(silent=True)

    def _relayout_dashboard_metrics(self):
        if not hasattr(self, "dashboard_metrics_grid") or not hasattr(self, "dashboard_metric_order"):
            return
        available = self.stack.width() if hasattr(self, "stack") else self.width()
        if available >= 1100:
            columns = 6
        elif available >= 720:
            columns = 3
        else:
            columns = 2
        grid = self.dashboard_metrics_grid
        for key in self.dashboard_metric_order:
            grid.removeWidget(self.cards[key])
        for i, key in enumerate(self.dashboard_metric_order):
            grid.addWidget(self.cards[key], i // columns, i % columns)
        for col in range(6):
            grid.setColumnStretch(col, 1 if col < columns else 0)

    def show_notice(self, text, tone="info", timeout=6000):
        self.notice_bar.setText(str(text))
        self.notice_bar.setProperty("tone", tone)
        self.notice_bar.style().unpolish(self.notice_bar)
        self.notice_bar.style().polish(self.notice_bar)
        self.notice_bar.show()
        if timeout:
            QTimer.singleShot(timeout, lambda: self.notice_bar.hide() if self.notice_bar.text() == str(text) else None)

    def call(self, title, fn, silent=False):
        # Retained for a few local/legacy paths. Never force a global wait
        # cursor: the UI should not look frozen during an operation.
        try:
            return fn()
        except Exception as exc:
            if not silent:
                self.show_notice(f"{title}: {exc}", "error", 10000)
            return None

    def _run_async(self, key, title, fn, on_success=None, on_error=None, silent=False):
        if key in self._async_busy:
            return False
        self._async_busy.add(key)
        task = AsyncTask(fn)
        self._async_tasks[key] = task

        if on_success is not None:
            def handle_result(result):
                try:
                    on_success(result)
                except Exception as exc:
                    # Exceptions raised by a Qt result callback otherwise do
                    # not pass through AsyncTask.run(), which made pages look
                    # permanently stuck on CHECKING/blank content.
                    self.show_notice(f"{title}: UI update failed: {exc}", "error", 12000)
            task.signals.result.connect(handle_result)

        def handle_error(message):
            if on_error is not None:
                on_error(message)
            elif not silent:
                self.show_notice(f"{title}: {message}", "error", 10000)

        def finished():
            self._async_busy.discard(key)
            self._async_tasks.pop(key, None)

        task.signals.error.connect(handle_error)
        task.signals.finished.connect(finished)
        self.thread_pool.start(task)
        return True

    def _run_action_async(self, key, title, fn, success_message=None, on_success=None):
        self.show_notice(f"{title} in progress…", "info", 0)

        def completed(result):
            if success_message:
                text = success_message(result) if callable(success_message) else success_message
                self.show_notice(text, "success")
            else:
                self.notice_bar.hide()
            if on_success:
                on_success(result)

        return self._run_async(key, title, fn, completed, silent=False)

    def _confirm_then(self, key, prompt, fn):
        now = time.time()
        if self.pending_confirmation == key and now <= self.pending_confirmation_until:
            self.pending_confirmation = None
            self.pending_confirmation_until = 0
            return fn()
        self.pending_confirmation = key
        self.pending_confirmation_until = now + 6
        self.show_notice(f"Confirmation required: {prompt} Click the same action again within 6 seconds.", "warning", 6500)
        return None

    def _page_header(self, title, subtitle, symbol):
        panel = QFrame()
        panel.setObjectName("panel")
        layout = QHBoxLayout(panel)
        layout.setContentsMargins(16, 12, 16, 12)
        icon = QLabel(symbol)
        icon.setObjectName("iconGlyph")
        icon.setStyleSheet("color:#5fb9ff; font-size:38px;")
        icon.setFixedWidth(50)
        layout.addWidget(icon)
        copy = QVBoxLayout()
        copy.setSpacing(1)
        t = QLabel(title); t.setObjectName("pageTitle")
        s = QLabel(subtitle); s.setObjectName("pageSubtitle"); s.setWordWrap(True)
        copy.addWidget(t); copy.addWidget(s)
        layout.addLayout(copy, 1)
        return panel

    def _summary_column(self, title: str):
        layout = QVBoxLayout(); layout.setSpacing(1)
        label = QLabel(title); label.setObjectName("summaryTitle")
        value = QLabel("-"); value.setObjectName("summaryValue")
        detail = QLabel(""); detail.setObjectName("summaryDetail"); detail.setWordWrap(True)
        layout.addWidget(label); layout.addWidget(value); layout.addWidget(detail)
        return layout, value, detail

    def _build_dashboard_page(self):
        scroll = QScrollArea(); scroll.setWidgetResizable(True); scroll.setFrameShape(QFrame.NoFrame)
        page = QWidget(); page.setObjectName("dashboardPage")
        layout = QVBoxLayout(page); layout.setContentsMargins(0, 0, 4, 4); layout.setSpacing(12)
        overview = QFrame(); overview.setObjectName("serverOverview"); overview.setFixedHeight(110)
        ov = QHBoxLayout(overview); ov.setContentsMargins(18, 13, 18, 13); ov.setSpacing(15)
        icon_tile = QFrame(); icon_tile.setObjectName("iconTile"); icon_tile.setFixedSize(72, 72)
        icon_layout = QVBoxLayout(icon_tile); icon_layout.setContentsMargins(0, 0, 0, 0)
        icon = QLabel("\u25a6"); icon.setObjectName("iconGlyph"); icon.setStyleSheet("color:#86a9ff; font-size:36px;"); icon.setAlignment(Qt.AlignCenter)
        icon_layout.addWidget(icon); ov.addWidget(icon_tile)
        server_box = QVBoxLayout(); server_box.setSpacing(1)
        self.server_name = QLabel("Loading server..."); self.server_name.setObjectName("serverName")
        self.server_subtitle = QLabel("Palworld Dedicated Server"); self.server_subtitle.setObjectName("serverSubtitle")
        badges = QHBoxLayout(); badges.setSpacing(8)
        self.service_badge = PillBadge("ACTIVE", "good"); self.health_badge = PillBadge("HEALTHY", "good")
        badges.addWidget(self.service_badge); badges.addWidget(self.health_badge); badges.addStretch(1)
        server_box.addWidget(self.server_name); server_box.addWidget(self.server_subtitle); server_box.addLayout(badges)
        ov.addLayout(server_box, 2)
        separator = QFrame(); separator.setFrameShape(QFrame.VLine); separator.setStyleSheet("color:#29456e;"); ov.addWidget(separator)
        host_col, self.host_value, self.host_detail = self._summary_column("Host")
        port_col, self.port_value, self.port_detail = self._summary_column("Game Port")
        uptime_col, self.uptime_value, self.uptime_detail = self._summary_column("Uptime")
        ov.addLayout(host_col, 1); ov.addLayout(port_col, 1); ov.addLayout(uptime_col, 1)
        layout.addWidget(overview)
        metrics = QGridLayout(); metrics.setHorizontalSpacing(10); metrics.setVerticalSpacing(10)
        self.dashboard_metrics_grid = metrics
        self.cards = {
            "Players": MetricCard("Players", "\u265f", "#37caff"),
            "FPS": MetricCard("Server FPS", "\u25f4", "#2b9dff"),
            "World": MetricCard("World Day", "\u2600", "#ffc247"),
            "Version": MetricCard("Game Version", "P", "#66b3ff"),
            "CPU_RAM": MetricCard("CPU / RAM", "\u25a6", "#44ed8e"),
            "Storage": MetricCard("Storage", "\u25a3", "#32d9c5"),
        }
        self.dashboard_metric_order = ("Players", "FPS", "World", "Version", "CPU_RAM", "Storage")
        for col, key in enumerate(self.dashboard_metric_order):
            metrics.addWidget(self.cards[key], 0, col); metrics.setColumnStretch(col, 1)
        layout.addLayout(metrics)
        actions = QFrame(); actions.setObjectName("panel"); actions.setFixedHeight(68)
        action_layout = QHBoxLayout(actions); action_layout.setContentsMargins(14, 10, 14, 10); action_layout.setSpacing(10)
        self.action_buttons = {}
        for key, text, obj, symbol, handler in (
            ("start", "Start", "successButton", self.ICONS["play"], lambda: self.service("start")),
            ("stop", "Stop", "dangerButton", self.ICONS["stop"], lambda: self.service("stop")),
            ("restart", "Restart", "warningButton", self.ICONS["restart"], lambda: self.service("restart")),
            ("save", "Save World", "primaryButton", self.ICONS["save"], self.save_world),
            ("backup", "Backup Now", "purpleButton", self.ICONS["archive"], self.backup_now),
            ("update", "Check Update", "primaryButton", self.ICONS["update"], self.update_check),
        ):
            button = QPushButton(text); button.setObjectName(obj); self._set_button_icon(button, symbol, size=22); button.clicked.connect(handler)
            action_layout.addWidget(button, 1); self.action_buttons[key] = button
        self._set_service_action_state(None, loading=True)
        layout.addWidget(actions)
        lower = QGridLayout(); lower.setHorizontalSpacing(12); lower.setVerticalSpacing(12)
        log_panel = QFrame(); log_panel.setObjectName("panel")
        log_layout = QVBoxLayout(log_panel); log_layout.setContentsMargins(14, 12, 14, 12)
        header = QHBoxLayout(); title = QLabel("Recent Server Log"); title.setObjectName("panelTitle"); header.addWidget(title); header.addStretch(1)
        self.auto_scroll_checkbox = QCheckBox("Auto Scroll"); self.auto_scroll_checkbox.setChecked(True); self.auto_scroll_checkbox.toggled.connect(lambda state: setattr(self, "auto_scroll_logs", bool(state))); header.addWidget(self.auto_scroll_checkbox)
        warn = QPushButton("Warnings / Errors"); warn.setObjectName("smallGhostButton"); warn.clicked.connect(lambda: self.refresh_logs(True)); header.addWidget(warn)
        full = QPushButton("Open Full Log"); full.setObjectName("smallGhostButton"); self._set_button_icon(full, self.ICONS["logs"], size=18); full.clicked.connect(lambda: self.show_named_page("logs")); header.addWidget(full)
        log_layout.addLayout(header)
        self.dashboard_log_view = QPlainTextEdit(); self.dashboard_log_view.setObjectName("dashboardLog"); self.dashboard_log_view.setReadOnly(True); self.dashboard_log_view.setLineWrapMode(QPlainTextEdit.NoWrap); self.dashboard_log_view.setFixedHeight(150); log_layout.addWidget(self.dashboard_log_view)
        lower.addWidget(log_panel, 0, 0, 1, 2)
        health_panel = QFrame(); health_panel.setObjectName("panel")
        health_layout = QVBoxLayout(health_panel); health_layout.setContentsMargins(14, 12, 14, 12)
        health_title = QLabel("Server Health"); health_title.setObjectName("panelTitle"); health_layout.addWidget(health_title)
        self.health_summary = QLabel("Healthy"); self.health_summary.setObjectName("healthBig"); self.health_summary.setAlignment(Qt.AlignCenter); health_layout.addWidget(self.health_summary)
        self.health_caption = QLabel("All systems operating normally"); self.health_caption.setObjectName("panelMuted"); self.health_caption.setAlignment(Qt.AlignCenter); health_layout.addWidget(self.health_caption)
        self.health_rows = {}
        for key in ("CPU Usage", "Memory Usage", "Disk Usage", "Palworld Service", "Game Port (8211)", "REST API (8212)", "Backups"):
            row = QHBoxLayout(); dot = QLabel("\u25cf"); dot.setObjectName("healthDotGood"); label = QLabel(key); label.setObjectName("rowLabel"); value = QLabel("-"); value.setObjectName("rowValue")
            row.addWidget(dot); row.addWidget(label); row.addStretch(1); row.addWidget(value); health_layout.addLayout(row); self.health_rows[key] = (dot, value)
        health_layout.addStretch(1); lower.addWidget(health_panel, 0, 2)
        players_panel = QFrame(); players_panel.setObjectName("panel")
        players_layout = QVBoxLayout(players_panel); players_layout.setContentsMargins(14, 12, 14, 12)
        player_header = QHBoxLayout(); self.players_panel_title = QLabel("Connected Players (0)"); self.players_panel_title.setObjectName("panelTitle"); player_header.addWidget(self.players_panel_title); player_header.addStretch(1)
        details = QPushButton("View Details"); details.setObjectName("smallGhostButton"); self._set_button_icon(details, self.ICONS["players"], size=19); details.clicked.connect(lambda: self.show_named_page("players")); player_header.addWidget(details); players_layout.addLayout(player_header)
        self.players_table = self._make_players_table(145); players_layout.addWidget(self.players_table); lower.addWidget(players_panel, 1, 0, 1, 2)
        auto_panel = QFrame(); auto_panel.setObjectName("panel")
        auto_layout = QVBoxLayout(auto_panel); auto_layout.setContentsMargins(14, 12, 14, 12)
        auto_title = QLabel("Next Automation"); auto_title.setObjectName("panelTitle"); auto_layout.addWidget(auto_title)
        self.automation_rows = {}
        for key in ("Backup", "Update Check", "Maintenance Window"):
            row = QHBoxLayout(); label = QLabel(key); label.setObjectName("rowLabel"); value = QLabel("-"); value.setObjectName("rowValue"); row.addWidget(label); row.addStretch(1); row.addWidget(value); auto_layout.addLayout(row); self.automation_rows[key] = value
        auto_layout.addStretch(1); lower.addWidget(auto_panel, 1, 2)
        lower.setColumnStretch(0, 2); lower.setColumnStretch(1, 2); lower.setColumnStretch(2, 1)
        layout.addLayout(lower); layout.addStretch(1); scroll.setWidget(page); self._register_page("dashboard", scroll)

    def _build_instances_page(self):
        page = QWidget(); page.setObjectName("instancesPage")
        layout = QVBoxLayout(page); layout.setContentsMargins(0, 0, 0, 0); layout.setSpacing(12)
        layout.addWidget(self._page_header(
            "Managed Servers",
            "Every server gets a permanent manager ID starting at 001. Servers may live on the same agent host or on completely separate Linux hosts.",
            self.ICONS["instances"],
        ))

        toolbar = QFrame(); toolbar.setObjectName("panel")
        tl = QHBoxLayout(toolbar); tl.setContentsMargins(12, 9, 12, 9); tl.setSpacing(8)
        refresh = QPushButton("Refresh"); refresh.setObjectName("ghostButton"); refresh.setProperty("compactAction", True); refresh.setMaximumWidth(130); self._set_button_icon(refresh, self.ICONS["refresh"], size=19); refresh.clicked.connect(lambda: self.refresh_instances())
        use = QPushButton("Manage Selected"); use.setObjectName("primaryButton"); use.setProperty("compactAction", True); use.setMaximumWidth(180); self._set_button_icon(use, self.ICONS["play"], size=19); use.clicked.connect(self.manage_selected_instance)
        setup = QPushButton("Server Setup"); setup.setObjectName("ghostButton"); setup.setProperty("compactAction", True); setup.setMaximumWidth(155); self._set_button_icon(setup, self.ICONS["settings"], size=19); setup.clicked.connect(self.setup_selected_instance)
        delete = QPushButton("Remove from Manager"); delete.setObjectName("ghostButton"); delete.setProperty("compactAction", True); delete.setMaximumWidth(190); self._set_button_icon(delete, self.ICONS["delete"], size=18); delete.clicked.connect(self.delete_selected_instance)
        uninstall = QPushButton("Uninstall Server"); uninstall.setObjectName("dangerButton"); uninstall.setProperty("compactAction", True); uninstall.setMaximumWidth(180); self._set_button_icon(uninstall, self.ICONS["delete"], size=18); uninstall.clicked.connect(self.uninstall_selected_instance)
        tl.addWidget(refresh); tl.addWidget(use); tl.addWidget(setup); tl.addStretch(1); tl.addWidget(delete); tl.addWidget(uninstall)
        layout.addWidget(toolbar)

        self.instances_table = QTableWidget(0, 8)
        self.instances_table.setAlternatingRowColors(True)
        self.instances_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.instances_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.instances_table.setSelectionMode(QTableWidget.SingleSelection)
        self.instances_table.verticalHeader().setVisible(False)
        self.instances_table.setHorizontalHeaderLabels(["Server Name", "Instance ID", "Host", "State", "Service", "Game Port", "REST Port", "Install Directory"])
        ih = self.instances_table.horizontalHeader()
        ih.setSectionResizeMode(0, QHeaderView.Stretch)
        ih.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        ih.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        ih.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        ih.setSectionResizeMode(4, QHeaderView.ResizeToContents)
        ih.setSectionResizeMode(5, QHeaderView.ResizeToContents)
        ih.setSectionResizeMode(6, QHeaderView.ResizeToContents)
        ih.setSectionResizeMode(7, QHeaderView.Stretch)
        self.instances_table.doubleClicked.connect(lambda _index: self.manage_selected_instance())
        layout.addWidget(self.instances_table, 1)

        rename_panel = QFrame(); rename_panel.setObjectName("panel")
        rl = QHBoxLayout(rename_panel); rl.setContentsMargins(14, 10, 14, 10); rl.setSpacing(10)
        rl.addWidget(QLabel("Rename selected server:"))
        self.instance_rename_input = QLineEdit(); self.instance_rename_input.setPlaceholderText("New display/server name"); self.instance_rename_input.setMaximumWidth(420)
        rename = QPushButton("Rename"); rename.setObjectName("primaryButton"); rename.setProperty("compactAction", True); rename.setMaximumWidth(120); self._set_button_icon(rename, self.ICONS["edit"], size=18); rename.clicked.connect(self.rename_selected_instance)
        rl.addWidget(self.instance_rename_input); rl.addWidget(rename); rl.addStretch(1)
        layout.addWidget(rename_panel)

        create_panel = QFrame(); create_panel.setObjectName("panel")
        form = QGridLayout(create_panel); form.setContentsMargins(16, 12, 16, 12); form.setHorizontalSpacing(12); form.setVerticalSpacing(8)
        title = QLabel("Add Another Server on the Current Host"); title.setObjectName("panelTitle")
        subtitle = QLabel("Instance ID is assigned automatically (001, 002, 003...). To add a completely different machine, use Remote Hosts in the sidebar."); subtitle.setObjectName("pageSubtitle"); subtitle.setWordWrap(True)
        form.addWidget(title, 0, 0, 1, 4); form.addWidget(subtitle, 1, 0, 1, 4)
        self.instance_name_input = QLineEdit(); self.instance_name_input.setPlaceholderText("Example: Community Server")
        self.instance_next_id_label = QLabel("Auto"); self.instance_next_id_label.setObjectName("rowValue")
        self.instance_install_input = QLineEdit(); self.instance_install_input.setPlaceholderText("Optional; auto-derived when blank")
        self.instance_service_input = QLineEdit(); self.instance_service_input.setPlaceholderText("Optional; auto-derived when blank")
        self.instance_game_port_input = QSpinBox(); self.instance_game_port_input.setRange(0, 65535); self.instance_game_port_input.setSpecialValueText("Auto")
        self.instance_rest_port_input = QSpinBox(); self.instance_rest_port_input.setRange(0, 65535); self.instance_rest_port_input.setSpecialValueText("Auto")
        form.addWidget(QLabel("Server name"), 2, 0); form.addWidget(self.instance_name_input, 2, 1)
        form.addWidget(QLabel("Instance ID"), 2, 2); form.addWidget(self.instance_next_id_label, 2, 3)
        form.addWidget(QLabel("Install directory"), 3, 0); form.addWidget(self.instance_install_input, 3, 1)
        form.addWidget(QLabel("Service name"), 3, 2); form.addWidget(self.instance_service_input, 3, 3)
        form.addWidget(QLabel("Game port"), 4, 0); form.addWidget(self.instance_game_port_input, 4, 1)
        form.addWidget(QLabel("REST API port"), 4, 2); form.addWidget(self.instance_rest_port_input, 4, 3)
        add = QPushButton("Add Server"); add.setObjectName("successButton"); add.setProperty("compactAction", True); add.setMaximumWidth(160); self._set_button_icon(add, self.ICONS["play"], size=19); add.clicked.connect(self.create_instance_from_form)
        form.addWidget(add, 5, 3, Qt.AlignRight)
        form.setColumnStretch(1, 1); form.setColumnStretch(3, 1)
        layout.addWidget(create_panel)
        self._register_page("instances", page)

    def _build_hosts_page(self):
        page = QWidget(); page.setObjectName("hostsPage")
        layout = QVBoxLayout(page); layout.setContentsMargins(0, 0, 0, 0); layout.setSpacing(12)
        layout.addWidget(self._page_header(
            "Remote Linux Hosts",
            "Connect a Linux machine over SSH, install or upgrade the PalServer Manager agent, discover existing Palworld servers, and link them to this manager.",
            self.ICONS["hosts"],
        ))

        self.hosts_table = QTableWidget(0, 7)
        self.hosts_table.setAlternatingRowColors(True); self.hosts_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.hosts_table.setSelectionBehavior(QTableWidget.SelectRows); self.hosts_table.setSelectionMode(QTableWidget.SingleSelection); self.hosts_table.verticalHeader().setVisible(False)
        self.hosts_table.setHorizontalHeaderLabels(["Host Name", "Host ID", "Address", "SSH User", "SSH Port", "Agent Port", "OS"])
        hh = self.hosts_table.horizontalHeader(); hh.setSectionResizeMode(0, QHeaderView.Stretch); hh.setSectionResizeMode(1, QHeaderView.ResizeToContents); hh.setSectionResizeMode(2, QHeaderView.Stretch); hh.setSectionResizeMode(3, QHeaderView.ResizeToContents); hh.setSectionResizeMode(4, QHeaderView.ResizeToContents); hh.setSectionResizeMode(5, QHeaderView.ResizeToContents); hh.setSectionResizeMode(6, QHeaderView.ResizeToContents)
        self.hosts_table.setMaximumHeight(180)
        self.hosts_table.itemSelectionChanged.connect(self._on_host_selection_changed)
        layout.addWidget(self.hosts_table)

        controls_panel = QFrame(); controls_panel.setObjectName("panel")
        controls = QGridLayout(controls_panel); controls.setContentsMargins(14, 11, 14, 11); controls.setHorizontalSpacing(8); controls.setVerticalSpacing(8)
        controls_title = QLabel("Selected Host Controls"); controls_title.setObjectName("panelTitle")
        self.selected_host_label = QLabel("Select a provisioned host above to manage its agent or install Palworld servers."); self.selected_host_label.setObjectName("pageSubtitle"); self.selected_host_label.setWordWrap(True)
        controls.addWidget(controls_title, 0, 0, 1, 3); controls.addWidget(self.selected_host_label, 1, 0, 1, 3)

        self.host_verify_button = QPushButton("Verify Agent"); self.host_verify_button.setObjectName("ghostButton"); self.host_verify_button.setProperty("compactAction", True); self.host_verify_button.setMaximumWidth(150); self.host_verify_button.clicked.connect(self.verify_selected_host_agent)
        self.host_discover_button = QPushButton("Discover & Link Servers"); self.host_discover_button.setObjectName("primaryButton"); self.host_discover_button.setProperty("compactAction", True); self.host_discover_button.setMaximumWidth(210); self.host_discover_button.clicked.connect(self.discover_selected_host_servers)
        self.host_install_server_button = QPushButton("Install New Palworld Server"); self.host_install_server_button.setObjectName("successButton"); self.host_install_server_button.setProperty("compactAction", True); self.host_install_server_button.setMaximumWidth(230); self.host_install_server_button.clicked.connect(self.prepare_install_for_selected_host)
        self.host_update_agent_button = QPushButton("Update Agent"); self.host_update_agent_button.setObjectName("ghostButton"); self.host_update_agent_button.setProperty("compactAction", True); self.host_update_agent_button.setMaximumWidth(150); self.host_update_agent_button.clicked.connect(self.update_selected_host_agent)
        self.host_update_linux_button = QPushButton("Update Linux"); self.host_update_linux_button.setObjectName("ghostButton"); self.host_update_linux_button.setProperty("compactAction", True); self.host_update_linux_button.setMaximumWidth(150); self.host_update_linux_button.clicked.connect(self.update_selected_host_linux)
        self.host_restart_agent_button = QPushButton("Restart Agent"); self.host_restart_agent_button.setObjectName("warningButton"); self.host_restart_agent_button.setProperty("compactAction", True); self.host_restart_agent_button.setMaximumWidth(150); self.host_restart_agent_button.clicked.connect(self.restart_selected_host_agent)
        self.host_uninstall_agent_button = QPushButton("Uninstall Agent"); self.host_uninstall_agent_button.setObjectName("dangerButton"); self.host_uninstall_agent_button.setProperty("compactAction", True); self.host_uninstall_agent_button.setMaximumWidth(155); self.host_uninstall_agent_button.clicked.connect(self.uninstall_selected_host_agent)
        host_actions = QHBoxLayout()
        host_actions.setContentsMargins(0, 0, 0, 0)
        host_actions.setSpacing(8)
        for button in (
            self.host_verify_button,
            self.host_discover_button,
            self.host_install_server_button,
            self.host_update_agent_button,
            self.host_update_linux_button,
            self.host_restart_agent_button,
            self.host_uninstall_agent_button,
        ):
            host_actions.addWidget(button, 0, Qt.AlignLeft)
        host_actions.addStretch(1)
        controls.addLayout(host_actions, 2, 0, 1, 3)
        controls.setColumnStretch(0, 0); controls.setColumnStretch(1, 0); controls.setColumnStretch(2, 1)
        self.host_control_buttons = [
            self.host_verify_button, self.host_discover_button, self.host_install_server_button,
            self.host_update_agent_button, self.host_update_linux_button, self.host_restart_agent_button, self.host_uninstall_agent_button,
        ]
        for button in self.host_control_buttons:
            button.setEnabled(False)
        layout.addWidget(controls_panel)

        connect_panel = QFrame(); connect_panel.setObjectName("panel")
        grid = QGridLayout(connect_panel); grid.setContentsMargins(16, 12, 16, 12); grid.setHorizontalSpacing(12); grid.setVerticalSpacing(8)
        title = QLabel("Add / Provision Linux Host"); title.setObjectName("panelTitle")
        note = QLabel("Automatic provisioning currently requires SSH public-key authentication and root or passwordless sudo. SSH passwords are never stored. The agent remains bound to 127.0.0.1 and is reached through an SSH tunnel."); note.setObjectName("pageSubtitle"); note.setWordWrap(True)
        grid.addWidget(title, 0, 0, 1, 4); grid.addWidget(note, 1, 0, 1, 4)
        self.host_name_input = QLineEdit(); self.host_name_input.setPlaceholderText("Example: Dallas Game Host")
        self.host_address_input = QLineEdit(); self.host_address_input.setPlaceholderText("IP address or DNS hostname")
        self.host_user_input = QLineEdit(); self.host_user_input.setPlaceholderText("root or sudo-capable user")
        self.host_port_input = QSpinBox(); self.host_port_input.setRange(1, 65535); self.host_port_input.setValue(22)
        self.host_key_input = QLineEdit(); self.host_key_input.setPlaceholderText("SSH private key path")
        self.host_update_os = QCheckBox("Update Debian/Ubuntu packages before installing the agent"); self.host_update_os.setChecked(True)
        grid.addWidget(QLabel("Host name"), 2, 0); grid.addWidget(self.host_name_input, 2, 1)
        grid.addWidget(QLabel("SSH host"), 2, 2); grid.addWidget(self.host_address_input, 2, 3)
        grid.addWidget(QLabel("SSH user"), 3, 0); grid.addWidget(self.host_user_input, 3, 1)
        grid.addWidget(QLabel("SSH port"), 3, 2); grid.addWidget(self.host_port_input, 3, 3)
        grid.addWidget(QLabel("Private key"), 4, 0); grid.addWidget(self.host_key_input, 4, 1, 1, 3)
        grid.addWidget(self.host_update_os, 5, 0, 1, 4)
        buttons = QHBoxLayout()
        test = QPushButton("Test SSH"); test.setObjectName("ghostButton"); test.setProperty("compactAction", True); test.setMaximumWidth(130); test.clicked.connect(self.test_new_host_ssh)
        provision = QPushButton("Connect & Install Agent"); provision.setObjectName("successButton"); provision.setProperty("compactAction", True); provision.setMaximumWidth(210); provision.clicked.connect(self.provision_new_host)
        buttons.addWidget(test); buttons.addWidget(provision); buttons.addStretch(1)
        grid.addLayout(buttons, 6, 0, 1, 4)
        grid.setColumnStretch(1, 1); grid.setColumnStretch(3, 1)
        layout.addWidget(connect_panel)

        self.host_provision_status = QLabel("Ready to connect a Linux host."); self.host_provision_status.setObjectName("noticeBar"); self.host_provision_status.setProperty("tone", "info"); self.host_provision_status.setWordWrap(True)
        layout.addWidget(self.host_provision_status)

        console_panel = QFrame(); console_panel.setObjectName("panel")
        self.host_provision_console_panel = console_panel
        console_layout = QVBoxLayout(console_panel); console_layout.setContentsMargins(14, 12, 14, 12); console_layout.setSpacing(8)
        console_top = QHBoxLayout()
        console_title = QLabel("Agent Provisioning Console"); console_title.setObjectName("panelTitle")
        console_hint = QLabel("Live SSH, package update, Python, agent install, and systemd output"); console_hint.setObjectName("muted")
        clear_console = QPushButton("Clear"); clear_console.setObjectName("ghostButton"); clear_console.setProperty("compactAction", True); clear_console.setMaximumWidth(90)
        console_top.addWidget(console_title); console_top.addWidget(console_hint); console_top.addStretch(1); console_top.addWidget(clear_console)
        console_layout.addLayout(console_top)
        self.host_provision_console = QPlainTextEdit(); self.host_provision_console.setObjectName("provisionConsole"); self.host_provision_console.setReadOnly(True); self.host_provision_console.setLineWrapMode(QPlainTextEdit.NoWrap); self.host_provision_console.setMinimumHeight(180)
        self.host_provision_console.document().setMaximumBlockCount(5000)
        self.host_provision_console.setPlainText("Provisioning console ready. Start with Test SSH or Connect & Install Agent.")
        clear_console.clicked.connect(self.host_provision_console.clear)
        console_layout.addWidget(self.host_provision_console, 1)
        layout.addWidget(console_panel, 1)
        self.host_provision_log_signals = ProvisioningLogSignals(self)
        self.host_provision_log_signals.line.connect(self._append_host_provision_log)

        # The Palworld installer is intentionally modal instead of being embedded at
        # the bottom of the Remote Hosts page. This keeps the host-management page
        # compact while still allowing installation on any previously provisioned host.
        self.host_install_dialog = None
        self.host_install_title = None
        self.host_install_subtitle = None
        self.host_install_status = None
        self.host_install_submit_button = None
        self.new_remote_server_name = None
        self.new_remote_install_dir = None
        self.new_remote_service = None
        self.new_remote_game_port = None
        self.new_remote_rest_port = None
        self.new_remote_max_players = None
        layout.addStretch(1)
        scroll = QScrollArea(); scroll.setObjectName("hostsScroll"); scroll.setWidgetResizable(True); scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff); scroll.setFrameShape(QFrame.NoFrame)
        self.hosts_scroll = scroll
        scroll.setWidget(page)
        self._register_page("hosts", scroll)

    def _build_tools_page(self):
        page = QWidget(); page.setObjectName("toolsPage")
        layout = QVBoxLayout(page); layout.setContentsMargins(0, 0, 0, 0); layout.setSpacing(12)
        layout.addWidget(self._page_header(
            "Server Management Tools",
            "Safe navigation only. Direct server actions are intentionally excluded from this page.",
            self.ICONS["tools"],
        ))
        self.tools_scroll = QScrollArea()
        self.tools_scroll.setWidgetResizable(True)
        self.tools_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.tools_scroll.setFrameShape(QFrame.NoFrame)
        self.tools_body = QWidget(); self.tools_body.setObjectName("toolsBody")
        self.tools_body.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        body_layout = QVBoxLayout(self.tools_body); body_layout.setContentsMargins(0, 0, 2, 0)
        self.tools_grid = QGridLayout(); self.tools_grid.setHorizontalSpacing(9); self.tools_grid.setVerticalSpacing(9)
        body_layout.addLayout(self.tools_grid, 1)
        self.tools_scroll.setWidget(self.tools_body); layout.addWidget(self.tools_scroll, 1)
        self.page_label = QLabel(""); self.page_label.hide()
        self.prev_button = QPushButton(); self.prev_button.hide()
        self.next_button = QPushButton(); self.next_button.hide()
        self._register_page("tools", page)
        self.render_tools()

    def _make_players_table(self, fixed_height=None):
        table = QTableWidget(0, 8); table.setAlternatingRowColors(True); table.setEditTriggers(QTableWidget.NoEditTriggers); table.setSelectionBehavior(QTableWidget.SelectRows); table.verticalHeader().setVisible(False)
        table.setHorizontalHeaderLabels(["Player Name", "Platform", "Level", "Ping", "Account", "User ID", "IP Address", "Location"])
        header = table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(4, QHeaderView.Stretch)
        header.setSectionResizeMode(5, QHeaderView.Stretch)
        header.setSectionResizeMode(6, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(7, QHeaderView.ResizeToContents)
        if fixed_height: table.setFixedHeight(fixed_height)
        return table

    def _build_players_page(self):
        page = QWidget(); page.setObjectName("playersPage")
        layout = QVBoxLayout(page); layout.setContentsMargins(0,0,0,0); layout.setSpacing(12)
        layout.addWidget(self._page_header(
            "Players & Ban Manager",
            "Connected players are labeled by platform. Bans created through PalServer Manager are tracked here for one-click unban.",
            self.ICONS["players"],
        ))

        connected = QFrame(); connected.setObjectName("panel")
        pl = QVBoxLayout(connected); pl.setContentsMargins(14,12,14,12); pl.setSpacing(10)
        toolbar = QHBoxLayout()
        title = QLabel("Connected Players"); title.setObjectName("panelTitle"); toolbar.addWidget(title)
        toolbar.addStretch(1)
        refresh = QPushButton("Refresh Players"); refresh.setObjectName("ghostButton"); self._set_button_icon(refresh, self.ICONS["refresh"], size=20); refresh.clicked.connect(lambda: self.refresh_players_page(False)); toolbar.addWidget(refresh)
        pl.addLayout(toolbar)
        self.players_page_table = self._make_players_table(); pl.addWidget(self.players_page_table, 1)

        admin = QFrame(); admin.setObjectName("detailCard")
        al = QGridLayout(admin); al.setContentsMargins(14,12,14,12); al.setHorizontalSpacing(10); al.setVerticalSpacing(10)
        al.addWidget(QLabel("Selected-player message:"), 0, 0)
        self.player_message = QLineEdit("Administrative action by server operator"); al.addWidget(self.player_message, 0, 1, 1, 3)
        kick = QPushButton("Kick Selected"); kick.setObjectName("warningButton"); self._set_button_icon(kick, "!", size=19); kick.clicked.connect(lambda: self.player_selected_action("kick")); al.addWidget(kick, 1, 0)
        ban = QPushButton("Ban Selected"); ban.setObjectName("dangerButton"); self._set_button_icon(ban, self.ICONS["delete"], size=19); ban.clicked.connect(lambda: self.player_selected_action("ban")); al.addWidget(ban, 1, 1)
        self.broadcast_text = QLineEdit(); self.broadcast_text.setPlaceholderText("Broadcast message to all connected players"); al.addWidget(self.broadcast_text, 2, 0, 1, 3)
        broadcast = QPushButton("Broadcast"); broadcast.setObjectName("primaryButton"); self._set_button_icon(broadcast, self.ICONS["broadcast"], size=19); broadcast.clicked.connect(self.broadcast_message); al.addWidget(broadcast, 2, 3)
        al.setColumnStretch(2, 1)
        pl.addWidget(admin)
        layout.addWidget(connected, 3)

        bans = QFrame(); bans.setObjectName("panel")
        bl = QVBoxLayout(bans); bl.setContentsMargins(14,12,14,12); bl.setSpacing(9)
        bh = QHBoxLayout()
        bt = QLabel("Ban Manager"); bt.setObjectName("panelTitle"); bh.addWidget(bt)
        ban_note = QLabel("Tracks successful bans made through PalServer Manager. Palworld's REST API does not expose a list-bans endpoint."); ban_note.setObjectName("panelMuted"); ban_note.setWordWrap(True); bh.addWidget(ban_note, 1)
        refresh_bans = QPushButton("Refresh Bans"); refresh_bans.setObjectName("ghostButton"); refresh_bans.clicked.connect(lambda: self.refresh_bans_page(False)); bh.addWidget(refresh_bans)
        bl.addLayout(bh)
        self.bans_table = QTableWidget(0, 6); self.bans_table.setAlternatingRowColors(True); self.bans_table.setEditTriggers(QTableWidget.NoEditTriggers); self.bans_table.setSelectionBehavior(QTableWidget.SelectRows); self.bans_table.verticalHeader().setVisible(False)
        self.bans_table.setHorizontalHeaderLabels(["Player", "Platform", "Account", "User ID", "Reason", "Banned"])
        bhdr = self.bans_table.horizontalHeader(); bhdr.setSectionResizeMode(0,QHeaderView.Stretch); bhdr.setSectionResizeMode(1,QHeaderView.ResizeToContents); bhdr.setSectionResizeMode(2,QHeaderView.Stretch); bhdr.setSectionResizeMode(3,QHeaderView.Stretch); bhdr.setSectionResizeMode(4,QHeaderView.Stretch); bhdr.setSectionResizeMode(5,QHeaderView.ResizeToContents)
        self.bans_table.setMinimumHeight(135); bl.addWidget(self.bans_table, 1)
        ban_actions = QHBoxLayout()
        self.unban_id = QLineEdit(); self.unban_id.setPlaceholderText("Platform User ID to unban (Steam, Xbox/GDK, PlayStation, etc.)"); ban_actions.addWidget(self.unban_id, 1)
        unban_id = QPushButton("Unban ID"); unban_id.setObjectName("ghostButton"); unban_id.clicked.connect(self.unban_player); ban_actions.addWidget(unban_id)
        unban_selected = QPushButton("Unban Selected"); unban_selected.setObjectName("successButton"); unban_selected.clicked.connect(self.unban_selected_ban); ban_actions.addWidget(unban_selected)
        bl.addLayout(ban_actions)
        layout.addWidget(bans, 2)
        self._register_page("players", page)

    def _build_worlds_page(self):
        page = QWidget(); page.setObjectName("worldsPage"); layout = QVBoxLayout(page); layout.setContentsMargins(0,0,0,0); layout.setSpacing(12)
        layout.addWidget(self._page_header("World Manager", "Archive, inspect, delete, or create worlds without leaving the main application window.", self.ICONS["worlds"]))
        panel = QFrame(); panel.setObjectName("panel"); pl = QVBoxLayout(panel); pl.setContentsMargins(14,12,14,12)
        self.world_table = QTableWidget(0,4); self.world_table.setHorizontalHeaderLabels(["World GUID", "Size", "Modified", "WorldOption.sav"]); self.world_table.setSelectionBehavior(QTableWidget.SelectRows); self.world_table.setEditTriggers(QTableWidget.NoEditTriggers); self.world_table.verticalHeader().setVisible(False); self.world_table.horizontalHeader().setSectionResizeMode(0,QHeaderView.Stretch); self.world_table.horizontalHeader().setSectionResizeMode(1,QHeaderView.ResizeToContents); self.world_table.horizontalHeader().setSectionResizeMode(2,QHeaderView.ResizeToContents); self.world_table.horizontalHeader().setSectionResizeMode(3,QHeaderView.ResizeToContents); pl.addWidget(self.world_table,1)
        buttons = QHBoxLayout()
        for text,obj,symbol,fn in (("Refresh","ghostButton",self.ICONS["refresh"],self.refresh_worlds_page),("Archive Selected","primaryButton",self.ICONS["archive"],self.archive_selected_world),("Delete Selected","dangerButton",self.ICONS["delete"],self.delete_selected_world),("Create Fresh World","warningButton",self.ICONS["restart"],self.create_fresh_world)):
            b=QPushButton(text); b.setObjectName(obj); self._set_button_icon(b,symbol,size=20); b.clicked.connect(fn); buttons.addWidget(b)
        buttons.addStretch(1); pl.addLayout(buttons); layout.addWidget(panel,1); self._register_page("worlds",page)

    def _build_settings_page(self):
        page = QWidget(); page.setObjectName("settingsPage"); layout = QVBoxLayout(page); layout.setContentsMargins(0,0,0,0); layout.setSpacing(12)
        layout.addWidget(self._page_header("Palworld Settings", "Human-readable names, detailed explanations, available values, and safe verified writes to PalWorldSettings.ini.", self.ICONS["settings"]))
        top = QFrame(); top.setObjectName("panel"); tl = QHBoxLayout(top); tl.setContentsMargins(14,10,14,10)
        self.setting_search = QLineEdit(); self.setting_search.setPlaceholderText("Search by readable name, technical key, description, or allowed values..."); self.setting_search.textChanged.connect(self.filter_settings_rows); tl.addWidget(self.setting_search,2)
        self.setting_category = QComboBox(); self.setting_category.addItem("All Categories"); self.setting_category.currentTextChanged.connect(self.filter_settings_rows); tl.addWidget(self.setting_category,1)
        refresh = QPushButton("Refresh"); refresh.setObjectName("ghostButton"); self._set_button_icon(refresh,self.ICONS["refresh"],size=19); refresh.clicked.connect(self.refresh_settings_page); tl.addWidget(refresh)
        compare = QPushButton("Compare Defaults"); compare.setObjectName("ghostButton"); compare.clicked.connect(self.show_compare_defaults); tl.addWidget(compare)
        layout.addWidget(top)
        body = QHBoxLayout(); body.setSpacing(12)
        table_panel=QFrame(); table_panel.setObjectName("panel"); tpl=QVBoxLayout(table_panel); tpl.setContentsMargins(12,12,12,12)
        self.settings_table=QTableWidget(0,4); self.settings_table.setHorizontalHeaderLabels(["Setting", "Current Value", "Category", "Technical Key"]); self.settings_table.setSelectionBehavior(QTableWidget.SelectRows); self.settings_table.setEditTriggers(QTableWidget.NoEditTriggers); self.settings_table.verticalHeader().setVisible(False); self.settings_table.horizontalHeader().setSectionResizeMode(0,QHeaderView.Stretch); self.settings_table.horizontalHeader().setSectionResizeMode(1,QHeaderView.ResizeToContents); self.settings_table.horizontalHeader().setSectionResizeMode(2,QHeaderView.ResizeToContents); self.settings_table.horizontalHeader().setSectionResizeMode(3,QHeaderView.Stretch); self.settings_table.itemSelectionChanged.connect(self.setting_selection_changed); tpl.addWidget(self.settings_table); body.addWidget(table_panel,3)
        detail=QFrame(); detail.setObjectName("detailCard"); dl=QVBoxLayout(detail); dl.setContentsMargins(16,14,16,14); dl.setSpacing(9)
        self.setting_detail_name=QLabel("Select a setting"); self.setting_detail_name.setObjectName("detailTitle"); self.setting_detail_name.setWordWrap(True); dl.addWidget(self.setting_detail_name)
        self.setting_detail_key=QLabel(""); self.setting_detail_key.setObjectName("detailKey"); dl.addWidget(self.setting_detail_key)
        self.setting_detail_desc=QLabel("Choose a row to see exactly what the setting controls."); self.setting_detail_desc.setObjectName("detailDescription"); self.setting_detail_desc.setWordWrap(True); dl.addWidget(self.setting_detail_desc)
        allowed_title=QLabel("Available / accepted values"); allowed_title.setObjectName("panelTitle"); dl.addWidget(allowed_title)
        self.setting_allowed=QLabel("-"); self.setting_allowed.setObjectName("allowedValues"); self.setting_allowed.setWordWrap(True); dl.addWidget(self.setting_allowed)
        current_title=QLabel("New value"); current_title.setObjectName("panelTitle"); dl.addWidget(current_title)
        self.setting_value_edit=QLineEdit(); dl.addWidget(self.setting_value_edit)
        self.setting_value_combo=QComboBox(); self.setting_value_combo.hide(); dl.addWidget(self.setting_value_combo)
        save=QPushButton("Save Setting"); save.setObjectName("successButton"); save.setProperty("compactAction", True); save.setMaximumWidth(220); self._set_button_icon(save,self.ICONS["save"],size=20); save.clicked.connect(self.save_selected_setting); dl.addWidget(save, 0, Qt.AlignLeft)
        reset=QPushButton("Reset Selected to Default"); reset.setObjectName("warningButton"); reset.setProperty("compactAction", True); reset.setMaximumWidth(220); reset.clicked.connect(self.reset_selected_setting); dl.addWidget(reset, 0, Qt.AlignLeft)
        reset_all=QPushButton("Reset All to Defaults"); reset_all.setObjectName("dangerButton"); reset_all.setProperty("compactAction", True); reset_all.setMaximumWidth(220); reset_all.clicked.connect(self.reset_all_settings); dl.addWidget(reset_all, 0, Qt.AlignLeft)
        dl.addStretch(1)
        profile_title=QLabel("Configuration Profile"); profile_title.setObjectName("panelTitle"); dl.addWidget(profile_title)
        self.profile_combo=QComboBox(); dl.addWidget(self.profile_combo)
        apply_profile=QPushButton("Apply Selected Profile"); apply_profile.setObjectName("primaryButton"); apply_profile.setProperty("compactAction", True); apply_profile.setMaximumWidth(220); apply_profile.clicked.connect(self.apply_profile); dl.addWidget(apply_profile, 0, Qt.AlignLeft)
        body.addWidget(detail,2); layout.addLayout(body,1); self._register_page("settings",page)

    def _build_mods_page(self):
        page=QWidget(); page.setObjectName("modsPage"); layout=QVBoxLayout(page); layout.setContentsMargins(0,0,0,0); layout.setSpacing(12)
        layout.addWidget(self._page_header(
            "Mods & Runtime",
            "Manage per-server Linux UE4SS runtime, managed mod packages, runtime health, and synchronized client mod packs.",
            self.ICONS["mods"],
        ))

        runtime=QFrame(); runtime.setObjectName("panel"); rl=QGridLayout(runtime); rl.setContentsMargins(14,12,14,12); rl.setHorizontalSpacing(18); rl.setVerticalSpacing(7)
        title=QLabel("Linux Mod Runtime"); title.setObjectName("panelTitle"); rl.addWidget(title,0,0,1,4)
        self.mod_runtime_state=QLabel("Loading…"); self.mod_runtime_state.setObjectName("detailTitle")
        self.mod_runtime_version=QLabel("Runtime: -"); self.mod_runtime_version.setObjectName("pageSubtitle")
        self.mod_runtime_health=QLabel("Health: -"); self.mod_runtime_health.setObjectName("pageSubtitle")
        self.modset_version_label=QLabel("Modset: -"); self.modset_version_label.setObjectName("pageSubtitle")
        rl.addWidget(self.mod_runtime_state,1,0); rl.addWidget(self.mod_runtime_version,1,1); rl.addWidget(self.mod_runtime_health,1,2); rl.addWidget(self.modset_version_label,1,3)
        runtime_note=QLabel("Linux mod support uses a community UE4SS runtime and is managed separately for each Palworld server. Runtime/mod changes create a backup and use a controlled server restart.")
        runtime_note.setObjectName("pageSubtitle"); runtime_note.setWordWrap(True); rl.addWidget(runtime_note,2,0,1,4)
        actions=QHBoxLayout(); actions.setSpacing(8)
        self.mod_enable_runtime=QPushButton("Enable Mod Support"); self.mod_enable_runtime.setObjectName("successButton"); self.mod_enable_runtime.setProperty("compactAction",True); self.mod_enable_runtime.clicked.connect(self.enable_mod_runtime)
        self.mod_disable_runtime=QPushButton("Disable Mod Support"); self.mod_disable_runtime.setObjectName("warningButton"); self.mod_disable_runtime.setProperty("compactAction",True); self.mod_disable_runtime.clicked.connect(self.disable_mod_runtime)
        self.mod_validate_button=QPushButton("Validate Runtime"); self.mod_validate_button.setObjectName("primaryButton"); self.mod_validate_button.setProperty("compactAction",True); self.mod_validate_button.clicked.connect(self.validate_mod_runtime)
        refresh=QPushButton("Refresh"); refresh.setObjectName("ghostButton"); refresh.setProperty("compactAction",True); refresh.clicked.connect(self.refresh_mods_page)
        for b in (self.mod_enable_runtime,self.mod_disable_runtime,self.mod_validate_button,refresh): actions.addWidget(b)
        actions.addStretch(1); rl.addLayout(actions,3,0,1,4)
        layout.addWidget(runtime)

        self.mods_tabs=QTabWidget(); self.mods_tabs.setObjectName("modsTabs")

        installed_tab=QWidget(); installed_layout=QVBoxLayout(installed_tab); installed_layout.setContentsMargins(0,0,0,0); installed_layout.setSpacing(12)
        panel=QFrame(); panel.setObjectName("panel"); pl=QVBoxLayout(panel); pl.setContentsMargins(14,12,14,12); pl.setSpacing(9)
        top=QHBoxLayout(); mt=QLabel("Installed Mods"); mt.setObjectName("panelTitle"); top.addWidget(mt); top.addStretch(1)
        self.client_pack_status=QLabel("Client pack: -"); self.client_pack_status.setObjectName("pageSubtitle"); top.addWidget(self.client_pack_status); pl.addLayout(top)
        self.mods_table=QTableWidget(0,8); self.mods_table.setAlternatingRowColors(True); self.mods_table.setEditTriggers(QTableWidget.NoEditTriggers); self.mods_table.setSelectionBehavior(QTableWidget.SelectRows); self.mods_table.setSelectionMode(QTableWidget.SingleSelection); self.mods_table.verticalHeader().setVisible(False)
        self.mods_table.setHorizontalHeaderLabels(["State","Mod","Version","Type","Runtime","Server","Client","Compatibility"])
        self.mods_table.horizontalHeader().setSectionResizeMode(0,QHeaderView.ResizeToContents); self.mods_table.horizontalHeader().setSectionResizeMode(1,QHeaderView.Stretch); self.mods_table.horizontalHeader().setSectionResizeMode(2,QHeaderView.ResizeToContents); self.mods_table.horizontalHeader().setSectionResizeMode(3,QHeaderView.ResizeToContents); self.mods_table.horizontalHeader().setSectionResizeMode(4,QHeaderView.ResizeToContents); self.mods_table.horizontalHeader().setSectionResizeMode(5,QHeaderView.ResizeToContents); self.mods_table.horizontalHeader().setSectionResizeMode(6,QHeaderView.ResizeToContents); self.mods_table.horizontalHeader().setSectionResizeMode(7,QHeaderView.Stretch)
        pl.addWidget(self.mods_table,1)
        buttons=QHBoxLayout(); buttons.setSpacing(8)
        self.mod_install_button=QPushButton("Install Mod Package"); self.mod_install_button.setObjectName("successButton"); self.mod_install_button.setProperty("compactAction",True); self.mod_install_button.clicked.connect(self.install_mod_package)
        self.mod_toggle_button=QPushButton("Enable / Disable Selected"); self.mod_toggle_button.setObjectName("primaryButton"); self.mod_toggle_button.setProperty("compactAction",True); self.mod_toggle_button.clicked.connect(self.toggle_selected_mod)
        self.mod_remove_button=QPushButton("Remove Selected"); self.mod_remove_button.setObjectName("dangerButton"); self.mod_remove_button.setProperty("compactAction",True); self.mod_remove_button.clicked.connect(self.remove_selected_mod)
        self.mod_pack_button=QPushButton("Download Client Pack"); self.mod_pack_button.setObjectName("ghostButton"); self.mod_pack_button.setProperty("compactAction",True); self.mod_pack_button.clicked.connect(self.download_client_mod_pack)
        for b in (self.mod_install_button,self.mod_toggle_button,self.mod_remove_button,self.mod_pack_button): buttons.addWidget(b)
        buttons.addStretch(1); pl.addLayout(buttons)
        format_note=QLabel("Managed ZIP format: palserver-mod.json at the archive root, with optional server/ and client/ payload folders. Use Browse Linux Mods for vetted one-click installs from supported upstream catalogs.")
        format_note.setObjectName("pageSubtitle"); format_note.setWordWrap(True); pl.addWidget(format_note)
        installed_layout.addWidget(panel,1)

        log_panel=QFrame(); log_panel.setObjectName("panel"); ll=QVBoxLayout(log_panel); ll.setContentsMargins(14,12,14,12); ll.setSpacing(7)
        log_top=QHBoxLayout(); lt=QLabel("UE4SS Runtime Log"); lt.setObjectName("panelTitle"); log_top.addWidget(lt); log_top.addStretch(1); ll.addLayout(log_top)
        self.mod_log_view=QPlainTextEdit(); self.mod_log_view.setReadOnly(True); self.mod_log_view.setLineWrapMode(QPlainTextEdit.NoWrap); self.mod_log_view.setMaximumHeight(190); self.mod_log_view.setPlaceholderText("UE4SS.log will appear here after the runtime starts."); ll.addWidget(self.mod_log_view)
        installed_layout.addWidget(log_panel)
        self.mods_tabs.addTab(installed_tab, "Installed")

        browse_tab=QWidget(); browse_layout=QVBoxLayout(browse_tab); browse_layout.setContentsMargins(10,10,10,10); browse_layout.setSpacing(10)
        browser=QFrame(); browser.setObjectName("panel"); bl=QVBoxLayout(browser); bl.setContentsMargins(14,12,14,12); bl.setSpacing(10)
        browser_title=QHBoxLayout(); bt=QLabel("Linux Mod Catalog"); bt.setObjectName("panelTitle"); browser_title.addWidget(bt); browser_title.addStretch(1)
        self.catalog_page_label=QLabel("Page 1"); self.catalog_page_label.setObjectName("pageSubtitle"); browser_title.addWidget(self.catalog_page_label); bl.addLayout(browser_title)
        browser_note=QLabel("One-click install is limited to native-Linux-safe UE4SS packages. PalServer Manager scans every downloaded archive and refuses Windows DLL-only or ambiguous installers before touching the server.")
        browser_note.setObjectName("pageSubtitle"); browser_note.setWordWrap(True); bl.addWidget(browser_note)

        key_row=QHBoxLayout(); key_row.setSpacing(8)
        key_row.addWidget(QLabel("Source"))
        self.catalog_provider=QComboBox(); self.catalog_provider.addItem("Curated Linux", "curated"); self.catalog_provider.addItem("CurseForge", "curseforge"); self.catalog_provider.setCurrentIndex(max(0,self.catalog_provider.findData(getattr(self.cfg,"mod_catalog_provider","curated")))); self.catalog_provider.currentIndexChanged.connect(self.catalog_provider_changed); key_row.addWidget(self.catalog_provider)
        key_row.addWidget(QLabel("CurseForge API key"))
        self.catalog_api_key=QLineEdit(str(getattr(self.cfg,"curseforge_api_key","") or "")); self.catalog_api_key.setEchoMode(QLineEdit.Password); self.catalog_api_key.setPlaceholderText("Required for live CurseForge search; never sent to agents"); key_row.addWidget(self.catalog_api_key,1)
        save_key=QPushButton("Save Key"); save_key.setObjectName("ghostButton"); save_key.setProperty("compactAction",True); save_key.clicked.connect(self.save_catalog_key); key_row.addWidget(save_key)
        bl.addLayout(key_row)

        search_row=QHBoxLayout(); search_row.setSpacing(8)
        self.catalog_search=QLineEdit(); self.catalog_search.setPlaceholderText("Search Linux-compatible Palworld server mods..."); self.catalog_search.returnPressed.connect(lambda: self.search_mod_catalog(reset=True)); search_row.addWidget(self.catalog_search,1)
        self.catalog_filter=QComboBox(); self.catalog_filter.addItem("Verified + Candidates", "safe"); self.catalog_filter.addItem("Verified only", "verified"); self.catalog_filter.addItem("All server mods", "server"); self.catalog_filter.currentIndexChanged.connect(lambda _i: self._apply_catalog_rows()); search_row.addWidget(self.catalog_filter)
        search_button=QPushButton("Search"); search_button.setObjectName("primaryButton"); search_button.setProperty("compactAction",True); search_button.clicked.connect(lambda: self.search_mod_catalog(reset=True)); search_row.addWidget(search_button)
        bl.addLayout(search_row)

        self.catalog_table=QTableWidget(0,8); self.catalog_table.setAlternatingRowColors(True); self.catalog_table.setEditTriggers(QTableWidget.NoEditTriggers); self.catalog_table.setSelectionBehavior(QTableWidget.SelectRows); self.catalog_table.setSelectionMode(QTableWidget.SingleSelection); self.catalog_table.verticalHeader().setVisible(False)
        self.catalog_table.setHorizontalHeaderLabels(["Linux","Mod","Version","Source","Type","Client","Downloads","Updated"])
        self.catalog_table.horizontalHeader().setSectionResizeMode(0,QHeaderView.ResizeToContents); self.catalog_table.horizontalHeader().setSectionResizeMode(1,QHeaderView.Stretch); self.catalog_table.horizontalHeader().setSectionResizeMode(2,QHeaderView.ResizeToContents); self.catalog_table.horizontalHeader().setSectionResizeMode(3,QHeaderView.ResizeToContents); self.catalog_table.horizontalHeader().setSectionResizeMode(4,QHeaderView.ResizeToContents); self.catalog_table.horizontalHeader().setSectionResizeMode(5,QHeaderView.ResizeToContents); self.catalog_table.horizontalHeader().setSectionResizeMode(6,QHeaderView.ResizeToContents); self.catalog_table.horizontalHeader().setSectionResizeMode(7,QHeaderView.ResizeToContents)
        self.catalog_table.itemSelectionChanged.connect(self.catalog_selection_changed); bl.addWidget(self.catalog_table,1)
        self.catalog_detail=QLabel("Select a mod to see Linux compatibility details."); self.catalog_detail.setObjectName("pageSubtitle"); self.catalog_detail.setWordWrap(True); bl.addWidget(self.catalog_detail)
        catalog_buttons=QHBoxLayout(); catalog_buttons.setSpacing(8)
        self.catalog_install_button=QPushButton("Install Selected"); self.catalog_install_button.setObjectName("successButton"); self.catalog_install_button.setProperty("compactAction",True); self.catalog_install_button.clicked.connect(self.install_catalog_selected); self.catalog_install_button.setEnabled(False); catalog_buttons.addWidget(self.catalog_install_button)
        self.catalog_prev=QPushButton("Previous"); self.catalog_prev.setObjectName("ghostButton"); self.catalog_prev.setProperty("compactAction",True); self.catalog_prev.clicked.connect(lambda: self.change_catalog_page(-1)); catalog_buttons.addWidget(self.catalog_prev)
        self.catalog_next=QPushButton("Next"); self.catalog_next.setObjectName("ghostButton"); self.catalog_next.setProperty("compactAction",True); self.catalog_next.clicked.connect(lambda: self.change_catalog_page(1)); catalog_buttons.addWidget(self.catalog_next)
        catalog_buttons.addStretch(1)
        self.catalog_status=QLabel("Curated Linux catalog is ready. Add your own CurseForge API key for live search."); self.catalog_status.setObjectName("pageSubtitle"); catalog_buttons.addWidget(self.catalog_status)
        bl.addLayout(catalog_buttons)
        browse_layout.addWidget(browser,1)
        self.mods_tabs.addTab(browse_tab, "Browse Linux Mods")
        layout.addWidget(self.mods_tabs,1)
        self.mod_rows=[]
        self.catalog_rows=[]
        self.catalog_visible_rows=[]
        self.catalog_index=0
        self.catalog_total=0
        self._mods_api_available = False
        self._mods_catalog_agent_compatible = False
        self._register_page("mods",page)

    def _build_backups_page(self):
        page=QWidget(); page.setObjectName("backupsPage"); layout=QVBoxLayout(page); layout.setContentsMargins(0,0,0,0); layout.setSpacing(12)
        layout.addWidget(self._page_header("Backup Manager", "Create, restore, and remove Palworld backups from a single integrated page.", self.ICONS["backups"]))
        panel=QFrame(); panel.setObjectName("panel"); pl=QVBoxLayout(panel); pl.setContentsMargins(14,12,14,12)
        self.backup_table=QTableWidget(0,3); self.backup_table.setHorizontalHeaderLabels(["Backup", "Size", "Created"]); self.backup_table.setSelectionBehavior(QTableWidget.SelectRows); self.backup_table.setEditTriggers(QTableWidget.NoEditTriggers); self.backup_table.verticalHeader().setVisible(False); self.backup_table.horizontalHeader().setSectionResizeMode(0,QHeaderView.Stretch); self.backup_table.horizontalHeader().setSectionResizeMode(1,QHeaderView.ResizeToContents); self.backup_table.horizontalHeader().setSectionResizeMode(2,QHeaderView.ResizeToContents); pl.addWidget(self.backup_table,1)
        buttons=QHBoxLayout()
        for text,obj,symbol,fn in (("Create Backup","successButton",self.ICONS["archive"],self.create_backup_page),("Restore Selected","warningButton",self.ICONS["restart"],self.restore_selected_backup),("Delete Selected","dangerButton",self.ICONS["delete"],self.delete_selected_backup),("Refresh","ghostButton",self.ICONS["refresh"],self.refresh_backups_page)):
            b=QPushButton(text); b.setObjectName(obj); self._set_button_icon(b,symbol,size=20); b.clicked.connect(fn); buttons.addWidget(b)
        buttons.addStretch(1); pl.addLayout(buttons); layout.addWidget(panel,1); self._register_page("backups",page)

    def _build_automation_page(self):
        page=QWidget(); page.setObjectName("automationPage"); layout=QVBoxLayout(page); layout.setContentsMargins(0,0,0,0); layout.setSpacing(12)
        layout.addWidget(self._page_header("Automation & Scheduling", "Scheduled jobs run in the always-on PalServer Manager Agent on the server host.", self.ICONS["automation"]))
        panel=QFrame(); panel.setObjectName("panel"); pl=QGridLayout(panel); pl.setContentsMargins(18,16,18,16); pl.setHorizontalSpacing(18); pl.setVerticalSpacing(12)
        self.auto_backup_enabled=QCheckBox("Enable automatic backups"); pl.addWidget(self.auto_backup_enabled,0,0,1,2)
        pl.addWidget(QLabel("Backup interval (minutes)"),1,0); self.auto_backup_interval=QSpinBox(); self.auto_backup_interval.setRange(5,10080); pl.addWidget(self.auto_backup_interval,1,1)
        pl.addWidget(QLabel("Backup retention count"),2,0); self.auto_retention=QSpinBox(); self.auto_retention.setRange(1,1000); pl.addWidget(self.auto_retention,2,1)
        self.auto_update_check=QCheckBox("Enable automatic update checks"); pl.addWidget(self.auto_update_check,3,0,1,2)
        pl.addWidget(QLabel("Update check interval (minutes)"),4,0); self.auto_update_interval=QSpinBox(); self.auto_update_interval.setRange(10,10080); pl.addWidget(self.auto_update_interval,4,1)
        self.auto_install_updates=QCheckBox("Automatically install available updates"); pl.addWidget(self.auto_install_updates,5,0,1,2)
        self.auto_only_empty=QCheckBox("Install updates only when no players are connected"); pl.addWidget(self.auto_only_empty,6,0,1,2)
        pl.addWidget(QLabel("Maintenance window start (HH:MM)"),7,0); self.auto_window_start=QLineEdit(); pl.addWidget(self.auto_window_start,7,1)
        pl.addWidget(QLabel("Maintenance window end (HH:MM)"),8,0); self.auto_window_end=QLineEdit(); pl.addWidget(self.auto_window_end,8,1)
        save=QPushButton("Save Automation Settings"); save.setObjectName("successButton"); save.setProperty("compactAction", True); save.setMaximumWidth(220); self._set_button_icon(save,self.ICONS["save"],size=20); save.clicked.connect(self.save_automation_page); pl.addWidget(save,9,0,1,2,Qt.AlignRight)
        pl.setColumnStretch(0,2); pl.setColumnStretch(1,1); layout.addWidget(panel); layout.addStretch(1); self._register_page("automation",page)

    def _build_logs_page(self):
        page=QWidget(); page.setObjectName("logsPage"); layout=QVBoxLayout(page); layout.setContentsMargins(0,0,0,0); layout.setSpacing(12)
        layout.addWidget(self._page_header("Live Server Logs", "Tail the server journal/log output and filter warnings and errors.", self.ICONS["logs"]))
        toolbar=QFrame(); toolbar.setObjectName("panel"); tb=QHBoxLayout(toolbar); tb.setContentsMargins(14,10,14,10)
        refresh=QPushButton("Refresh Logs"); refresh.setObjectName("ghostButton"); self._set_button_icon(refresh,self.ICONS["refresh"],size=20); refresh.clicked.connect(self.refresh_logs); tb.addWidget(refresh)
        warning=QPushButton("Warnings / Errors"); warning.setObjectName("warningButton"); warning.clicked.connect(lambda:self.refresh_logs(True)); tb.addWidget(warning); tb.addStretch(1); layout.addWidget(toolbar)
        panel=QFrame(); panel.setObjectName("panel"); pl=QVBoxLayout(panel); pl.setContentsMargins(11,11,11,11)
        self.log_view=QPlainTextEdit(); self.log_view.setObjectName("fullLog"); self.log_view.setReadOnly(True); self.log_view.setLineWrapMode(QPlainTextEdit.NoWrap); pl.addWidget(self.log_view); layout.addWidget(panel,1); self._register_page("logs",page)

    def _build_health_page(self):
        page = QWidget(); page.setObjectName("healthPage")
        layout = QVBoxLayout(page); layout.setContentsMargins(0,0,0,0); layout.setSpacing(12)
        layout.addWidget(self._page_header(
            "Server Health",
            "Live resource usage and every health check that contributes to the server's overall status.",
            self.ICONS["health"],
        ))

        summary = QFrame(); summary.setObjectName("panel")
        sl = QHBoxLayout(summary); sl.setContentsMargins(18,12,18,12); sl.setSpacing(12)
        self.health_page_badge = PillBadge("CHECKING", "warning")
        sl.addWidget(self.health_page_badge)
        copy = QVBoxLayout(); copy.setSpacing(1)
        self.health_page_text = QLabel("Checking server health..."); self.health_page_text.setObjectName("sectionTitle")
        self.health_page_reason = QLabel("Waiting for live server checks."); self.health_page_reason.setObjectName("pageSubtitle"); self.health_page_reason.setWordWrap(True)
        copy.addWidget(self.health_page_text); copy.addWidget(self.health_page_reason)
        sl.addLayout(copy, 1)
        refresh = QPushButton("Refresh Health"); refresh.setObjectName("ghostButton")
        self._set_button_icon(refresh, self.ICONS["refresh"], size=23); refresh.clicked.connect(self.refresh_health_page)
        sl.addWidget(refresh)
        layout.addWidget(summary)

        resources = QGridLayout(); resources.setHorizontalSpacing(12); resources.setVerticalSpacing(12)
        self.health_metric_cards = {}
        metric_defs = (
            ("cpu", "CPU Usage", "Processor load across the server host"),
            ("ram", "Memory Usage", "Physical memory currently in use"),
            ("disk", "Disk Usage", "Palworld installation volume usage"),
        )
        for col, (key, title, detail_text) in enumerate(metric_defs):
            card = QFrame(); card.setObjectName("detailCard"); card.setMinimumHeight(155); card.setMaximumHeight(175)
            cl = QVBoxLayout(card); cl.setContentsMargins(16,14,16,14); cl.setSpacing(6)
            lab = QLabel(title); lab.setObjectName("panelTitle")
            val = QLabel("-"); val.setObjectName("metricValue")
            detail = QLabel(detail_text); detail.setObjectName("pageSubtitle"); detail.setWordWrap(True)
            bar = QProgressBar(); bar.setObjectName("bigProgress"); bar.setRange(0,100); bar.setTextVisible(False)
            threshold = QLabel(""); threshold.setObjectName("panelMuted")
            cl.addWidget(lab); cl.addWidget(val); cl.addWidget(detail); cl.addWidget(bar); cl.addWidget(threshold)
            resources.addWidget(card, 0, col); resources.setColumnStretch(col, 1)
            self.health_metric_cards[key] = (val, bar, threshold)
        layout.addLayout(resources)

        checks_title = QLabel("SERVICE & AVAILABILITY CHECKS"); checks_title.setObjectName("sectionTitle")
        layout.addWidget(checks_title)
        checks_grid = QGridLayout(); checks_grid.setHorizontalSpacing(12); checks_grid.setVerticalSpacing(12)
        self.health_check_cards = {}
        check_defs = (
            ("Service", "Palworld Service", "Operating-system service/process state"),
            ("Game Port", "Game Port", "UDP game listener availability"),
            ("REST API", "REST API", "Private Palworld management API"),
            ("Backup", "Latest Backup", "Age of the newest manager backup"),
            ("Server FPS", "Server FPS", "Live dedicated-server frame rate"),
            ("Manager Link", "Manager Connection", "Current local/direct/SSH management path"),
        )
        for i, (key, title, detail_text) in enumerate(check_defs):
            card = QFrame(); card.setObjectName("detailCard"); card.setMinimumHeight(118); card.setMaximumHeight(138)
            cl = QVBoxLayout(card); cl.setContentsMargins(15,12,15,12); cl.setSpacing(4)
            top = QHBoxLayout(); t = QLabel(title); t.setObjectName("panelTitle"); state = PillBadge("CHECKING", "warning")
            top.addWidget(t); top.addStretch(1); top.addWidget(state); cl.addLayout(top)
            value = QLabel("-"); value.setObjectName("detailTitle"); value.setStyleSheet("font-size:17px;")
            detail = QLabel(detail_text); detail.setObjectName("pageSubtitle"); detail.setWordWrap(True)
            cl.addWidget(value); cl.addWidget(detail)
            checks_grid.addWidget(card, i // 3, i % 3); checks_grid.setColumnStretch(i % 3, 1)
            self.health_check_cards[key] = (value, detail, state)
        layout.addLayout(checks_grid)
        layout.addStretch(1)
        self._register_page("health", page)

    def _build_diagnostics_page(self):
        page = QWidget(); page.setObjectName("diagnosticsPage")
        layout = QVBoxLayout(page); layout.setContentsMargins(0,0,0,0); layout.setSpacing(12)
        layout.addWidget(self._page_header(
            "Diagnostics",
            "Installation, paths, service state, network tools, and configuration checks in a compact system dashboard.",
            self.ICONS["diagnostics"],
        ))
        actions = QFrame(); actions.setObjectName("panel")
        al = QHBoxLayout(actions); al.setContentsMargins(14,10,14,10); al.setSpacing(9)
        action_defs = (
            ("Refresh", self.ICONS["refresh"], self.refresh_diagnostics_page),
            ("Network", self.ICONS["connection"], self.show_network_diagnostics),
            ("Crash History", self.ICONS["report"], self.show_crash_history),
            ("Live Watchdog", self.ICONS["watchdog"], lambda: self.show_named_page("watchdog")),
            ("Server Setup", self.ICONS["server_setup"], lambda: self.show_named_page("server_setup")),
        )
        for text, symbol, fn in action_defs:
            b = QPushButton(text); b.setObjectName("ghostButton"); self._set_button_icon(b, symbol, size=23); b.clicked.connect(fn); al.addWidget(b)
        al.addStretch(1); layout.addWidget(actions)

        self.diagnostics_scroll = QScrollArea(); self.diagnostics_scroll.setWidgetResizable(True); self.diagnostics_scroll.setFrameShape(QFrame.NoFrame)
        self.diagnostics_body = QWidget(); self.diagnostics_body.setObjectName("diagnosticsBody")
        diagnostics_body_layout = QVBoxLayout(self.diagnostics_body); diagnostics_body_layout.setContentsMargins(0,0,4,4); diagnostics_body_layout.setSpacing(12)
        install_header = QLabel("INSTALLATION & SERVICE"); install_header.setObjectName("sectionTitle"); diagnostics_body_layout.addWidget(install_header)
        self.diagnostics_layout = QGridLayout(); self.diagnostics_layout.setHorizontalSpacing(12); self.diagnostics_layout.setVerticalSpacing(12)
        diagnostics_body_layout.addLayout(self.diagnostics_layout)
        network_header = QLabel("NETWORK"); network_header.setObjectName("sectionTitle"); diagnostics_body_layout.addWidget(network_header)
        self.network_diagnostics_layout = QGridLayout(); self.network_diagnostics_layout.setHorizontalSpacing(12); self.network_diagnostics_layout.setVerticalSpacing(12)
        diagnostics_body_layout.addLayout(self.network_diagnostics_layout)
        diagnostics_body_layout.addStretch(1)
        self.diagnostics_scroll.setWidget(self.diagnostics_body); layout.addWidget(self.diagnostics_scroll, 1)
        self._register_page("diagnostics", page)

    def _build_connection_page(self):
        page=QWidget(); page.setObjectName("connectionPage"); layout=QVBoxLayout(page); layout.setContentsMargins(0,0,0,0); layout.setSpacing(12)
        layout.addWidget(self._page_header("Connection", "Configure the connection used by the currently selected node.", self.ICONS["connection"]))
        self.connection_context_label=QLabel(""); self.connection_context_label.setObjectName("allowedValues"); self.connection_context_label.setWordWrap(True); layout.addWidget(self.connection_context_label)
        panel=QFrame(); panel.setObjectName("panel"); form=QGridLayout(panel); form.setContentsMargins(18,16,18,16); form.setHorizontalSpacing(18); form.setVerticalSpacing(10)
        self.conn_mode=QComboBox(); self.conn_mode.addItems(["local","direct","ssh"]); self.conn_mode.currentTextChanged.connect(self._sync_connection_fields)
        self.conn_remote_url=QLineEdit(); self.conn_token=QLineEdit(); self.conn_token.setEchoMode(QLineEdit.Password); self.conn_verify_tls=QCheckBox("Verify TLS certificate"); self.conn_ssh_host=QLineEdit(); self.conn_ssh_user=QLineEdit(); self.conn_ssh_port=QSpinBox(); self.conn_ssh_port.setRange(1,65535); self.conn_ssh_key=QLineEdit(); self.conn_local_port=QSpinBox(); self.conn_local_port.setRange(1024,65535); self.conn_remote_port=QSpinBox(); self.conn_remote_port.setRange(1,65535)
        rows=(("Mode",self.conn_mode),("Direct agent URL",self.conn_remote_url),("Agent token",self.conn_token),("TLS",self.conn_verify_tls),("SSH host",self.conn_ssh_host),("SSH user",self.conn_ssh_user),("SSH port",self.conn_ssh_port),("SSH private key",self.conn_ssh_key),("Local tunnel port",self.conn_local_port),("Remote agent port",self.conn_remote_port))
        for r,(label,widget) in enumerate(rows): form.addWidget(QLabel(label),r,0); form.addWidget(widget,r,1)
        self.conn_hint=QLabel(""); self.conn_hint.setObjectName("allowedValues"); self.conn_hint.setWordWrap(True); form.addWidget(self.conn_hint,len(rows),0,1,2)
        save=QPushButton("Save & Connect"); save.setObjectName("successButton"); save.setProperty("compactAction", True); save.setMaximumWidth(200); self._set_button_icon(save,self.ICONS["connection"],size=20); save.clicked.connect(self.save_connection_page); form.addWidget(save,len(rows)+1,0,1,2,Qt.AlignRight); form.setColumnStretch(1,1); layout.addWidget(panel); layout.addStretch(1); self._register_page("connection",page)

    def _build_about_page(self):
        page=QWidget(); page.setObjectName("aboutPage"); layout=QVBoxLayout(page); layout.setContentsMargins(0,0,0,0); layout.setSpacing(12)
        layout.addWidget(self._page_header("About PalServer Manager", "Project information and credits.", self.ICONS["about"]))
        card=QFrame(); card.setObjectName("panel"); cl=QVBoxLayout(card); cl.setContentsMargins(26,24,26,24); cl.setSpacing(12)
        title=QLabel("PalServer Manager"); title.setObjectName("serverName"); cl.addWidget(title)
        version=QLabel(f"Version {__version__}"); version.setObjectName("pageSubtitle"); cl.addWidget(version)
        developer=QLabel("Created and developed by Supr Solutions LLC"); developer.setObjectName("detailTitle"); developer.setStyleSheet("color:#62d4ff; font-size:20px; font-weight:800;"); cl.addWidget(developer)
        text=QLabel("PalServer Manager is a cross-platform management interface for Palworld dedicated servers. It supports local administration, secure SSH-tunneled remote management, Palworld REST API telemetry, backups, world management, automation, settings, diagnostics, player administration, and server updates."); text.setObjectName("detailDescription"); text.setWordWrap(True); cl.addWidget(text)
        note=QLabel("PalServer Manager is an unofficial server-management project and is not affiliated with Pocketpair, Inc."); note.setObjectName("allowedValues"); note.setWordWrap(True); cl.addWidget(note); cl.addStretch(1); layout.addWidget(card,1); self._register_page("about",page)

    def _build_report_page(self):
        page=QWidget(); page.setObjectName("reportPage"); layout=QVBoxLayout(page); layout.setContentsMargins(0,0,0,0); layout.setSpacing(12)
        self.report_header=self._page_header("Report", "Structured results are shown here instead of raw JSON.", self.ICONS["report"]); layout.addWidget(self.report_header)
        self.report_title_labels=[w for w in self.report_header.findChildren(QLabel) if w.objectName()=="pageTitle"]
        self.report_subtitle_labels=[w for w in self.report_header.findChildren(QLabel) if w.objectName()=="pageSubtitle"]
        self.report_action=QPushButton(""); self.report_action.setObjectName("primaryButton"); self.report_action.hide(); layout.addWidget(self.report_action,0,Qt.AlignRight)
        scroll=QScrollArea(); scroll.setWidgetResizable(True); scroll.setFrameShape(QFrame.NoFrame); scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff); self.report_body=QWidget(); self.report_body.setObjectName("reportBody"); self.report_layout=QVBoxLayout(self.report_body); self.report_layout.setContentsMargins(0,0,4,0); self.report_layout.setSpacing(10); self.report_layout.addStretch(1); scroll.setWidget(self.report_body); layout.addWidget(scroll,1); self._register_page("report",page)

    def _build_watchdog_page(self):
        page=QWidget(); page.setObjectName("watchdogPage"); layout=QVBoxLayout(page); layout.setContentsMargins(0,0,0,0); layout.setSpacing(12)
        layout.addWidget(self._page_header("Live Watchdog", "Live service, endpoint, resource, player, and log information in the main window.", self.ICONS["watchdog"]))
        summary=QFrame(); summary.setObjectName("panel"); sl=QGridLayout(summary); sl.setContentsMargins(16,14,16,14); sl.setHorizontalSpacing(24); sl.setVerticalSpacing(8); self.watchdog_values={}
        watchdog_fields = (
            "Server", "Service",
            "Endpoint", "Players",
            "Health", "Host CPU Load",
            "Host Memory", "Storage",
            "PalServer Process", "Server Time",
        )
        for i,key in enumerate(watchdog_fields):
            label=QLabel(key); label.setObjectName("summaryTitle")
            value=QLabel("-"); value.setObjectName("summaryValue"); value.setStyleSheet("font-size:14px;"); value.setWordWrap(True)
            sl.addWidget(label,i//2*2,i%2*2); sl.addWidget(value,i//2*2,i%2*2+1); self.watchdog_values[key]=value
        sl.setColumnStretch(1, 1); sl.setColumnStretch(3, 1)
        layout.addWidget(summary)

        cpu_note = QFrame(); cpu_note.setObjectName("detailCard")
        cn = QHBoxLayout(cpu_note); cn.setContentsMargins(14,10,14,10); cn.setSpacing(10)
        icon = QLabel(self.ICONS["watchdog"]); icon.setObjectName("toolIcon"); icon.setStyleSheet("font-size:22px; color:#62d4ff;")
        note = QLabel(
            "CPU readings measure two different things: Host CPU Load is total Ubuntu server/VM CPU utilization across all cores. "
            "PalServer Process CPU is only the Palworld process; 100% equals one logical CPU core, so a multi-threaded PalServer can correctly exceed 100%."
        )
        note.setObjectName("allowedValues"); note.setWordWrap(True)
        cn.addWidget(icon); cn.addWidget(note, 1)
        layout.addWidget(cpu_note)

        panel=QFrame(); panel.setObjectName("panel"); pl=QVBoxLayout(panel); pl.setContentsMargins(12,12,12,12); self.watchdog_logs=QPlainTextEdit(); self.watchdog_logs.setReadOnly(True); self.watchdog_logs.setLineWrapMode(QPlainTextEdit.NoWrap); pl.addWidget(self.watchdog_logs); layout.addWidget(panel,1); self._register_page("watchdog",page)

    def _build_server_setup_page(self):
        page=QWidget(); page.setObjectName("serverSetupPage"); layout=QVBoxLayout(page); layout.setContentsMargins(0,0,0,0); layout.setSpacing(12)
        layout.addWidget(self._page_header("Server Setup", "Configure server-host paths, service name, game port, and Palworld REST API integration.", self.ICONS["server_setup"]))
        panel=QFrame(); panel.setObjectName("panel"); form=QGridLayout(panel); form.setContentsMargins(18,16,18,16); form.setHorizontalSpacing(18); form.setVerticalSpacing(9)
        self.setup_install_dir=QLineEdit(); self.setup_steamcmd=QLineEdit(); self.setup_steam_user=QLineEdit(); self.setup_service_name=QLineEdit(); self.setup_game_port=QSpinBox(); self.setup_game_port.setRange(1,65535); self.setup_rest_host=QLineEdit(); self.setup_rest_port=QSpinBox(); self.setup_rest_port.setRange(1,65535); self.setup_rest_user=QLineEdit(); self.setup_admin_password=QLineEdit(); self.setup_admin_password.setEchoMode(QLineEdit.Password); self.setup_admin_password.setPlaceholderText("Leave blank to keep current password"); self.setup_sync_rest=QCheckBox("Enable/sync Palworld REST API settings in PalWorldSettings.ini")
        rows=(("Palworld install directory",self.setup_install_dir),("SteamCMD path",self.setup_steamcmd),("Linux Steam/Palworld user",self.setup_steam_user),("OS service name",self.setup_service_name),("Game listen port",self.setup_game_port),("Palworld REST host",self.setup_rest_host),("Palworld REST port",self.setup_rest_port),("REST username",self.setup_rest_user),("Admin password",self.setup_admin_password))
        for r,(label,widget) in enumerate(rows): form.addWidget(QLabel(label),r,0); form.addWidget(widget,r,1)
        form.addWidget(self.setup_sync_rest,len(rows),0,1,2)
        save=QPushButton("Save Server Setup"); save.setObjectName("successButton"); save.setProperty("compactAction", True); save.setMaximumWidth(220); self._set_button_icon(save,self.ICONS["save"],size=20); save.clicked.connect(self.save_server_setup_page); form.addWidget(save,len(rows)+1,0,1,2,Qt.AlignRight); form.setColumnStretch(1,1); layout.addWidget(panel); layout.addStretch(1); self._register_page("server_setup",page)

    def _safe_tools(self):
        excluded = self.DIRECT_ACTION_TOOL_IDS | self.HIDDEN_TOOL_IDS
        return [tool for tool in TOOLS if tool.id not in excluded]

    def _tool_columns(self):
        width = 0
        if hasattr(self, "tools_scroll"):
            width = self.tools_scroll.viewport().width()
        if width <= 0 and hasattr(self, "stack"):
            width = self.stack.width()
        # Three compact columns produce six short rows for the current safe
        # tool set. That uses the default window height efficiently while
        # keeping every tool on one page with no horizontal scrollbar.
        if width >= 900:
            return 3
        if width >= 580:
            return 2
        return 1

    def render_tools(self):
        while self.tools_grid.count():
            item = self.tools_grid.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        safe_tools = self._safe_tools()
        columns = self._tool_columns()
        self._last_tool_columns = columns

        for i, tool in enumerate(safe_tools):
            symbol = self.ICONS.get(self._tool_page_target(tool.id) or "report", "\u25a6")
            card = ToolNavCard(
                tool.name,
                tool.description,
                symbol,
                lambda tool_id=tool.id: self.run_tool(tool_id),
            )
            self.tools_grid.addWidget(card, i // columns, i % columns)

        row_count = max(1, (len(safe_tools) + columns - 1) // columns)
        for col in range(3):
            self.tools_grid.setColumnStretch(col, 1 if col < columns else 0)
        for row in range(row_count):
            self.tools_grid.setRowStretch(row, 1)
        self.page_label.setText(f"{len(safe_tools)} tools")
        self.prev_button.hide(); self.next_button.hide()

    def change_tool_page(self,delta):
        # Retained for compatibility with older shortcuts. Tools are now all on
        # one responsive page, so there is no page to change.
        self.render_tools()

    def _tool_page_target(self,tool_id):
        return {"dashboard":"dashboard","watchdog":"watchdog","health":"health","logs":"logs","settings":"settings","mods":"mods","search":"settings","profiles":"settings","backups":"backups","worlds":"worlds","scheduler":"automation","players":"players","broadcast":"players","network":"diagnostics","diagnostics":"diagnostics","setup":"server_setup","connection":"connection"}.get(tool_id)

    @staticmethod
    def _display(value,fallback="-"):
        return fallback if value in (None,"","None") else str(value)

    @staticmethod
    def _format_uptime(seconds):
        if seconds in (None,""): return "-"
        try: seconds=int(float(seconds))
        except Exception: return str(seconds)
        hours,rem=divmod(seconds,3600); minutes,_=divmod(rem,60); return f"{hours}h {minutes}m" if hours else f"{minutes}m"

    @staticmethod
    def _format_age(seconds):
        seconds=int(max(0,seconds))
        if seconds<60: return f"{seconds}s"
        if seconds<3600: return f"{seconds//60}m"
        if seconds<86400:
            hours,rem=divmod(seconds,3600); minutes=rem//60; return f"{hours}h {minutes}m" if minutes else f"{hours}h"
        return f"{seconds//86400}d"

    @staticmethod
    def _remaining(interval_minutes,elapsed_seconds=0):
        try: remaining=max(0,int(interval_minutes)*60-int(elapsed_seconds))
        except Exception: return "-"
        if remaining<60: return "Due now" if remaining==0 else f"In {remaining}s"
        hours,rem=divmod(remaining,3600); minutes=rem//60; return f"In {hours}h {minutes}m" if hours else f"In {minutes}m"

    def _set_health_row(self,key,value,state="healthy"):
        if key not in self.health_rows: return
        dot,value_label=self.health_rows[key]; dot.setObjectName("healthDotGood" if state=="healthy" else ("healthDotWarn" if state=="warning" else "healthDotBad")); dot.style().unpolish(dot); dot.style().polish(dot); value_label.setText(str(value))

    def _set_connection_state(self, connected: bool, mode=None, host=None):
        mode_text = str(mode or self._current_connection_mode() or "local").upper()
        if connected:
            label = f"\u25cf {mode_text}" + (f"  {host}" if host else "")
            side = f"\u25cf Connected via {mode_text}"
        else:
            label = "\u25cf DISCONNECTED"
            side = "\u25cf Disconnected"
        self.connection_label.setText(label)
        self.connection_label.setProperty("connected", bool(connected))
        self.sidebar_status.setText(side)
        self.sidebar_status.setProperty("connected", bool(connected))
        for widget in (self.connection_label, self.sidebar_status):
            widget.style().unpolish(widget); widget.style().polish(widget)


    def _fleet_mode(self):
        return bool(getattr(self.cfg, "fleet_hosts", [])) and hasattr(self.manager, "hosts")

    def _active_host_id(self):
        if not self._fleet_mode():
            return ""
        host_id = str(getattr(self.cfg, "active_fleet_host_id", "") or "")
        if host_id and any(row.id == host_id for row in self.cfg.fleet_hosts):
            return host_id
        try:
            ref = self.cfg.fleet_server(self.cfg.active_fleet_server_id)
            return str(ref.host_id)
        except Exception:
            return str(self.cfg.fleet_hosts[0].id) if self.cfg.fleet_hosts else ""

    def _set_active_host_id(self, value):
        if self._fleet_mode():
            self.cfg.active_fleet_host_id = str(value or "")

    def _active_host_config(self):
        if not self._fleet_mode():
            return None
        try:
            return self.cfg.fleet_host(self._active_host_id())
        except Exception:
            return None

    def _current_connection_mode(self):
        host = self._active_host_config()
        return str(host.mode if host is not None else self.cfg.connection.mode or "local")

    def _active_server_id(self):
        return str(self.cfg.active_fleet_server_id if self._fleet_mode() else self.cfg.active_instance_id)

    def _set_active_server_id(self, value):
        if self._fleet_mode():
            self.cfg.active_fleet_server_id = str(value)
            try:
                self.cfg.active_fleet_host_id = self.cfg.fleet_server(str(value)).host_id
            except Exception:
                pass
        else:
            self.cfg.active_instance_id = str(value)

    def _server_context_available(self):
        if not self._fleet_mode():
            return True
        try:
            ref = self.cfg.fleet_server(self.cfg.active_fleet_server_id)
            return str(ref.host_id) == self._active_host_id()
        except Exception:
            return False

    def _servers_on_active_host(self, rows=None):
        rows = list(self.instance_rows if rows is None else (rows or []))
        if not self._fleet_mode():
            return rows
        host_id = self._active_host_id()
        return [row for row in rows if str(row.get("host_id") or "") == host_id]

    def _active_instance_row(self):
        active_id = self._active_server_id()
        for row in self.instance_rows:
            if str(row.get("id") or "") == active_id:
                return row
        return {}

    def _populate_node_selector(self):
        if not hasattr(self, "node_selector"):
            return
        if not self._fleet_mode():
            self.node_selector.hide()
            return
        self.node_selector.show()
        active_host = self._active_host_id()
        self._loading_node_selector = True
        try:
            self.node_selector.clear()
            selected_index = 0
            for index, host in enumerate(self.cfg.fleet_hosts):
                label = str(host.name or host.id)
                self.node_selector.addItem(f"Node: {label}", host.id)
                if host.id == active_host:
                    selected_index = index
            if self.node_selector.count():
                self.node_selector.setCurrentIndex(selected_index)
                selected_host = self.cfg.fleet_host(str(self.node_selector.currentData()))
                address = str(selected_host.ssh_host or selected_host.remote_url or "").strip()
                self.node_selector.setToolTip(
                    f"Selected node: {selected_host.name} ({selected_host.id})" + (f" • {address}" if address else "")
                )
        finally:
            self._loading_node_selector = False

    def refresh_instances(self, silent=False, refresh_after=False):
        def done(rows):
            self._apply_instances(rows)
            if refresh_after:
                self.refresh_dashboard(silent=True)
        def failed(message):
            if refresh_after:
                self.refresh_dashboard(silent=True)
            if not silent:
                self.show_notice(f"Server Instances: {message}", "error", 10000)
        self._run_async("instances-list", "Server Instances", lambda: self.manager.instances(True), done, failed, silent=silent)

    def _apply_instances(self, rows):
        rows = list(rows or [])
        self.instance_rows = rows
        self._populate_node_selector()

        visible_rows = self._servers_on_active_host(rows)
        self.visible_instance_rows = visible_rows
        active_id = self._active_server_id()
        visible_ids = [str(row.get("id", "")) for row in visible_rows]

        # The selected server must always belong to the selected node. Node
        # changes normally select a server through switch_node(); this fallback
        # keeps older configs and externally edited catalogs consistent.
        if visible_rows and active_id not in visible_ids:
            active_id = visible_ids[0]
            self._set_active_server_id(active_id)
            save_config(self.cfg)

        self._loading_instance_selector = True
        try:
            self.instance_selector.clear()
            selected_index = 0
            for index, row in enumerate(visible_rows):
                name = str(row.get("name") or row.get("id") or "Server")
                state = str(row.get("state", "unknown")).lower()
                suffix = " • Running" if state in {"active", "running"} else ""
                self.instance_selector.addItem(f"{name}{suffix}", str(row.get("id", "")))
                if str(row.get("id", "")) == active_id:
                    selected_index = index
            if visible_rows:
                self.instance_selector.setEnabled(True)
                self.instance_selector.setCurrentIndex(selected_index)
                self.instance_selector.setToolTip("Select a Palworld server on the selected node")
            else:
                self.instance_selector.addItem("No servers on this node", "")
                self.instance_selector.setCurrentIndex(0)
                self.instance_selector.setEnabled(False)
                self.instance_selector.setToolTip("This node has no linked Palworld servers")
        finally:
            self._loading_instance_selector = False

        if hasattr(self, "instance_next_id_label"):
            try:
                self.instance_next_id_label.setText(self.cfg.next_fleet_server_id() if self._fleet_mode() else "Auto")
            except Exception:
                self.instance_next_id_label.setText("Auto")

        # The Servers page is node-scoped too. Remote Hosts remains the fleet
        # inventory page when the owner wants to see every node at once.
        if hasattr(self, "instances_table"):
            self.instances_table.setRowCount(len(visible_rows))
            active_row = -1
            for r, row in enumerate(visible_rows):
                state = str(row.get("state", "unknown"))
                values = (
                    row.get("name", "-"), row.get("id", "-"),
                    row.get("host_name") or row.get("host_address") or "Local Host", state,
                    row.get("service_name", "-"), row.get("game_port", "-"),
                    row.get("rest_api_port", "-"), row.get("install_dir", "-"),
                )
                for c, value in enumerate(values):
                    item = QTableWidgetItem(str(value))
                    if c == 0 and str(row.get("id", "")) == active_id:
                        item.setText(f"{value}  • CURRENT")
                    self.instances_table.setItem(r, c, item)
                if str(row.get("id", "")) == active_id:
                    active_row = r
            if active_row >= 0:
                self.instances_table.selectRow(active_row)

    def _node_selector_changed(self, index):
        if self._loading_node_selector or index < 0 or not self._fleet_mode():
            return
        host_id = str(self.node_selector.itemData(index) or "")
        if host_id and host_id != self._active_host_id():
            self.switch_node(host_id, after_page=self.current_page_name())

    def switch_node(self, host_id: str, after_page="dashboard"):
        target = str(host_id or "").strip()
        if not target or not self._fleet_mode():
            return
        try:
            host = self.cfg.fleet_host(target)
        except Exception as exc:
            self.show_notice(f"Switch Node: {exc}", "error", 10000)
            return

        self.node_selector.setEnabled(False)
        self.instance_selector.setEnabled(False)
        previous_page = after_page or self.current_page_name()

        def apply_selection(result):
            self._set_active_host_id(target)
            server_id = str((result or {}).get("server_id") or "")
            if server_id:
                self._set_active_server_id(server_id)
            save_config(self.cfg)
            self._overview_cache = None
            self._overview_cache_at = 0.0
            self.node_selector.setEnabled(True)
            self._apply_instances(self.instance_rows)
            self.show_notice(f"Now managing node: {host.name}", "success", 3000)
            # Connection is node-scoped. Server pages are only valid when the
            # selected node has a selected server.
            if server_id:
                destination = "dashboard" if previous_page == "report" else previous_page
                if destination in self.pages:
                    self.show_named_page(destination)
                self.refresh_instances(silent=True)
            else:
                self.show_notice(
                    f"{host.name} has no linked Palworld servers. Use Remote Hosts to discover or install one.",
                    "warning", 7000,
                )
                if previous_page == "connection":
                    self.show_named_page("connection")
                elif previous_page == "instances":
                    self.show_named_page("instances")
                else:
                    self.show_named_page("hosts")

        def failed(message):
            self.node_selector.setEnabled(True)
            self._populate_node_selector()
            self._apply_instances(self.instance_rows)
            self.show_notice(f"Switch Node: {message}", "error", 10000)

        selector = getattr(self.manager, "select_host", None)
        if selector is None:
            # Compatibility fallback for non-fleet managers should never be
            # needed when the node selector is visible.
            apply_selection({"host_id": target, "server_id": ""})
            return
        self._run_async(
            f"node-switch-{target}",
            "Switch Node",
            lambda: selector(target),
            apply_selection,
            failed,
            silent=False,
        )

    def _instance_selector_changed(self, index):
        if self._loading_instance_selector or index < 0:
            return
        instance_id = self.instance_selector.itemData(index)
        if instance_id and str(instance_id) != self._active_server_id():
            self.switch_instance(str(instance_id), after_page=self.current_page_name())

    def _selected_instance(self):
        if not hasattr(self, "instances_table"):
            return None
        row = self.instances_table.currentRow()
        rows = getattr(self, "visible_instance_rows", self.instance_rows)
        return rows[row] if 0 <= row < len(rows) else None

    def switch_instance(self, instance_id: str, after_page="dashboard"):
        target = str(instance_id or "").strip()
        if not target:
            return
        self.instance_selector.setEnabled(False)
        self._set_service_action_state(None, loading=True)

        def done(result):
            self._set_active_server_id(target)
            if self._fleet_mode():
                host_id = str((result or {}).get("host_id") or self._active_host_id())
                self._set_active_host_id(host_id)
                self._populate_node_selector()
            save_config(self.cfg)
            self._overview_cache = None
            self._overview_cache_at = 0.0
            self.instance_selector.setEnabled(True)
            self._apply_instances(self.instance_rows)
            self.show_notice(f"Now managing server instance: {target}", "success", 3500)
            self.refresh_instances(silent=True)
            destination = "dashboard" if after_page == "report" else after_page
            if destination and destination in self.pages:
                self.show_named_page(destination)
            else:
                self.show_named_page("dashboard")

        def failed(message):
            self.instance_selector.setEnabled(True)
            self.show_notice(f"Switch Server: {message}", "error", 10000)
            self.refresh_instances(silent=True)

        self._run_async("instance-switch", "Switch Server", lambda: self.manager.select_instance(target), done, failed, silent=False)

    def manage_selected_instance(self):
        row = self._selected_instance()
        if not row:
            self.show_notice("Select a server instance first.", "warning")
            return
        self.switch_instance(str(row.get("id")), after_page="dashboard")

    def setup_selected_instance(self):
        row = self._selected_instance()
        if not row:
            self.show_notice("Select a server instance first.", "warning")
            return
        self.switch_instance(str(row.get("id")), after_page="server_setup")

    def create_instance_from_form(self):
        name = self.instance_name_input.text().strip()
        if not name:
            self.show_notice("Enter a server name before adding an instance.", "warning")
            return
        server = {}
        if self.instance_install_input.text().strip(): server["install_dir"] = self.instance_install_input.text().strip()
        if self.instance_service_input.text().strip(): server["service_name"] = self.instance_service_input.text().strip()
        if self.instance_game_port_input.value() > 0: server["game_port"] = self.instance_game_port_input.value()
        if self.instance_rest_port_input.value() > 0: server["rest_api_port"] = self.instance_rest_port_input.value()
        payload = {"name": name, "server": server}

        def done(result):
            self.show_notice(f"Server instance '{result.get('name') or result.get('id')}' added. Select it and use Server Setup to finish configuration.", "success", 7000)
            for widget in (self.instance_name_input, self.instance_install_input, self.instance_service_input):
                widget.clear()
            self.instance_game_port_input.setValue(0); self.instance_rest_port_input.setValue(0)
            self.refresh_instances(silent=True)

        self._run_action_async("instance-create", "Add Server", lambda: self.manager.create_instance(payload), on_success=done)

    def delete_selected_instance(self):
        row = self._selected_instance()
        if not row:
            self.show_notice("Select a server instance first.", "warning")
            return
        instance_id = str(row.get("id"))
        name = str(row.get("name") or instance_id)

        def execute():
            def done(result):
                self._set_active_server_id(str(result.get("active_instance_id") or self._active_server_id()))
                save_config(self.cfg)
                self._overview_cache = None
                self.show_notice(f"Removed server instance: {name}", "success")
                self.refresh_instances(silent=True, refresh_after=True)
            self._run_action_async("instance-delete", "Remove Server", lambda: self.manager.delete_instance(instance_id), on_success=done)

        self._confirm_then(f"delete-instance-{instance_id}", f"Remove '{name}' from PalServer Manager? The server must already be stopped. This removes the manager configuration only; it does not delete game files.", execute)

    def uninstall_selected_instance(self):
        row = self._selected_instance()
        if not row:
            self.show_notice("Select a server instance first.", "warning")
            return
        instance_id = str(row.get("id"))
        name = str(row.get("name") or instance_id)
        if not hasattr(self.manager, "uninstall_instance"):
            self.show_notice("Full server uninstall is available for agent-managed fleet servers. Use Remove from Manager for configuration-only removal.", "warning", 9000)
            return

        def execute():
            def done(result):
                self._set_active_server_id(str(result.get("active_instance_id") or self._active_server_id()))
                save_config(self.cfg)
                self._overview_cache = None
                backup = result.get("final_backup") or {}
                backup_note = f" Final backup: {backup.get('name')}." if isinstance(backup, dict) and backup.get("name") else ""
                self.show_notice(f"Uninstalled Palworld server '{name}'. Game files and systemd service were removed; shared SteamCMD and manager backups were preserved.{backup_note}", "success", 10000)
                self.refresh_instances(silent=True, refresh_after=True)
                if hasattr(self, "hosts_table"):
                    self.refresh_hosts_page()
            self._run_action_async(f"instance-uninstall-{instance_id}", "Uninstall Palworld Server", lambda: self.manager.uninstall_instance(instance_id), on_success=done)

        self._confirm_then(
            f"uninstall-instance-{instance_id}",
            f"Permanently uninstall '{name}'? PalServer Manager will create a final backup when save data exists, stop/disable the service, delete the Palworld install directory, and unlink the server. Shared SteamCMD and backup files are preserved.",
            execute,
        )

    def rename_selected_instance(self):
        row = self._selected_instance()
        if not row:
            self.show_notice("Select a server first.", "warning")
            return
        name = self.instance_rename_input.text().strip()
        if not name:
            self.show_notice("Enter the new server name first.", "warning")
            return
        instance_id = str(row.get("id"))
        fn = getattr(self.manager, "rename_instance", None)
        if fn is None:
            fn = lambda iid, new_name: self.manager.update_instance(iid, {"name": new_name})

        def done(_result):
            self.instance_rename_input.clear()
            self.show_notice(f"Server {instance_id} renamed to {name}.", "success")
            self.refresh_instances(silent=True, refresh_after=True)

        self._run_action_async(f"instance-rename-{instance_id}", "Rename Server", lambda: fn(instance_id, name), on_success=done)

    def _append_host_provision_log(self, message):
        if not hasattr(self, "host_provision_console"):
            return
        # Strip terminal color/control sequences while keeping command output
        # otherwise intact and readable in the embedded console.
        text = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", str(message or ""))
        for raw in text.replace("\r", "\n").splitlines():
            line = raw.rstrip()
            if not line:
                continue
            stamp = datetime.now().strftime("%H:%M:%S")
            self.host_provision_console.appendPlainText(f"[{stamp}] {line}")
        bar = self.host_provision_console.verticalScrollBar()
        bar.setValue(bar.maximum())

    def refresh_hosts_page(self):
        if not hasattr(self, "hosts_table"):
            return
        selected_id = getattr(self, "selected_host_id", "")
        rows = []
        try:
            rows = self.manager.hosts() if hasattr(self.manager, "hosts") else []
        except Exception as exc:
            self.host_provision_status.setText(f"Unable to load fleet hosts: {exc}")
            self.host_provision_status.setProperty("tone", "error")
        self.host_rows = list(rows or [])
        self.hosts_table.setRowCount(len(self.host_rows))
        for r, row in enumerate(self.host_rows):
            values = (
                row.get("name", "-"), row.get("id", "-"), row.get("ssh_host", "-") or row.get("remote_url", "-"),
                row.get("ssh_user", "-"), row.get("ssh_port", "-"), row.get("agent_port", "-"), row.get("os_type", "linux"),
            )
            for c, value in enumerate(values):
                self.hosts_table.setItem(r, c, QTableWidgetItem(str(value)))
        selected_row = -1
        if selected_id:
            for index, row in enumerate(self.host_rows):
                if str(row.get("id")) == str(selected_id):
                    selected_row = index
                    break
        if selected_row < 0 and self.host_rows:
            selected_row = 0
        if selected_row >= 0:
            self.hosts_table.selectRow(selected_row)
        else:
            self.selected_host_id = ""
            self._on_host_selection_changed()

    def _selected_host_row(self):
        if not hasattr(self, "hosts_table"):
            return None
        row_index = self.hosts_table.currentRow()
        rows = getattr(self, "host_rows", [])
        if row_index < 0 or row_index >= len(rows):
            return None
        return rows[row_index]

    def _on_host_selection_changed(self):
        row = self._selected_host_row()
        self.selected_host_id = str((row or {}).get("id") or "")
        enabled = bool(row)
        for button in getattr(self, "host_control_buttons", []):
            button.setEnabled(enabled)
        if not hasattr(self, "selected_host_label"):
            return
        if not row:
            self.selected_host_label.setText("Select a provisioned host above to manage its agent or install Palworld servers.")
            return
        address = row.get("ssh_host") or row.get("remote_url") or "-"
        self.selected_host_label.setText(
            f"{row.get('name', 'Host')}  •  {row.get('id', '-')}  •  {row.get('ssh_user', '-')}@{address}:{row.get('ssh_port', 22)}  •  agent 127.0.0.1:{row.get('agent_port', 8765)}"
        )

    def _require_selected_host(self):
        row = self._selected_host_row()
        if not row:
            self.show_notice("Select a provisioned Linux host first.", "warning")
            return None
        return row

    def _bootstrapper_for_host_id(self, host_id):
        host = self.cfg.fleet_host(str(host_id))
        if str(host.mode).lower() != "ssh":
            raise ValueError("This host is not configured for SSH management")
        return LinuxHostBootstrapper(host.ssh_host, host.ssh_user, host.ssh_port, host.ssh_key_file)

    def verify_selected_host_agent(self):
        row = self._require_selected_host()
        if not row:
            return
        host_id = str(row.get("id"))
        self.host_provision_status.setText(f"Verifying the PalServer Manager agent on {row.get('name', host_id)}…")
        self.host_provision_status.setProperty("tone", "info")
        self._append_host_provision_log(f"Verifying agent connectivity for {row.get('name', host_id)} ({host_id})...")

        def done(info):
            version = info.get("agent_version") or "unknown"
            hostname = info.get("hostname") or row.get("ssh_host") or host_id
            count = info.get("instances", 0)
            self.host_provision_status.setText(f"Agent online on {hostname}. Version {version}; {count} local instance configuration(s).")
            self.host_provision_status.setProperty("tone", "success")
            self._append_host_provision_log(f"Agent verification successful: {hostname}, version {version}, {count} instance configuration(s).")

        def failed(message):
            self.host_provision_status.setText(f"Agent verification failed: {message}")
            self.host_provision_status.setProperty("tone", "error")
            self._append_host_provision_log(f"ERROR: Agent verification failed for {host_id}: {message}")

        self._run_async(f"host-verify-{host_id}", "Verify Agent", lambda: self.manager.remote_for_host(host_id).host_info(), done, on_error=failed, silent=False)

    def discover_selected_host_servers(self):
        row = self._require_selected_host()
        if not row:
            return
        host_id = str(row.get("id"))
        self.host_provision_status.setText(f"Scanning {row.get('name', host_id)} for Palworld server installations…")
        self.host_provision_status.setProperty("tone", "info")
        self._append_host_provision_log(f"Scanning host {host_id} for existing Palworld installations...")

        def done(discovered):
            discovered = list(discovered or [])
            self._append_host_provision_log(f"Discovery on {host_id} found {len(discovered)} Palworld server installation(s).")
            if discovered:
                self.host_provision_status.setText(f"Found {len(discovered)} Palworld server installation(s). Linking them to the manager…")
                self._adopt_discovered_servers(host_id, discovered, False)
            else:
                self.host_provision_status.setText("No Palworld server installation was found on this host. You can install one now with Install New Palworld Server.")
                self.host_provision_status.setProperty("tone", "warning")

        def failed(message):
            self.host_provision_status.setText(f"Host discovery failed: {message}")
            self.host_provision_status.setProperty("tone", "error")
            self._append_host_provision_log(f"ERROR: Discovery failed on {host_id}: {message}")

        self._run_async(f"host-discover-{host_id}", "Discover Servers", lambda: self.manager.remote_for_host(host_id).host_discover(), done, on_error=failed, silent=False)

    def prepare_install_for_selected_host(self):
        row = self._require_selected_host()
        if not row:
            return
        self.prepare_install_for_host(str(row.get("id")))

    def _build_host_install_dialog(self, host, next_id):
        dialog = QDialog(self)
        dialog.setObjectName("palworldInstallDialog")
        dialog.setWindowTitle(f"Install Palworld Server - {host.name}")
        dialog.setModal(True)
        dialog.resize(760, 420)
        dialog.setMinimumWidth(680)

        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(12)

        self.host_install_title = QLabel(f"Install New Palworld Server on {host.name}")
        self.host_install_title.setObjectName("pageTitle")
        self.host_install_subtitle = QLabel(
            "Review the automatically selected defaults below. Change anything you want, then install and automatically link the new Linux Palworld server to PalServer Manager."
        )
        self.host_install_subtitle.setObjectName("pageSubtitle")
        self.host_install_subtitle.setWordWrap(True)
        layout.addWidget(self.host_install_title)
        layout.addWidget(self.host_install_subtitle)

        form_panel = QFrame()
        form_panel.setObjectName("panel")
        form = QGridLayout(form_panel)
        form.setContentsMargins(16, 14, 16, 14)
        form.setHorizontalSpacing(12)
        form.setVerticalSpacing(10)

        self.new_remote_server_name = QLineEdit(f"Palworld Server {next_id}")
        self.new_remote_server_name.setPlaceholderText("Server name")
        self.new_remote_install_dir = QLineEdit("/opt/palworld")
        self.new_remote_service = QLineEdit("palworld")
        self.new_remote_game_port = QSpinBox(); self.new_remote_game_port.setRange(1, 65535); self.new_remote_game_port.setValue(8211)
        self.new_remote_rest_port = QSpinBox(); self.new_remote_rest_port.setRange(1, 65535); self.new_remote_rest_port.setValue(8212)
        self.new_remote_max_players = QSpinBox(); self.new_remote_max_players.setRange(1, 128); self.new_remote_max_players.setValue(32)

        form.addWidget(QLabel("Server name"), 0, 0); form.addWidget(self.new_remote_server_name, 0, 1)
        form.addWidget(QLabel("Install directory"), 0, 2); form.addWidget(self.new_remote_install_dir, 0, 3)
        form.addWidget(QLabel("Service name"), 1, 0); form.addWidget(self.new_remote_service, 1, 1)
        form.addWidget(QLabel("Game port"), 1, 2); form.addWidget(self.new_remote_game_port, 1, 3)
        form.addWidget(QLabel("REST API port"), 2, 0); form.addWidget(self.new_remote_rest_port, 2, 1)
        form.addWidget(QLabel("Max players"), 2, 2); form.addWidget(self.new_remote_max_players, 2, 3)
        form.setColumnStretch(1, 1); form.setColumnStretch(3, 1)
        layout.addWidget(form_panel)

        self.host_install_status = QLabel("Checking this host for existing Palworld installations and selecting safe defaults...")
        self.host_install_status.setObjectName("noticeBar")
        self.host_install_status.setProperty("tone", "info")
        self.host_install_status.setWordWrap(True)
        layout.addWidget(self.host_install_status)

        actions = QHBoxLayout()
        actions.addStretch(1)
        cancel = QPushButton("Cancel")
        cancel.setObjectName("ghostButton")
        cancel.setProperty("compactAction", True)
        cancel.setMaximumWidth(120)
        cancel.clicked.connect(dialog.reject)
        self.host_install_submit_button = QPushButton("Install Palworld Server")
        self.host_install_submit_button.setObjectName("successButton")
        self.host_install_submit_button.setProperty("compactAction", True)
        self.host_install_submit_button.setMaximumWidth(220)
        self.host_install_submit_button.clicked.connect(self.install_palworld_on_pending_host)
        self.host_install_submit_button.setEnabled(False)
        actions.addWidget(cancel)
        actions.addWidget(self.host_install_submit_button)
        layout.addLayout(actions)

        dialog.finished.connect(lambda _result: self._host_install_dialog_closed(dialog))
        self.host_install_dialog = dialog
        return dialog

    def _host_install_dialog_closed(self, dialog):
        if getattr(self, "host_install_dialog", None) is dialog:
            self.host_install_dialog = None
            self.host_install_submit_button = None

    def _close_host_install_dialog(self):
        dialog = getattr(self, "host_install_dialog", None)
        if dialog is not None:
            dialog.accept()

    def prepare_install_for_host(self, host_id):
        host = self.cfg.fleet_host(str(host_id))
        self.pending_install_host_id = host.id
        next_id = self.cfg.next_fleet_server_id()

        previous = getattr(self, "host_install_dialog", None)
        if previous is not None:
            previous.reject()
        dialog = self._build_host_install_dialog(host, next_id)
        dialog.open()

        self.host_provision_status.setText(f"Preparing a new Palworld server installation for {host.name}...")
        self.host_provision_status.setProperty("tone", "info")
        self._append_host_provision_log(f"Preparing new Palworld server defaults for {host.name} ({host.id}).")

        def work():
            remote = self.manager.remote_for_host(host.id)
            return {"info": remote.host_info(), "discovered": remote.host_discover()}

        def done(result):
            if getattr(self, "pending_install_host_id", "") != host.id:
                return
            if getattr(self, "host_install_dialog", None) is not dialog or not dialog.isVisible():
                return
            info = dict((result or {}).get("info") or {})
            agent_version = str(info.get("agent_version") or "0.0.0")
            if version_key(agent_version) < version_key("0.4.9"):
                if self.host_install_status is not None:
                    self.host_install_status.setText(f"This host is running agent {agent_version}. Update the agent to 0.4.9 or newer before installing Palworld so live installation progress streaming is available.")
                    self.host_install_status.setProperty("tone", "warning")
                    self.host_install_status.style().unpolish(self.host_install_status); self.host_install_status.style().polish(self.host_install_status)
                self.host_provision_status.setText(f"Agent update required on {host.name}: current version {agent_version}, required 0.4.9 or newer.")
                self.host_provision_status.setProperty("tone", "warning")
                self._append_host_provision_log(f"Agent {agent_version} detected on {host.name}. Update Agent is required before installing a new Palworld server with live progress.")
                return
            rows = list((result or {}).get("discovered") or [])
            if self.host_install_submit_button is not None:
                self.host_install_submit_button.setEnabled(True)
            used_game = {int(r.get("game_port") or 0) for r in rows}
            used_rest = {int(r.get("rest_api_port") or 0) for r in rows}
            used_dirs = {str(r.get("install_dir") or "") for r in rows}
            used_services = {str(r.get("service_name") or "") for r in rows}
            game_port = 8211
            rest_port = 8212
            while game_port in used_game or game_port in used_rest or rest_port in used_game or rest_port in used_rest:
                game_port += 10
                rest_port += 10
            install_dir = "/opt/palworld" if "/opt/palworld" not in used_dirs else f"/opt/palworld-{next_id}"
            service_name = "palworld" if "palworld" not in used_services else f"palworld-{next_id}"
            self.new_remote_install_dir.setText(install_dir)
            self.new_remote_service.setText(service_name)
            self.new_remote_game_port.setValue(game_port)
            self.new_remote_rest_port.setValue(rest_port)
            if self.host_install_status is not None:
                self.host_install_status.setText(f"Defaults ready. Found {len(rows)} existing Palworld installation(s) on this host. Ports, paths, and service names only need to be unique on this physical host; other remote hosts may reuse the same values.")
                self.host_install_status.setProperty("tone", "success")
                self.host_install_status.style().unpolish(self.host_install_status); self.host_install_status.style().polish(self.host_install_status)
            self.host_provision_status.setText(f"New-server defaults are ready for {host.name}. Found {len(rows)} existing Palworld installation(s) on the host.")
            self.host_provision_status.setProperty("tone", "success")
            self._append_host_provision_log(f"Install defaults ready: {install_dir}, service {service_name}, game UDP {game_port}, REST {rest_port}.")

        def failed(message):
            if getattr(self, "host_install_dialog", None) is dialog and dialog.isVisible() and self.host_install_status is not None:
                self.host_install_status.setText(f"Could not verify this host before installation: {message}. Verify or update the agent, then reopen the installer.")
                self.host_install_status.setProperty("tone", "warning")
                self.host_install_status.style().unpolish(self.host_install_status); self.host_install_status.style().polish(self.host_install_status)
            self.host_provision_status.setText(f"Could not verify the host before setup: {message}. Verify or update the agent, then retry.")
            self.host_provision_status.setProperty("tone", "warning")
            self._append_host_provision_log(f"WARNING: Could not inspect host before preparing install defaults: {message}")

        self._run_async(f"host-install-prep-{host.id}", "Prepare Palworld Install", work, done, on_error=failed, silent=True)

    def update_selected_host_agent(self):
        row = self._require_selected_host()
        if not row:
            return
        host_id = str(row.get("id"))
        try:
            bootstrapper = self._bootstrapper_for_host_id(host_id)
        except Exception as exc:
            self.show_notice(f"Update Agent: {exc}", "error")
            return
        progress_signal = self.host_provision_log_signals.line
        self.host_provision_status.setText(f"Updating the PalServer Manager agent on {row.get('name', host_id)}…")
        self.host_provision_status.setProperty("tone", "info")
        self._append_host_provision_log(f"Agent update requested for {row.get('name', host_id)}. Linux package upgrade is disabled for this action.")

        def work():
            result = bootstrapper.install_agent(update_os=False, progress=progress_signal.emit)
            host_cfg = self.cfg.fleet_host(host_id)
            if result.get("agent_token"):
                host_cfg.agent_token = str(result["agent_token"])
                save_config(self.cfg)

            # The upgrade intentionally restarts palserver-manager-agent.  Any
            # HTTP request/tunnel that existed before that restart can be aborted
            # by Windows with WSAECONNABORTED (10053).  Reconnect through a new
            # tunnel and retry the read-only host-info request instead of
            # reporting a successful installation as a failure.
            reconnect_warning = ""
            info = {}
            try:
                if hasattr(self.manager, "wait_for_host_info"):
                    def retry_notice(attempt, attempts, exc):
                        progress_signal.emit(
                            f"Agent restart interrupted the previous management connection; "
                            f"reconnecting ({attempt + 1}/{attempts})..."
                        )
                    info = self.manager.wait_for_host_info(
                        host_id, attempts=8, delay=0.75, on_retry=retry_notice
                    )
                else:
                    if hasattr(self.manager, "reset_host_connection"):
                        self.manager.reset_host_connection(host_id)
                    info = self.manager.remote_for_host(host_id).host_info()
            except Exception as exc:
                reconnect_warning = str(exc)
            return {"bootstrap": result, "info": info, "reconnect_warning": reconnect_warning}

        def done(result):
            version = (result.get("info") or {}).get("agent_version") or (result.get("bootstrap") or {}).get("agent_version") or "unknown"
            warning = str(result.get("reconnect_warning") or "").strip()
            if warning:
                self.host_provision_status.setText(
                    f"Agent update installed successfully on {row.get('name', host_id)} (version {version}), "
                    "but the manager could not immediately reconnect. Use Verify Agent to recheck connectivity."
                )
                self.host_provision_status.setProperty("tone", "warning")
                self._append_host_provision_log(
                    f"WARNING: Agent {version} was installed successfully on {host_id}, but post-restart API verification "
                    f"did not reconnect yet: {warning}"
                )
            else:
                self.host_provision_status.setText(f"Agent update completed successfully. {row.get('name', host_id)} is online with agent version {version}.")
                self.host_provision_status.setProperty("tone", "success")
                self._append_host_provision_log(f"Agent update complete on {host_id}; verified version {version} after reconnect.")

        def failed(message):
            self.host_provision_status.setText(f"Agent update failed: {message}")
            self.host_provision_status.setProperty("tone", "error")
            self._append_host_provision_log(f"ERROR: Agent update failed on {host_id}: {message}")

        self._run_async(f"host-update-agent-{host_id}", "Update Agent", work, done, on_error=failed, silent=False)

    def update_selected_host_linux(self):
        row = self._require_selected_host()
        if not row:
            return
        host_id = str(row.get("id"))
        try:
            bootstrapper = self._bootstrapper_for_host_id(host_id)
        except Exception as exc:
            self.show_notice(f"Update Linux: {exc}", "error")
            return
        progress_signal = self.host_provision_log_signals.line
        self.host_provision_status.setText(f"Updating Debian/Ubuntu packages on {row.get('name', host_id)}. The host will not be rebooted automatically…")
        self.host_provision_status.setProperty("tone", "info")
        self._append_host_provision_log(f"Linux package update requested for {row.get('name', host_id)} ({host_id}).")

        def done(result):
            reboot = bool((result or {}).get("reboot_required"))
            suffix = " A reboot is recommended." if reboot else " No reboot is currently reported as required."
            self.host_provision_status.setText(f"Linux package update completed on {row.get('name', host_id)}.{suffix}")
            self.host_provision_status.setProperty("tone", "warning" if reboot else "success")
            self._append_host_provision_log(f"Linux package update completed on {host_id}.{suffix}")

        def failed(message):
            self.host_provision_status.setText(f"Linux update failed: {message}")
            self.host_provision_status.setProperty("tone", "error")
            self._append_host_provision_log(f"ERROR: Linux update failed on {host_id}: {message}")

        self._run_async(f"host-update-linux-{host_id}", "Update Linux", lambda: bootstrapper.update_linux(progress=progress_signal.emit), done, on_error=failed, silent=False)

    def restart_selected_host_agent(self):
        row = self._require_selected_host()
        if not row:
            return
        host_id = str(row.get("id"))
        try:
            bootstrapper = self._bootstrapper_for_host_id(host_id)
        except Exception as exc:
            self.show_notice(f"Restart Agent: {exc}", "error")
            return
        progress_signal = self.host_provision_log_signals.line
        self.host_provision_status.setText(f"Restarting the PalServer Manager agent on {row.get('name', host_id)}…")
        self.host_provision_status.setProperty("tone", "info")
        self._append_host_provision_log(f"Restarting agent service on {row.get('name', host_id)} ({host_id})...")

        def work():
            result = bootstrapper.restart_agent(progress=progress_signal.emit)
            if hasattr(self.manager, "reset_host_connection"):
                self.manager.reset_host_connection(host_id)
            info = self.manager.remote_for_host(host_id).host_info()
            return {"restart": result, "info": info}

        def done(result):
            version = (result.get("info") or {}).get("agent_version") or "unknown"
            self.host_provision_status.setText(f"Agent restarted successfully on {row.get('name', host_id)}. Version {version} is responding.")
            self.host_provision_status.setProperty("tone", "success")
            self._append_host_provision_log(f"Agent restart complete on {host_id}; API verification succeeded with version {version}.")

        def failed(message):
            self.host_provision_status.setText(f"Agent restart failed: {message}")
            self.host_provision_status.setProperty("tone", "error")
            self._append_host_provision_log(f"ERROR: Agent restart failed on {host_id}: {message}")

        self._run_async(f"host-restart-agent-{host_id}", "Restart Agent", work, done, on_error=failed, silent=False)

    def uninstall_selected_host_agent(self):
        row = self._require_selected_host()
        if not row:
            return
        host_id = str(row.get("id"))
        host_name = str(row.get("name") or host_id)
        linked = [ref for ref in getattr(self.cfg, "fleet_servers", []) if str(ref.host_id) == host_id]
        if linked:
            labels = ", ".join(f"{ref.id} {ref.name}" for ref in linked)
            self.show_notice(
                f"Uninstall Agent blocked: {host_name} still has managed server(s): {labels}. Remove or uninstall those server instances first. The agent uninstall never deletes Palworld game servers automatically.",
                "warning", 12000,
            )
            return
        try:
            bootstrapper = self._bootstrapper_for_host_id(host_id)
        except Exception as exc:
            self.show_notice(f"Uninstall Agent: {exc}", "error")
            return

        def execute():
            progress_signal = self.host_provision_log_signals.line
            self.host_provision_status.setText(f"Uninstalling the PalServer Manager agent from {host_name}…")
            self.host_provision_status.setProperty("tone", "warning")
            self._append_host_provision_log(f"Agent uninstall requested for {host_name} ({host_id}). Palworld server files will not be removed.")

            def work():
                result = bootstrapper.uninstall_agent(progress=progress_signal.emit)
                removed = self.manager.remove_host(host_id)
                return {"agent": result, "host": removed}

            def done(_result):
                self.selected_host_id = ""
                self.host_provision_status.setText(f"PalServer Manager agent was removed from {host_name}. Palworld server files, backups, SSH access, and the Linux user were preserved.")
                self.host_provision_status.setProperty("tone", "success")
                self._append_host_provision_log(f"Agent uninstall completed on {host_name}; host removed from the manager fleet catalog.")
                self.refresh_hosts_page()
                self.show_notice(f"Agent uninstalled from {host_name}.", "success", 9000)

            def failed(message):
                self.host_provision_status.setText(f"Agent uninstall failed: {message}")
                self.host_provision_status.setProperty("tone", "error")
                self._append_host_provision_log(f"ERROR: Agent uninstall failed on {host_name}: {message}")
                self.show_notice(f"Uninstall Agent: {message}", "error", 12000)

            self._run_async(f"host-uninstall-agent-{host_id}", "Uninstall Agent", work, done, on_error=failed, silent=False)

        self._confirm_then(
            f"uninstall-agent-{host_id}",
            f"Uninstall the PalServer Manager agent from '{host_name}'? This removes the agent service and agent files only. Palworld servers, backups, SSH access, and the Linux account are preserved.",
            execute,
        )

    def _bootstrapper_from_form(self):
        return LinuxHostBootstrapper(
            self.host_address_input.text().strip(),
            self.host_user_input.text().strip(),
            self.host_port_input.value(),
            self.host_key_input.text().strip(),
        )

    def test_new_host_ssh(self):
        try:
            bootstrapper = self._bootstrapper_from_form()
        except Exception as exc:
            self.show_notice(f"SSH: {exc}", "error")
            return
        self.host_provision_status.setText("Testing SSH connection…")
        self.host_provision_status.setProperty("tone", "info")
        self._append_host_provision_log(f"Testing SSH key authentication to {bootstrapper.target}:{bootstrapper.port}...")

        def done(info):
            self.host_provision_status.setText(f"SSH connection successful: {info.get('hostname')} ({info.get('os')}). Root/passwordless sudo access is available.")
            self.host_provision_status.setProperty("tone", "success")
            self._append_host_provision_log(f"SSH test successful. Connected to {info.get('hostname')} with non-interactive sudo access.")
            self.show_notice("SSH connection successful.", "success")

        def failed(message):
            self.host_provision_status.setText(f"SSH test failed: {message}")
            self.host_provision_status.setProperty("tone", "error")
            self._append_host_provision_log(f"ERROR: SSH test failed: {message}")
            self.show_notice(f"SSH Test: {message}", "error", 10000)

        self._run_async("host-ssh-test", "SSH Test", bootstrapper.test_connection, done, on_error=failed, silent=False)

    def provision_new_host(self):
        if not hasattr(self.manager, "register_host"):
            self.show_notice("Remote-host provisioning requires fleet mode. Connect this manager to its primary agent first.", "error")
            return
        try:
            bootstrapper = self._bootstrapper_from_form()
        except Exception as exc:
            self.show_notice(f"Provision Host: {exc}", "error")
            return
        display_name = self.host_name_input.text().strip()
        address = self.host_address_input.text().strip()
        user = self.host_user_input.text().strip()
        port = self.host_port_input.value()
        key = self.host_key_input.text().strip()
        update_os = self.host_update_os.isChecked()
        self.host_provision_status.setText("Connecting to the Linux host and installing/upgrading the PalServer Manager agent. This can take several minutes when operating-system updates are enabled…")
        self.host_provision_status.setProperty("tone", "info")
        self.host_provision_console.clear()
        self._append_host_provision_log(f"Provision request started for {user}@{address}:{port}.")
        progress_signal = self.host_provision_log_signals.line

        def work():
            result = bootstrapper.install_agent(update_os=update_os, progress=progress_signal.emit)
            progress_signal.emit("Registering the new Linux host with PalServer Manager...")
            host_id = self.cfg.next_fleet_host_id()
            host = FleetHostConfig(
                id=host_id,
                name=display_name or str(result.get("hostname") or address),
                mode="ssh",
                ssh_host=address,
                ssh_port=port,
                ssh_user=user,
                ssh_key_file=key,
                local_tunnel_port=find_free_local_port(),
                remote_agent_port=int(result.get("agent_port") or 8765),
                agent_token=str(result.get("agent_token") or ""),
                enabled=True,
                os_type="linux",
            )
            self.manager.register_host(host)
            progress_signal.emit(f"Host registered as {host.id}: {host.name}.")
            progress_signal.emit("Opening the private SSH tunnel and verifying the remote agent...")
            remote = self.manager.remote_for_host(host_id)
            host_info = remote.host_info()
            progress_signal.emit("Remote agent verified. Scanning the host for existing Palworld server installations...")
            discovered = remote.host_discover()
            progress_signal.emit(f"Discovery complete: {len(discovered or [])} existing Palworld server(s) found.")
            return {"host": host, "info": host_info, "discovered": discovered, "bootstrap": result}

        def done(result):
            host = result["host"]
            self.pending_install_host_id = host.id
            discovered = list(result.get("discovered") or [])
            reboot = bool((result.get("bootstrap") or {}).get("reboot_required"))
            if discovered:
                self.host_provision_status.setText(f"Agent linked on {host.name}. Found {len(discovered)} existing Palworld server(s); registering them now…")
                self._adopt_discovered_servers(host.id, discovered, reboot)
            else:
                suffix = " A Linux reboot is recommended after the package update." if reboot else ""
                self.host_provision_status.setText(f"Agent installed and linked on {host.name}. No existing Palworld server was detected.{suffix}")
                self.host_provision_status.setProperty("tone", "warning")
                self.refresh_hosts_page()
                self.prepare_install_for_host(host.id)

        def failed(message):
            self.host_provision_status.setText(f"Provisioning failed: {message}")
            self.host_provision_status.setProperty("tone", "error")
            self._append_host_provision_log(f"ERROR: Provisioning failed: {message}")
            self.show_notice(f"Provision Linux Host: {message}", "error", 12000)

        self.show_notice("Provision Linux Host in progress…", "info", 0)

        def completed(result):
            self.notice_bar.hide()
            done(result)

        self._run_async("host-provision", "Provision Linux Host", work, completed, on_error=failed, silent=False)

    def _adopt_discovered_servers(self, host_id, discovered, reboot_required=False):
        def work():
            remote = self.manager.remote_for_host(host_id)
            linked = []
            restart_required = False
            already_managed = 0
            repaired_duplicates = 0
            remote_rows = {str(row.get("id")): row for row in remote.instances(False)}
            for found in discovered:
                global_id = self.cfg.next_fleet_server_id()
                preferred_name = str(found.get("name") or f"Palworld Server {global_id}")
                managed_id = str(found.get("managed_instance_id") or "")
                if managed_id and managed_id in remote_rows:
                    adopted = dict(remote_rows[managed_id])
                    adopted["already_managed"] = True
                    already_managed += 1
                else:
                    payload = {
                        "id": global_id,
                        "name": preferred_name,
                        "server": {
                            "install_dir": found.get("install_dir"),
                            "config_path": found.get("config_path"),
                            "save_dir": found.get("save_dir"),
                            "service_name": found.get("service_name") or "palworld",
                            "game_port": int(found.get("game_port") or 8211),
                            "rest_api_port": int(found.get("rest_api_port") or 8212),
                        },
                    }
                    adopted = remote.host_adopt(payload)
                    if adopted.get("already_managed"):
                        already_managed += 1
                    repaired_duplicates += len(adopted.get("repaired_duplicate_ids") or [])
                    restart_required = restart_required or bool(adopted.get("restart_required"))
                linked.append(self.manager.link_remote_instance(host_id, adopted, preferred_name))
            return {
                "linked": linked,
                "restart_required": restart_required,
                "already_managed": already_managed,
                "repaired_duplicates": repaired_duplicates,
            }

        def done(result):
            linked = result.get("linked", [])
            suffix = " The host reports that a reboot is recommended for Linux package updates." if reboot_required else ""
            if result.get("restart_required"):
                suffix += " One or more existing Palworld servers were already running; restart them from the manager once convenient so the private REST management settings become active."
            already_managed = int(result.get("already_managed") or 0)
            repaired = int(result.get("repaired_duplicates") or 0)
            detail = ""
            if already_managed:
                detail += f" {already_managed} server(s) were already registered with the host agent and were linked without re-adopting them."
            if repaired:
                detail += f" Repaired {repaired} stale duplicate agent registration(s) left by an older failed discovery/adoption attempt."
            self.host_provision_status.setText(f"Linked {len(linked)} existing Palworld server(s) to this manager.{detail}{suffix}")
            self.host_provision_status.setProperty("tone", "success")
            self._close_host_install_dialog()
            self.refresh_hosts_page(); self.refresh_instances(silent=True)
            self.show_notice(f"Linked {len(linked)} server(s) from the new host.", "success", 7000)

        self._run_async(f"host-adopt-{host_id}", "Link Existing Servers", work, done, silent=False)

    def install_palworld_on_pending_host(self):
        host_id = getattr(self, "pending_install_host_id", "")
        if not host_id:
            self.show_notice("Select a provisioned remote host and choose Install New Palworld Server first.", "warning")
            return
        name = self.new_remote_server_name.text().strip()
        if not name:
            self.show_notice("Enter a server name before installation.", "warning")
            return
        global_id = self.cfg.next_fleet_server_id()
        payload = {
            "id": global_id,
            "name": name,
            "install_dir": self.new_remote_install_dir.text().strip() or "/opt/palworld",
            "service_name": self.new_remote_service.text().strip() or "palworld",
            "game_port": self.new_remote_game_port.value(),
            "rest_api_port": self.new_remote_rest_port.value(),
            "max_players": self.new_remote_max_players.value(),
            "replace_bootstrap_placeholder": not any(
                str(ref.host_id) == str(host_id) for ref in getattr(self.cfg, "fleet_servers", [])
            ),
        }
        try:
            host_name = self.cfg.fleet_host(host_id).name
        except Exception:
            host_name = host_id

        # The modal's job is only to collect/validate installation options. As
        # soon as the user starts installation, get it out of the way and make
        # the persistent provisioning console the primary progress surface.
        self._close_host_install_dialog()
        self.host_provision_status.setText(
            f"Installing Palworld on {host_name}. Live command output is streaming in the console below…"
        )
        self.host_provision_status.setProperty("tone", "info")
        self._append_host_provision_log(
            f"Starting Palworld installation on {host_name} ({host_id}): {payload['install_dir']}, "
            f"service {payload['service_name']}, game UDP {payload['game_port']}, REST {payload['rest_api_port']}."
        )
        self._append_host_provision_log("Submitting installation job to the remote agent...")
        if hasattr(self, "hosts_scroll") and hasattr(self, "host_provision_console"):
            QTimer.singleShot(0, lambda: self.hosts_scroll.ensureWidgetVisible(self.host_provision_console, 20, 80))
            self.host_provision_console.setFocus(Qt.OtherFocusReason)

        def started(result):
            job_id = str((result or {}).get("job_id") or "")
            if not job_id:
                self._palworld_install_start_failed(host_id, host_name, "Remote agent did not return an installation job ID")
                return
            self._append_host_provision_log(f"Remote installation job {job_id[:8]} started. Streaming live output...")
            jobs = getattr(self, "host_install_jobs", None)
            if jobs is None:
                self.host_install_jobs = {}
                jobs = self.host_install_jobs
            jobs[host_id] = {
                "job_id": job_id,
                "offset": 0,
                "name": name,
                "host_name": host_name,
                "poll_errors": 0,
            }
            self._poll_palworld_install_job(host_id)

        def failed(message):
            self._palworld_install_start_failed(host_id, host_name, message)

        self._run_async(
            f"host-install-start-{host_id}",
            "Start Palworld Install",
            lambda: self.manager.remote_for_host(host_id).host_install_palworld_start(payload),
            started,
            on_error=failed,
            silent=True,
        )

    def _palworld_install_start_failed(self, host_id, host_name, message):
        self.host_provision_status.setText(f"Could not start Palworld installation: {message}")
        self.host_provision_status.setProperty("tone", "error")
        self._append_host_provision_log(f"ERROR: Could not start Palworld installation on {host_name}: {message}")
        if "404" in str(message) or "install-palworld/start" in str(message):
            self._append_host_provision_log("The remote agent does not support streamed installs. Update this host's agent to 0.4.9 or newer and retry.")
        self.show_notice(f"Install Palworld Server: {message}", "error", 12000)

    def _poll_palworld_install_job(self, host_id):
        jobs = getattr(self, "host_install_jobs", {})
        state = jobs.get(str(host_id)) or jobs.get(host_id)
        if not state:
            return
        job_id = str(state.get("job_id") or "")
        offset = int(state.get("offset") or 0)
        if not job_id:
            return
        key = f"host-install-poll-{host_id}"

        def done(snapshot):
            current = getattr(self, "host_install_jobs", {}).get(host_id)
            if not current or str(current.get("job_id")) != job_id:
                return
            for line in list((snapshot or {}).get("lines") or []):
                self._append_host_provision_log(line)
            current["offset"] = int((snapshot or {}).get("next_offset") or current.get("offset") or 0)
            current["poll_errors"] = 0
            status = str((snapshot or {}).get("status") or "running").lower()
            if status == "running":
                self.host_provision_status.setText(
                    f"Installing Palworld on {current.get('host_name', host_id)}. Live output is streaming below…"
                )
                QTimer.singleShot(650, lambda: self._poll_palworld_install_job(host_id))
                return
            if status == "completed":
                installed = dict((snapshot or {}).get("result") or {})
                try:
                    linked = self.manager.link_remote_instance(host_id, installed, current.get("name", ""))
                except Exception as exc:
                    self.host_provision_status.setText(f"Palworld installed, but linking it to the manager failed: {exc}")
                    self.host_provision_status.setProperty("tone", "error")
                    self._append_host_provision_log(f"ERROR: Palworld installed successfully but manager linking failed: {exc}")
                    self.show_notice(f"Palworld installed but linking failed: {exc}", "error", 12000)
                else:
                    self.host_provision_status.setText(
                        f"Palworld server installed successfully and linked as instance {linked.get('id')}: {linked.get('name')}."
                    )
                    self.host_provision_status.setProperty("tone", "success")
                    self._append_host_provision_log(
                        f"Palworld installation completed successfully and linked as manager instance {linked.get('id')}: {linked.get('name')}."
                    )
                    self.refresh_hosts_page(); self.refresh_instances(silent=True)
                    self.show_notice(f"New remote server {linked.get('id')} is ready to manage.", "success", 8000)
                self.host_install_jobs.pop(host_id, None)
                return
            message = str((snapshot or {}).get("error") or "Remote installation failed")
            self.host_provision_status.setText(f"Palworld server installation failed: {message}")
            self.host_provision_status.setProperty("tone", "error")
            self._append_host_provision_log(f"ERROR: Palworld installation failed on {current.get('host_name', host_id)}: {message}")
            self.show_notice(f"Install Palworld Server: {message}", "error", 12000)
            self.host_install_jobs.pop(host_id, None)

        def failed(message):
            current = getattr(self, "host_install_jobs", {}).get(host_id)
            if not current or str(current.get("job_id")) != job_id:
                return
            current["poll_errors"] = int(current.get("poll_errors") or 0) + 1
            retries = current["poll_errors"]
            if retries <= 10:
                if retries in {1, 4, 8}:
                    self._append_host_provision_log(
                        f"WARNING: Live install status check failed ({retries}/10): {message}. Retrying without interrupting the remote install..."
                    )
                QTimer.singleShot(1000, lambda: self._poll_palworld_install_job(host_id))
                return
            self.host_provision_status.setText(
                "The remote installation may still be running, but live status polling was interrupted. Verify the agent and discover servers after it finishes."
            )
            self.host_provision_status.setProperty("tone", "warning")
            self._append_host_provision_log(
                f"ERROR: Live installation polling stopped after repeated connection failures: {message}"
            )
            self.host_install_jobs.pop(host_id, None)

        started = self._run_async(
            key,
            "Palworld Install Progress",
            lambda: self.manager.remote_for_host(host_id).host_install_palworld_job(job_id, offset),
            done,
            on_error=failed,
            silent=True,
        )
        if not started:
            QTimer.singleShot(250, lambda: self._poll_palworld_install_job(host_id))

    def _set_service_action_state(self, service_state=None, loading=False):
        if not hasattr(self, "action_buttons"):
            return
        if loading or service_state is None:
            self.action_buttons.get("start", QPushButton()).setEnabled(False)
            self.action_buttons.get("stop", QPushButton()).setEnabled(False)
            self.action_buttons.get("restart", QPushButton()).setEnabled(False)
            return
        running = str(service_state).lower() in {"active", "running"}
        if "start" in self.action_buttons: self.action_buttons["start"].setEnabled(not running)
        if "stop" in self.action_buttons: self.action_buttons["stop"].setEnabled(running)
        if "restart" in self.action_buttons: self.action_buttons["restart"].setEnabled(running)

    def _apply_header_status(self, status):
        status = status or {}
        host = status.get("host", {}) or {}
        lan_ip = self._display(status.get("lan_ip"))
        host_name = host.get("hostname") or lan_ip
        mode = str(self._current_connection_mode() or "local").upper()
        self._set_connection_state(True, mode, host_name)
        current = self._display(status.get("current_players"), "0")
        maximum = self._display(status.get("max_players"), "0")
        self.player_count_header.setText(f"Players  {current} / {maximum}")
        service = status.get("service", {}) or {}
        self._set_service_action_state(service.get("state"))

    def refresh_header_status(self, silent=True):
        if self._fleet_mode() and not self._server_context_available():
            host = self._active_host_config()
            remote_for_host = getattr(self.manager, "remote_for_host", None)
            if host is None or remote_for_host is None:
                self._set_connection_state(False)
                self.player_count_header.setText("Players  - / -")
                return

            def apply_node(info):
                hostname = str((info or {}).get("hostname") or host.ssh_host or host.name)
                self._set_connection_state(True, str(host.mode or "ssh").upper(), hostname)
                self.player_count_header.setText("Players  - / -")
                self._set_service_action_state(None, loading=True)

            self._run_async(
                f"header-node-{host.id}",
                "Node Status",
                lambda: remote_for_host(host.id).host_info(),
                apply_node,
                on_error=lambda _message: self._set_connection_state(False, str(host.mode or "ssh").upper(), host.name),
                silent=silent,
            )
            return
        self._run_async(
            "header-status",
            "Header Status",
            lambda: self.manager.status(False),
            self._apply_header_status,
            silent=silent,
        )

    def refresh_dashboard(self, silent=False):
        if self._fleet_mode() and not self._server_context_available():
            self.show_named_page("hosts")
            return
        def failed(message):
            self._set_connection_state(False)
            self.player_count_header.setText("Players  - / -")
            if not silent:
                self.show_notice(f"Dashboard: {message}", "error", 10000)

        self._run_async(
            "overview",
            "Dashboard",
            self.manager.overview,
            self._apply_overview,
            failed,
            silent=silent,
        )

    def _apply_overview(self, data):
        if not isinstance(data, dict):
            return
        self._overview_cache = data
        self._overview_cache_at = time.time()
        status = data.get("status", {}) or {}
        health = data.get("health", {}) or {}
        players = data.get("players", []) or []
        logs = data.get("logs", []) or []
        scheduler = data.get("scheduler", {}) or {}
        backups = data.get("backups", []) or []

        service=status.get("service",{}); build=status.get("build",{}) or {}; host=status.get("host",{}) or {}; mode=str(self._current_connection_mode() or "local").upper(); lan_ip=self._display(status.get("lan_ip")); host_name=host.get("hostname") or lan_ip
        self._set_connection_state(True, mode, host_name)
        header_current = self._display(status.get("current_players"), "0"); header_max = self._display(status.get("max_players"), "0")
        self.player_count_header.setText(f"Players  {header_current} / {header_max}")
        self.server_name.setText(self._display(status.get("server_name"),"Palworld Server")); self.host_value.setText(str(host_name)); self.host_detail.setText(f"{host.get('os','Linux')} {host.get('os_release','')}".strip()); game_port=self._display(status.get("game_port")); self.port_value.setText(f"{game_port} / UDP"); self.port_detail.setText(f"LAN: {lan_ip}"); self.uptime_value.setText(self._format_uptime(status.get("server_uptime"))); self.uptime_detail.setText("Current session")
        service_state=str(service.get("state","unknown")).lower(); service_good=service_state in {"active","running"}; self._set_service_action_state(service_state); self.service_badge.set_state("ACTIVE" if service_good else service_state.upper(),"good" if service_good else "bad"); overall=str(health.get("overall","unknown")).lower(); health_tone="good" if overall=="healthy" else ("warning" if overall=="warning" else "bad"); self.health_badge.set_state(overall.upper(),health_tone)
        current=self._display(status.get("current_players"),"0"); maximum=self._display(status.get("max_players"),"0"); self.cards["Players"].set_data(f"{current} / {maximum}","Live from REST API")
        fps=self._display(status.get("server_fps")); avg=self._display(status.get("server_fps_average"));
        if avg!="-":
            try: avg=f"{float(avg):.1f} avg"
            except Exception: avg=f"{avg} avg"
        else: avg="Live server performance"
        self.cards["FPS"].set_data(str(fps),avg); world_day=self._display(status.get("world_days")); base_camps=self._display(status.get("base_camps"),"0"); self.cards["World"].set_data(f"Day {world_day}",f"{base_camps} base camps")
        version=self._display(status.get("game_version"),"Unavailable"); build_state=str(build.get("state","unknown")).lower(); version_badge="Up to date" if build_state=="current" else ("Update available" if build_state=="available" else ""); self.cards["Version"].set_data(version,f"Build {build.get('installed') or '-'}",badge_text=version_badge,badge_good=build_state=="current")
        cpu=float(health.get("cpu_percent",0) or 0); ram=float(health.get("memory_percent",0) or 0); disk=float(health.get("disk_percent",0) or 0); self.cards["CPU_RAM"].set_data(f"{cpu:.0f}% CPU",f"{ram:.0f}% RAM",max(cpu,ram)); self.cards["Storage"].set_data(f"{disk:.1f}% Used",f"{human_bytes(health.get('disk_used'))} / {human_bytes(health.get('disk_total'))}",disk)
        checks={str(row.get("name")):row for row in health.get("checks",[])}; self.health_summary.setText("Healthy" if overall=="healthy" else overall.title())
        if overall=="healthy": self.health_summary.setStyleSheet("color:#55ee9a; font-size:20px; font-weight:800;"); self.health_caption.setText("All systems operating normally")
        elif overall=="warning": self.health_summary.setStyleSheet("color:#ffc75a; font-size:20px; font-weight:800;"); self.health_caption.setText("One or more checks need attention")
        else: self.health_summary.setStyleSheet("color:#ff707b; font-size:20px; font-weight:800;"); self.health_caption.setText("Critical server condition detected")
        for source_key,ui_key in (("CPU","CPU Usage"),("RAM","Memory Usage"),("Disk","Disk Usage"),("Service","Palworld Service"),("Game Port","Game Port (8211)"),("Backup","Backups")):
            check=checks.get(source_key,{}); value=check.get("value","-"); unit=check.get("unit","");
            if unit=="%": value=f"{value}%"
            elif source_key=="Backup" and isinstance(value,(int,float)): value=f"{self._format_age(value)} old"
            self._set_health_row(ui_key,value,str(check.get("state","warning")))
        rest_online=status.get("current_players") is not None or status.get("server_fps") is not None; self._set_health_row("REST API (8212)","Online" if rest_online else "Unavailable","healthy" if rest_online else "warning")

        self._apply_player_rows(players)
        self._apply_ban_rows(data.get("bans", []) or [])
        self._refresh_automation_dashboard(checks.get("Backup",{}), scheduler)
        self._apply_logs(logs)

        # Reuse the same overview payload for any currently-visible telemetry
        # page instead of immediately performing another remote request.
        page = self.current_page_name()
        if page == "watchdog":
            self._apply_watchdog_overview(data)
        elif page == "health":
            self._apply_health_data(health, status)

        if time.time()-self.last_update_check>=600:
            QTimer.singleShot(250,self.refresh_update_card)

    def _player_values(self, row):
        user_id = str(row.get("userId", "") or row.get("playerId", "")).strip()
        platform = str(row.get("platform") or platform_from_user_id(user_id))
        ping = row.get("ping")
        try:
            ping_text = f"{float(ping):.0f} ms" if ping not in (None, "") else "-"
        except Exception:
            ping_text = str(ping or "-")
        locx, locy = row.get("location_x"), row.get("location_y")
        location = "-"
        if locx is not None and locy is not None:
            try:
                location = f"({int(float(locx))}, {int(float(locy))})"
            except Exception:
                location = f"({locx}, {locy})"
        return (
            row.get("name") or row.get("accountName") or "Unknown",
            platform,
            row.get("level", "-"),
            ping_text,
            row.get("accountName") or "-",
            user_id or "-",
            row.get("iP") or row.get("ip") or "-",
            location,
        )

    @staticmethod
    def _platform_color(platform):
        value = str(platform or "").lower()
        if "steam" in value:
            return QColor("#66c0f4")
        if "xbox" in value or "microsoft" in value:
            return QColor("#69d56d")
        if "playstation" in value:
            return QColor("#73a9ff")
        if "epic" in value or "eos" in value:
            return QColor("#d7dce5")
        return QColor("#b5c6dc")

    def _fill_player_table(self, table, rows):
        table.setRowCount(len(rows))
        for r, row in enumerate(rows):
            values = self._player_values(row)
            for c, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                if c == 1:
                    item.setForeground(self._platform_color(value))
                    font = item.font(); font.setBold(True); item.setFont(font)
                if c == 3 and "ms" in str(value):
                    item.setForeground(QColor("#55e996"))
                table.setItem(r, c, item)

    def _apply_player_rows(self, rows):
        rows = list(rows or [])
        self.player_rows = rows
        self.players_panel_title.setText(f"Connected Players ({len(rows)})")
        self._fill_player_table(self.players_table, rows)
        if hasattr(self, "players_page_table"):
            self._fill_player_table(self.players_page_table, rows)

    def _apply_ban_rows(self, rows):
        rows = list(rows or [])
        self.ban_rows = rows
        if not hasattr(self, "bans_table"):
            return
        self.bans_table.setRowCount(len(rows))
        for r, row in enumerate(rows):
            banned_at = row.get("banned_at")
            if banned_at:
                try:
                    banned_text = datetime.fromtimestamp(float(banned_at)).strftime("%Y-%m-%d %I:%M %p")
                except Exception:
                    banned_text = str(banned_at)
            else:
                banned_text = "-"
            vals = (
                row.get("player_name") or "Unknown / ID only",
                row.get("platform") or "Unknown",
                row.get("account_name") or "-",
                row.get("user_id") or "-",
                row.get("reason") or "-",
                banned_text,
            )
            for c, value in enumerate(vals):
                item = QTableWidgetItem(str(value))
                if c == 1:
                    item.setForeground(self._platform_color(value))
                    font = item.font(); font.setBold(True); item.setFont(font)
                self.bans_table.setItem(r, c, item)

    def _refresh_players_tables(self):
        # Kept for compatibility with older code paths; use the last overview
        # payload rather than issuing a synchronous network request.
        if self._overview_cache:
            self._apply_player_rows(self._overview_cache.get("players", []) or [])

    def refresh_players_page(self, silent=False):
        if self._overview_cache:
            self._apply_player_rows(self._overview_cache.get("players", []) or [])
            self._apply_ban_rows(self._overview_cache.get("bans", []) or [])
        def load():
            return {
                "players": self.manager.players(),
                "bans": self.manager.banned_players(),
            }

        def apply(data):
            self._apply_player_rows((data or {}).get("players", []))
            self._apply_ban_rows((data or {}).get("bans", []))

        self._run_async("players-page", "Players", load, apply, silent=silent)

    def refresh_bans_page(self, silent=False):
        self._run_async(
            "bans-page",
            "Ban Manager",
            self.manager.banned_players,
            self._apply_ban_rows,
            silent=silent,
        )

    def _selected_player(self):
        row = self.players_page_table.currentRow()
        return self.player_rows[row] if 0 <= row < len(self.player_rows) else None

    def _selected_ban(self):
        if not hasattr(self, "bans_table"):
            return None
        row = self.bans_table.currentRow()
        return self.ban_rows[row] if 0 <= row < len(self.ban_rows) else None

    def player_selected_action(self, action):
        row = self._selected_player()
        if not row:
            self.show_notice("Select a player first.", "warning")
            return
        user_id = str(row.get("userId", "") or row.get("playerId", "")).strip()
        name = row.get("name") or "selected player"
        platform = row.get("platform") or "Unknown platform"
        message = self.player_message.text().strip() or f"{action.title()}ed by administrator"

        def execute():
            def done(_result):
                self.show_notice(f"{action.title()} action sent for {name} ({platform}).", "success")
                self.refresh_players_page(silent=True)
                self.refresh_dashboard(silent=True)
            self._run_action_async(
                f"player-{action}-{user_id}",
                action.title(),
                lambda: self.manager.player_action(action, user_id, message),
                on_success=done,
            )

        self._confirm_then(f"confirm-player-{action}-{user_id}", f"{action.title()} {name}?", execute)

    def unban_player(self):
        user_id = self.unban_id.text().strip()
        if not user_id:
            self.show_notice("Enter a platform User ID to unban.", "warning")
            return

        def done(_result):
            self.show_notice(f"Unbanned {user_id}.", "success")
            self.unban_id.clear()
            self.refresh_bans_page(silent=True)

        self._run_action_async(
            f"unban-{user_id}",
            "Unban Player",
            lambda: self.manager.player_action("unban", user_id),
            on_success=done,
        )

    def unban_selected_ban(self):
        row = self._selected_ban()
        if not row:
            self.show_notice("Select a banned player first.", "warning")
            return
        user_id = str(row.get("user_id", "")).strip()
        if not user_id:
            return

        def execute():
            def done(_result):
                self.show_notice(f"Unbanned {row.get('player_name') or user_id}.", "success")
                self.refresh_bans_page(silent=True)
            self._run_action_async(
                f"unban-selected-{user_id}",
                "Unban Player",
                lambda: self.manager.player_action("unban", user_id),
                on_success=done,
            )

        self._confirm_then(f"confirm-unban-{user_id}", f"Unban {row.get('player_name') or user_id}?", execute)

    def broadcast_message(self):
        message = self.broadcast_text.text().strip()
        if not message:
            self.show_notice("Enter a broadcast message first.", "warning")
            return

        def done(_result):
            self.show_notice("Broadcast sent to the server.", "success")
            self.broadcast_text.clear()

        self._run_action_async("broadcast", "Broadcast", lambda: self.manager.announce(message), on_success=done)

    def _refresh_automation_dashboard(self, backup_check, sched=None):
        sched = sched or {"backup": {}, "updates": {}}
        backup_cfg=sched.get("backup",{}); update_cfg=sched.get("updates",{})
        if backup_cfg.get("enabled",True):
            backup_elapsed=int(backup_check.get("value")) if isinstance(backup_check.get("value"),(int,float)) else 0; self.automation_rows["Backup"].setText(self._remaining(backup_cfg.get("interval_minutes",120),backup_elapsed))
        else: self.automation_rows["Backup"].setText("Disabled")
        if update_cfg.get("auto_check",True): elapsed=max(0,time.time()-self.last_update_check) if self.last_update_check else 0; self.automation_rows["Update Check"].setText(self._remaining(update_cfg.get("check_interval_minutes",60),elapsed))
        else: self.automation_rows["Update Check"].setText("Disabled")
        self.automation_rows["Maintenance Window"].setText(f"{update_cfg.get('maintenance_start','04:00')} - {update_cfg.get('maintenance_end','05:00')}")

    def refresh_update_card(self):
        def apply(result):
            self.last_update_result = result or {"state": "unknown"}
            self.last_update_check = time.time()
        self._run_async("update-check-background", "Update Check", self.manager.update_check, apply, silent=True)

    def _apply_logs(self, lines):
        lines = list(lines or [])
        if hasattr(self, "log_view"):
            self.log_view.setPlainText("\n".join(lines))
            if self.auto_scroll_logs:
                self.log_view.verticalScrollBar().setValue(self.log_view.verticalScrollBar().maximum())
        if hasattr(self, "dashboard_log_view"):
            self.dashboard_log_view.setPlainText("\n".join(lines[-35:]))
            if self.auto_scroll_logs:
                self.dashboard_log_view.verticalScrollBar().setValue(self.dashboard_log_view.verticalScrollBar().maximum())

    def refresh_logs(self, errors_only=False, silent=False):
        if not errors_only and self._overview_cache:
            self._apply_logs(self._overview_cache.get("logs", []) or [])
        self._run_async(
            f"logs-{'errors' if errors_only else 'all'}",
            "Logs",
            lambda: self.manager.logs_tail(200, errors_only),
            self._apply_logs,
            silent=silent,
        )

    def service(self, action):
        def execute():
            self._set_service_action_state(None, loading=True)
            self.show_notice(f"{action.title()} in progress…", "info", 0)

            def done(_result):
                self.show_notice(f"Server {action} command completed.", "success")
                self.refresh_dashboard(silent=True)

            def failed(message):
                self.show_notice(f"{action.title()}: {message}", "error", 10000)
                self.refresh_header_status(silent=True)

            self._run_async(
                f"service-{action}",
                action.title(),
                lambda: self.manager.service_action(action),
                done,
                failed,
                silent=False,
            )
        if action in {"stop", "restart"}:
            self._confirm_then(f"confirm-service-{action}", f"{action.title()} the Palworld server? Connected players may be disconnected.", execute)
        else:
            execute()

    def save_world(self):
        self._run_action_async(
            "save-world",
            "Save World",
            self.manager.save_world,
            success_message="Palworld accepted the world-save request.",
        )

    def backup_now(self):
        def done(result):
            self.show_notice(f"Backup created: {result.get('path') or result.get('name') or 'completed'}", "success")
            self.refresh_dashboard(silent=True)
            self.refresh_backups_page(silent=True)
        self._run_action_async("backup-now", "Backup", lambda: self.manager.backup_create("manual"), on_success=done)

    def update_check(self):
        def done(result):
            self.last_update_result = result
            self.last_update_check = time.time()
            self.show_report("Palworld Update Status", "Steam build and update status", result)
            self.refresh_dashboard(silent=True)
        self._run_action_async("update-check", "Update Check", self.manager.update_check, on_success=done)

    def _apply_world_rows(self, rows):
        rows = list(rows or [])
        self.world_rows = rows
        self.world_table.setRowCount(len(rows))
        for r, row in enumerate(rows):
            modified = datetime.fromtimestamp(float(row.get("modified",0))).strftime("%Y-%m-%d %I:%M:%S %p") if row.get("modified") else "-"
            vals = (row.get("guid","-"), human_bytes(row.get("size")), modified, "Yes" if row.get("has_world_option") else "No")
            for c, v in enumerate(vals):
                self.world_table.setItem(r, c, QTableWidgetItem(str(v)))

    def refresh_worlds_page(self, silent=False):
        self._run_async("worlds-page", "Worlds", self.manager.world_list, self._apply_world_rows, silent=silent)

    def _selected_world(self):
        row = self.world_table.currentRow()
        return self.world_rows[row] if 0 <= row < len(self.world_rows) else None

    def archive_selected_world(self):
        row = self._selected_world()
        if not row:
            self.show_notice("Select a world first.", "warning")
            return
        def done(result):
            self.show_notice(f"World {row['guid']} archived.", "success")
            self.show_report("World Archive", "Archive operation result", result)
            self.refresh_worlds_page(silent=True)
        self._run_action_async(f"archive-world-{row['guid']}", "World Archive", lambda: self.manager.world_archive(row["guid"]), on_success=done)

    def delete_selected_world(self):
        row = self._selected_world()
        if not row:
            self.show_notice("Select a world first.", "warning")
            return
        def execute():
            def done(_result):
                self.show_notice(f"World {row['guid']} deleted after safety archive.", "success")
                self.refresh_worlds_page(silent=True)
                self.refresh_dashboard(silent=True)
            self._run_action_async(f"delete-world-{row['guid']}", "Delete World", lambda: self.manager.world_delete(row["guid"]), on_success=done)
        self._confirm_then(f"confirm-delete-world-{row['guid']}", f"Delete world {row['guid']}? A safety archive is created first.", execute)

    def create_fresh_world(self):
        def execute():
            def done(result):
                self.show_notice("Current worlds were archived and cleared. Palworld will create a fresh world on startup.", "success")
                self.show_report("Fresh World", "World reset result", result)
                self.refresh_worlds_page(silent=True)
                self.refresh_dashboard(silent=True)
            self._run_action_async("fresh-world", "Create Fresh World", self.manager.world_new, on_success=done)
        self._confirm_then("confirm-fresh-world", "Archive all current worlds and create a fresh world on next startup?", execute)

    def _set_mod_controls_available(self, available: bool, runtime_enabled: bool = False):
        self._mods_api_available = bool(available)
        self.mod_enable_runtime.setEnabled(bool(available) and not bool(runtime_enabled))
        self.mod_disable_runtime.setEnabled(bool(available) and bool(runtime_enabled))
        for name in ("mod_validate_button", "mod_install_button", "mod_toggle_button", "mod_remove_button", "mod_pack_button"):
            button = getattr(self, name, None)
            if button is not None:
                button.setEnabled(bool(available))
        if hasattr(self, "catalog_install_button"):
            if available:
                self.catalog_selection_changed()
            else:
                self.catalog_install_button.setEnabled(False)

    def _apply_mods_unavailable(self, message):
        text = str(message or "The selected node does not provide the mod-management API.")
        self._mods_catalog_agent_compatible = False
        self._set_mod_controls_available(False, False)
        self.mod_runtime_state.setText("AGENT UPDATE REQUIRED")
        self.mod_runtime_state.setStyleSheet("color:#ffcc66;")
        self.mod_runtime_version.setText("Runtime: unavailable")
        self.mod_runtime_health.setText("Health: unavailable")
        self.modset_version_label.setText("Modset: unavailable")
        self.client_pack_status.setText("Client pack: unavailable")
        self.mod_rows = []
        self.mods_table.setRowCount(0)
        self.mod_log_view.setPlainText(text + "\n\nOpen Remote Hosts, select this node, click Update Agent, verify version 0.6.0 or newer, then return here and click Refresh.")
        self.show_notice(text, "warning", 12000)

    def _apply_mods_status(self, data):
        data = data or {}
        runtime = data.get("runtime") or {}
        validation = data.get("validation") or {}
        enabled = bool(runtime.get("enabled"))
        health = str(validation.get("health") or ("configured" if enabled else "disabled"))
        self.mod_runtime_state.setText("MODDED" if enabled else "VANILLA")
        self.mod_runtime_state.setStyleSheet("color:#59efa0;" if enabled else "color:#dce8f8;")
        version = str(runtime.get("version") or "-")
        runtime_type = str(runtime.get("type") or "vanilla")
        self.mod_runtime_version.setText(f"Runtime: {runtime_type}  {version}")
        detail = str(validation.get("detail") or "")
        self.mod_runtime_health.setText(f"Health: {health}" + (f" • {detail}" if detail else ""))
        modset = int(data.get("modset_version") or 0)
        self.modset_version_label.setText(f"Modset version: {modset}")
        pack = data.get("client_pack") or {}
        self.client_pack_status.setText(f"Client pack v{pack.get('version', modset)} • {pack.get('mod_count', 0)} client-required mod(s)")
        agent_version = str(data.get("manager_version") or "0.0.0")
        self._mods_catalog_agent_compatible = version_key(agent_version) >= version_key("0.7.0")
        self._set_mod_controls_available(True, enabled)
        if hasattr(self, "catalog_status") and not self._mods_catalog_agent_compatible:
            self.catalog_status.setText(f"Browse is available, but one-click catalog installs require agent 0.7.0+ on this node (detected {agent_version or 'unknown'}).")

        rows = list(data.get("mods") or [])
        self.mod_rows = rows
        self.mods_table.setRowCount(len(rows))
        for r,row in enumerate(rows):
            compatibility = row.get("compatibility") or {}
            if isinstance(compatibility, dict):
                comp = ", ".join(f"{k}: {v}" for k,v in compatibility.items()) or "-"
            else:
                comp = str(compatibility or "-")
            vals = (
                "Enabled" if row.get("enabled", True) else "Disabled",
                row.get("name") or row.get("id") or "-",
                row.get("version") or "-",
                row.get("type") or "generic",
                row.get("runtime") or "-",
                "Required" if row.get("server_required", True) else "No",
                "Required" if row.get("client_required", False) else "No",
                comp,
            )
            for c,value in enumerate(vals):
                self.mods_table.setItem(r,c,QTableWidgetItem(str(value)))
        log = validation.get("log_tail") or []
        self.mod_log_view.setPlainText("\n".join(str(line) for line in log))
        if log:
            self.mod_log_view.verticalScrollBar().setValue(self.mod_log_view.verticalScrollBar().maximum())

    def refresh_mods_page(self, silent=False):
        self.mod_runtime_state.setText("Loading…")
        self._run_async(
            "mods-page", "Mods", self.manager.mods_status, self._apply_mods_status,
            on_error=self._apply_mods_unavailable, silent=silent,
        )
        if hasattr(self, "catalog_table") and not getattr(self, "catalog_rows", None):
            QTimer.singleShot(0, lambda: self.search_mod_catalog(reset=True))

    def enable_mod_runtime(self):
        if not getattr(self, "_mods_api_available", False):
            self.show_notice("Mod management requires agent version 0.6.0 or newer on the selected node. Update the agent from Remote Hosts first.", "warning", 10000)
            return
        def execute():
            def done(_result):
                self.show_notice("Linux UE4SS mod support enabled. A safety backup was created and the server was restarted if it was running.", "success", 9000)
                self.refresh_mods_page(silent=True); self.refresh_dashboard(silent=True)
            self._run_action_async("mods-runtime-enable", "Enable Mod Support", lambda: self.manager.mod_runtime_enable(""), on_success=done)
        self._confirm_then(
            "confirm-enable-mod-runtime",
            "Enable community Linux UE4SS mod support for this server? PalServer Manager will create a backup and perform a controlled restart if the server is running.",
            execute,
        )

    def disable_mod_runtime(self):
        if not getattr(self, "_mods_api_available", False):
            self.show_notice("Mod management requires agent version 0.6.0 or newer on the selected node. Update the agent from Remote Hosts first.", "warning", 10000)
            return
        def execute():
            def done(_result):
                self.show_notice("Mod runtime disabled. Installed managed mod metadata/files were preserved for later re-enable.", "success")
                self.refresh_mods_page(silent=True); self.refresh_dashboard(silent=True)
            self._run_action_async("mods-runtime-disable", "Disable Mod Support", self.manager.mod_runtime_disable, on_success=done)
        self._confirm_then("confirm-disable-mod-runtime", "Disable the UE4SS runtime and restart this server? A safety backup will be created first.", execute)

    def validate_mod_runtime(self):
        if not getattr(self, "_mods_api_available", False):
            self.show_notice("Mod management requires agent version 0.6.0 or newer on the selected node. Update the agent from Remote Hosts first.", "warning", 10000)
            return
        def done(result):
            self.show_notice(f"Runtime validation: {result.get('health','unknown')} — {result.get('detail','')}", "success" if result.get("health") == "healthy" else "warning", 10000)
            self.refresh_mods_page(silent=True)
        self._run_action_async("mods-validate", "Validate Mod Runtime", self.manager.mod_validate, on_success=done)

    def _selected_mod(self):
        row = self.mods_table.currentRow() if hasattr(self,"mods_table") else -1
        return self.mod_rows[row] if 0 <= row < len(self.mod_rows) else None

    def install_mod_package(self):
        if not getattr(self, "_mods_api_available", False):
            self.show_notice("Mod management requires agent version 0.6.0 or newer on the selected node. Update the agent from Remote Hosts first.", "warning", 10000)
            return
        path, _ = QFileDialog.getOpenFileName(self, "Install Managed Palworld Mod Package", "", "ZIP archives (*.zip);;All files (*)")
        if not path:
            return
        source = Path(path)
        try:
            payload = source.read_bytes()
        except OSError as exc:
            self.show_notice(f"Unable to read mod package: {exc}", "error", 10000); return
        def done(_result):
            self.show_notice(f"Installed managed mod package {source.name}. A safety backup was created and the server was restarted if required.", "success", 9000)
            self.refresh_mods_page(silent=True)
        self._run_action_async(
            f"mod-install-{source.name}", "Install Mod Package",
            lambda: self.manager.mod_install_package(payload, source.name), on_success=done,
        )

    def toggle_selected_mod(self):
        if not getattr(self, "_mods_api_available", False):
            self.show_notice("Mod management requires agent version 0.6.0 or newer on the selected node. Update the agent from Remote Hosts first.", "warning", 10000)
            return
        row = self._selected_mod()
        if not row:
            self.show_notice("Select an installed mod first.", "warning"); return
        enabled = not bool(row.get("enabled", True))
        action = "Enable" if enabled else "Disable"
        def execute():
            def done(_result):
                self.show_notice(f"{action}d {row.get('name') or row.get('id')}.", "success")
                self.refresh_mods_page(silent=True)
            self._run_action_async(f"mod-toggle-{row.get('id')}", f"{action} Mod", lambda: self.manager.mod_set_enabled(str(row.get("id")), enabled), on_success=done)
        self._confirm_then(f"confirm-mod-toggle-{row.get('id')}-{enabled}", f"{action} {row.get('name') or row.get('id')}? A backup and controlled restart will be used.", execute)

    def remove_selected_mod(self):
        if not getattr(self, "_mods_api_available", False):
            self.show_notice("Mod management requires agent version 0.6.0 or newer on the selected node. Update the agent from Remote Hosts first.", "warning", 10000)
            return
        row = self._selected_mod()
        if not row:
            self.show_notice("Select an installed mod first.", "warning"); return
        mod_id = str(row.get("id") or "")
        def execute():
            def done(_result):
                self.show_notice(f"Removed managed mod {row.get('name') or mod_id}.", "success")
                self.refresh_mods_page(silent=True)
            self._run_action_async(f"mod-remove-{mod_id}", "Remove Mod", lambda: self.manager.mod_remove(mod_id), on_success=done)
        self._confirm_then(f"confirm-mod-remove-{mod_id}", f"Remove {row.get('name') or mod_id} and its manager-tracked server/client files? A safety backup is created first.", execute)

    def download_client_mod_pack(self):
        if not getattr(self, "_mods_api_available", False):
            self.show_notice("Mod management requires agent version 0.6.0 or newer on the selected node. Update the agent from Remote Hosts first.", "warning", 10000)
            return
        suggested = f"palserver-client-modpack-{self._active_server_id() or 'server'}.zip"
        destination, _ = QFileDialog.getSaveFileName(self, "Save Client Mod Pack", suggested, "ZIP archives (*.zip)")
        if not destination:
            return
        def work():
            result = self.manager.mod_client_pack()
            if isinstance(result, dict) and isinstance(result.get("bytes"), (bytes, bytearray)):
                data = bytes(result["bytes"]); name = result.get("name")
            else:
                source = Path(str((result or {}).get("path") or ""))
                data = source.read_bytes(); name = source.name
            Path(destination).write_bytes(data)
            return {"path": destination, "size": len(data), "name": name}
        def done(result):
            self.show_notice(f"Client mod pack saved to {result.get('path')} ({human_bytes(result.get('size'))}).", "success", 10000)
        self._run_action_async("mods-client-pack", "Build Client Mod Pack", work, on_success=done)

    def _catalog_service(self):
        return ModCatalogService(
            api_key=str(getattr(self.cfg, "curseforge_api_key", "") or ""),
            game_id=int(getattr(self.cfg, "curseforge_game_id", 0) or 0),
        )

    def save_catalog_key(self):
        key = self.catalog_api_key.text().strip() if hasattr(self, "catalog_api_key") else ""
        self.cfg.curseforge_api_key = key
        if hasattr(self, "catalog_provider"):
            self.cfg.mod_catalog_provider = str(self.catalog_provider.currentData() or "curated")
        save_config(self.cfg)
        if key:
            self.show_notice("CurseForge API key saved on this manager PC. It is never sent to Linux agents.", "success", 8000)
        else:
            self.show_notice("Stored CurseForge API key cleared. Curated Linux mods remain available.", "info", 7000)

    def catalog_provider_changed(self):
        provider = str(self.catalog_provider.currentData() or "curated")
        self.cfg.mod_catalog_provider = provider
        save_config(self.cfg)
        self.catalog_index = 0
        if provider == "curseforge" and not str(getattr(self.cfg, "curseforge_api_key", "") or "").strip():
            self.catalog_status.setText("CurseForge live search needs your own API key. Paste it above and click Save Key.")
            self.catalog_rows = []
            self._apply_catalog_rows()
            return
        self.search_mod_catalog(reset=True)

    def search_mod_catalog(self, reset=False):
        if not hasattr(self, "catalog_provider"):
            return
        provider = str(self.catalog_provider.currentData() or "curated")
        if reset:
            self.catalog_index = 0
        query = self.catalog_search.text().strip() if hasattr(self, "catalog_search") else ""
        if provider == "curseforge" and not str(getattr(self.cfg, "curseforge_api_key", "") or "").strip():
            self.catalog_status.setText("CurseForge live search needs your own API key. Curated Linux remains available without one.")
            self.show_notice("Add your CurseForge API key on the Browse Linux Mods tab before using live CurseForge search.", "warning", 9000)
            return
        self.catalog_status.setText("Searching mod catalog…")
        service = self._catalog_service()
        index = int(self.catalog_index or 0)

        def work():
            result = service.search(provider, query, index=index, page_size=20)
            result["resolved_game_id"] = int(service.game_id or 0)
            return result

        def done(result):
            game_id = int((result or {}).get("resolved_game_id") or 0)
            if game_id and game_id != int(getattr(self.cfg, "curseforge_game_id", 0) or 0):
                self.cfg.curseforge_game_id = game_id
                save_config(self.cfg)
            self.catalog_rows = list((result or {}).get("items") or [])
            page = (result or {}).get("pagination") or {}
            self.catalog_index = int(page.get("index") or index)
            self.catalog_total = int(page.get("totalCount") or len(self.catalog_rows))
            page_size = int(page.get("pageSize") or max(1, len(self.catalog_rows)))
            page_no = (self.catalog_index // max(1, page_size)) + 1
            page_count = max(1, (self.catalog_total + max(1, page_size) - 1) // max(1, page_size))
            self.catalog_page_label.setText(f"Page {page_no} of {page_count} • {self.catalog_total} result(s)")
            self.catalog_prev.setEnabled(self.catalog_index > 0)
            self.catalog_next.setEnabled(self.catalog_index + page_size < self.catalog_total)
            self.catalog_status.setText("Catalog results are classified for native Linux before one-click installation.")
            self._apply_catalog_rows()

        def failed(message):
            self.catalog_status.setText(str(message))
            self.show_notice(f"Mod Catalog: {message}", "error", 11000)

        self._run_async(f"catalog-search-{provider}", "Mod Catalog", work, done, on_error=failed, silent=True)

    def change_catalog_page(self, direction):
        if not self.catalog_rows and direction < 0:
            return
        step = 20
        self.catalog_index = max(0, int(self.catalog_index or 0) + int(direction) * step)
        self.search_mod_catalog(reset=False)

    def _apply_catalog_rows(self):
        if not hasattr(self, "catalog_table"):
            return
        mode = str(self.catalog_filter.currentData() or "safe") if hasattr(self, "catalog_filter") else "safe"
        visible = []
        for row in list(getattr(self, "catalog_rows", []) or []):
            compatibility = str(row.get("compatibility") or "untested").lower()
            server_required = bool(row.get("server_required", False))
            if mode == "verified" and compatibility != "verified":
                continue
            if mode == "safe" and compatibility not in {"verified", "compatible"}:
                continue
            if mode == "server" and not server_required:
                continue
            visible.append(row)
        self.catalog_visible_rows = visible
        self.catalog_table.setRowCount(len(visible))
        badges = {
            "verified": "VERIFIED", "compatible": "CANDIDATE", "untested": "UNTESTED",
            "windows-only": "WINDOWS ONLY", "unsupported": "UNSUPPORTED",
        }
        for r,row in enumerate(visible):
            compatibility = str(row.get("compatibility") or "untested").lower()
            cats = [str(value) for value in (row.get("categories") or [])]
            type_name = "Lua" if any("lua" in value.lower() for value in cats) else ("C++" if any("c++" in value.lower() for value in cats) else "Mod")
            updated = str(row.get("date_modified") or "")[:10] or "-"
            vals = (
                badges.get(compatibility, compatibility.upper()),
                row.get("name") or "-",
                row.get("version") or "latest",
                "CurseForge" if str(row.get("source") or "").lower() == "curseforge" else str(row.get("source") or "Catalog"),
                type_name,
                "Required" if row.get("client_required") else "No",
                f"{int(row.get('download_count') or 0):,}",
                updated,
            )
            for c,value in enumerate(vals):
                self.catalog_table.setItem(r,c,QTableWidgetItem(str(value)))
        self.catalog_detail.setText("Select a mod to see Linux compatibility details.")
        self.catalog_install_button.setEnabled(False)

    def _selected_catalog_mod(self):
        row = self.catalog_table.currentRow() if hasattr(self, "catalog_table") else -1
        return self.catalog_visible_rows[row] if 0 <= row < len(getattr(self, "catalog_visible_rows", [])) else None

    def catalog_selection_changed(self):
        row = self._selected_catalog_mod()
        if not row:
            self.catalog_install_button.setEnabled(False)
            return
        compatibility = str(row.get("compatibility") or "untested").lower()
        categories = ", ".join(str(value) for value in (row.get("categories") or [])) or "-"
        detail = str(row.get("compatibility_detail") or "Linux compatibility has not been verified.")
        summary = str(row.get("summary") or "")
        author = str(row.get("author") or "Unknown author")
        self.catalog_detail.setText(
            f"{row.get('name')} • by {author} • Linux: {('CANDIDATE' if compatibility == 'compatible' else compatibility.upper())}\n"
            f"{detail}\nCategories: {categories}" + (f"\n{summary}" if summary else "")
        )
        installable = bool(row.get("server_required", False)) and compatibility not in {"windows-only", "unsupported"}
        agent_ok = bool(getattr(self, "_mods_catalog_agent_compatible", False))
        self.catalog_install_button.setEnabled(bool(installable and getattr(self, "_mods_api_available", False) and agent_ok))
        if installable and getattr(self, "_mods_api_available", False) and not agent_ok:
            self.catalog_detail.setText(self.catalog_detail.text() + "\nUpdate this node's PalServer Manager agent to 0.7.0+ before one-click installation.")

    def install_catalog_selected(self):
        if not getattr(self, "_mods_api_available", False):
            self.show_notice("Update the selected node's agent before installing catalog mods.", "warning", 9000)
            return
        if not getattr(self, "_mods_catalog_agent_compatible", False):
            self.show_notice("One-click Linux catalog installs require PalServer Manager agent 0.7.0 or newer on the selected node. Update Agent from Remote Hosts first.", "warning", 11000)
            return
        row = self._selected_catalog_mod()
        if not row:
            self.show_notice("Select a catalog mod first.", "warning")
            return
        compatibility = str(row.get("compatibility") or "untested").lower()
        if compatibility in {"windows-only", "unsupported"}:
            self.show_notice("This package is not eligible for native Linux one-click installation.", "error", 9000)
            return

        def execute():
            self.catalog_status.setText(f"Installing {row.get('name')}… Download, Linux archive scan, backup and restart are automatic.")

            def work():
                service = self._catalog_service()
                plan = service.install_plan(row)
                status = self.manager.mods_status() or {}
                runtime_enabled = bool((status.get("runtime") or {}).get("enabled"))
                runtime_result = None
                if not runtime_enabled:
                    runtime_result = self.manager.mod_runtime_enable("")
                installed = list((self.manager.mods_status() or {}).get("mods") or [])
                installed_ids = {str(item.get("id") or "") for item in installed}
                results = []
                for plan_item in plan:
                    generated_id = f"{str(plan_item.get('source') or 'catalog')}-{plan_item.get('mod_id') or plan_item.get('slug')}"
                    if generated_id in installed_ids:
                        results.append({"name": plan_item.get("name"), "skipped": True, "reason": "already installed"})
                        continue
                    raw, metadata = service.download(plan_item)
                    package, conversion = build_managed_package(raw, metadata)
                    filename = f"{generated_id}-{str(metadata.get('version') or 'latest')}.zip"
                    remote_result = self.manager.mod_install_package(package, filename)
                    results.append({
                        "name": metadata.get("name"),
                        "version": metadata.get("version"),
                        "compatibility": (conversion.get("scan") or {}).get("compatibility"),
                        "remote": remote_result,
                    })
                    installed_ids.add(generated_id)
                return {"runtime_enabled": runtime_result is not None, "results": results, "game_id": int(service.game_id or 0)}

            def done(result):
                game_id = int((result or {}).get("game_id") or 0)
                if game_id and game_id != int(getattr(self.cfg, "curseforge_game_id", 0) or 0):
                    self.cfg.curseforge_game_id = game_id
                    save_config(self.cfg)
                installed = [item for item in (result or {}).get("results", []) if not item.get("skipped")]
                skipped = [item for item in (result or {}).get("results", []) if item.get("skipped")]
                message = f"Installed {len(installed)} catalog package(s)"
                if skipped:
                    message += f"; {len(skipped)} already installed dependency/package(s) skipped"
                self.catalog_status.setText(message + ".")
                self.show_notice(message + ".", "success", 10000)
                self.refresh_mods_page(silent=True)
                self.mods_tabs.setCurrentIndex(0)

            def failed(message):
                self.catalog_status.setText(f"Install failed: {message}")
                self.show_notice(f"Catalog install failed: {message}", "error", 12000)

            self._run_async(
                f"catalog-install-{row.get('source')}-{row.get('mod_id')}",
                "Install Catalog Mod", work, done, on_error=failed, silent=True,
            )

        if compatibility == "untested":
            self._confirm_then(
                f"confirm-catalog-untested-{row.get('mod_id')}",
                "This mod is a server-side candidate but is not Linux-verified. PalServer Manager will scan the archive and refuse unsafe Windows-only content. Click Install Selected again to continue.",
                execute,
            )
        else:
            execute()

    def _apply_backup_rows(self, rows):
        rows = list(rows or [])
        self.backup_rows = rows
        self.backup_table.setRowCount(len(rows))
        for r, row in enumerate(rows):
            created = datetime.fromtimestamp(float(row.get("created",0))).strftime("%Y-%m-%d %I:%M:%S %p") if row.get("created") else "-"
            vals = (row.get("name","-"), human_bytes(row.get("size")), created)
            for c, v in enumerate(vals):
                self.backup_table.setItem(r, c, QTableWidgetItem(str(v)))

    def refresh_backups_page(self, silent=False):
        if self._overview_cache:
            self._apply_backup_rows(self._overview_cache.get("backups", []) or [])
        self._run_async("backups-page", "Backups", self.manager.backup_list, self._apply_backup_rows, silent=silent)

    def _selected_backup(self):
        row = self.backup_table.currentRow()
        return self.backup_rows[row] if 0 <= row < len(self.backup_rows) else None

    def create_backup_page(self):
        def done(_result):
            self.show_notice("Backup created successfully.", "success")
            self.refresh_backups_page(silent=True)
            self.refresh_dashboard(silent=True)
        self._run_action_async("backup-create-page", "Create Backup", lambda: self.manager.backup_create("manual"), on_success=done)

    def restore_selected_backup(self):
        row = self._selected_backup()
        if not row:
            self.show_notice("Select a backup first.", "warning")
            return
        def execute():
            def done(_result):
                self.show_notice(f"Backup {row['name']} restored successfully.", "success")
                self.refresh_dashboard(silent=True)
            self._run_action_async(f"restore-backup-{row['name']}", "Restore Backup", lambda: self.manager.backup_restore(row["name"]), on_success=done)
        self._confirm_then(f"confirm-restore-backup-{row['name']}", f"Restore {row['name']}? The server will be stopped while data is replaced.", execute)

    def delete_selected_backup(self):
        row = self._selected_backup()
        if not row:
            self.show_notice("Select a backup first.", "warning")
            return
        def execute():
            def done(_result):
                self.show_notice(f"Backup {row['name']} deleted.", "success")
                self.refresh_backups_page(silent=True)
            self._run_action_async(f"delete-backup-{row['name']}", "Delete Backup", lambda: self.manager.backup_delete(row["name"]), on_success=done)
        self._confirm_then(f"confirm-delete-backup-{row['name']}", f"Permanently delete backup {row['name']}?", execute)

    def _normalize_settings_rows(self, rows):
        normalized = []
        for original in rows or []:
            row = dict(original)
            key = str(row.get("key", ""))
            raw = str(row.get("raw_value", row.get("display_value", "")))
            row.setdefault("readable_name", display_name_for(key))
            row.setdefault("description", description_for(key))
            row.setdefault("allowed_values", allowed_values_for(key, raw))
            choices = list(row.get("choices") or CHOICES.get(key, []))
            if raw in {"True", "False"} or key.startswith("b"):
                choices = ["True", "False"]
            row["choices"] = choices
            normalized.append(row)
        return normalized

    def _apply_settings_bundle(self, bundle):
        bundle = bundle or {}
        normalized = self._normalize_settings_rows(bundle.get("rows", []))
        self.settings_rows = normalized
        current_cat = self.setting_category.currentText()
        cats = sorted({str(r.get("category", "Other")) for r in normalized})
        self.setting_category.blockSignals(True)
        self.setting_category.clear(); self.setting_category.addItem("All Categories"); self.setting_category.addItems(cats)
        idx = self.setting_category.findText(current_cat); self.setting_category.setCurrentIndex(idx if idx >= 0 else 0)
        self.setting_category.blockSignals(False)
        profiles = bundle.get("profiles", {}) or {}
        current_profile = self.profile_combo.currentText()
        self.profile_combo.clear(); self.profile_combo.addItems(list(profiles))
        pidx = self.profile_combo.findText(current_profile); self.profile_combo.setCurrentIndex(pidx if pidx >= 0 else 0)
        self.filter_settings_rows()

    def refresh_settings_page(self, silent=False):
        def load():
            return {"rows": self.manager.settings(""), "profiles": self.manager.profiles_list()}
        self._run_async("settings-page", "Settings", load, self._apply_settings_bundle, silent=silent)

    def filter_settings_rows(self,*args):
        q=self.setting_search.text().strip().lower() if hasattr(self,"setting_search") else ""; category=self.setting_category.currentText() if hasattr(self,"setting_category") else "All Categories"; filtered=[]
        for row in self.settings_rows:
            hay=" ".join(str(row.get(k,"")) for k in ("readable_name","key","description","allowed_values","category")).lower()
            if q and q not in hay: continue
            if category!="All Categories" and row.get("category")!=category: continue
            filtered.append(row)
        self.filtered_settings_rows=filtered; self.settings_table.setRowCount(len(filtered))
        for r,row in enumerate(filtered):
            vals=(row.get("readable_name") or row.get("key"),row.get("display_value","-"),row.get("category","Other"),row.get("key","-"))
            for c,v in enumerate(vals): self.settings_table.setItem(r,c,QTableWidgetItem(str(v)))
        if filtered and self.settings_table.currentRow()<0: self.settings_table.selectRow(0)
        elif not filtered: self._clear_setting_detail()

    def _clear_setting_detail(self):
        self.setting_detail_name.setText("Select a setting"); self.setting_detail_key.setText(""); self.setting_detail_desc.setText("Choose a row to see exactly what the setting controls."); self.setting_allowed.setText("-"); self.setting_value_edit.clear(); self.setting_value_combo.clear(); self.setting_value_combo.hide(); self.setting_value_edit.show()

    def _selected_setting(self):
        row=self.settings_table.currentRow(); return self.filtered_settings_rows[row] if 0<=row<len(self.filtered_settings_rows) else None

    def setting_selection_changed(self):
        row=self._selected_setting()
        if not row: self._clear_setting_detail(); return
        self.setting_detail_name.setText(str(row.get("readable_name") or row.get("key"))); self.setting_detail_key.setText(f"Technical key: {row.get('key','-')}"); self.setting_detail_desc.setText(str(row.get("description","No description available."))); self.setting_allowed.setText(str(row.get("allowed_values","Use the existing format.")))
        choices=row.get("choices") or []; secret=bool(row.get("secret")); raw=str(row.get("raw_value","")).strip(); display=str(row.get("display_value","")).strip();
        if choices:
            self.setting_value_edit.hide(); self.setting_value_combo.show(); self.setting_value_combo.clear(); self.setting_value_combo.addItems([str(x) for x in choices]); current=raw.strip('"'); idx=self.setting_value_combo.findText(current); self.setting_value_combo.setCurrentIndex(idx if idx>=0 else 0)
        else:
            self.setting_value_combo.hide(); self.setting_value_edit.show(); self.setting_value_edit.setEchoMode(QLineEdit.Password if secret else QLineEdit.Normal); self.setting_value_edit.setPlaceholderText("Enter a new value; current secret is hidden" if secret else "Enter new value"); self.setting_value_edit.setText("" if secret else raw.strip('"'))

    def save_selected_setting(self):
        row = self._selected_setting()
        if not row:
            self.show_notice("Select a setting first.", "warning")
            return
        value = self.setting_value_combo.currentText() if self.setting_value_combo.isVisible() else self.setting_value_edit.text()
        if value == "" and not row.get("secret"):
            self.show_notice("Enter a value before saving.", "warning")
            return
        if row.get("secret") and value == "":
            self.show_notice("Enter a new secret value; leaving it blank makes no change.", "warning")
            return
        def done(_result):
            self.show_notice(f"{row.get('readable_name') or row['key']} saved and verified.", "success")
            self.refresh_settings_page(silent=True)
            self.refresh_dashboard(silent=True)
        self._run_action_async(f"save-setting-{row['key']}", "Save Setting", lambda: self.manager.set_setting(row["key"], value), on_success=done)

    def reset_selected_setting(self):
        row = self._selected_setting()
        if not row:
            self.show_notice("Select a setting first.", "warning")
            return
        def execute():
            def done(_result):
                self.show_notice(f"{row.get('readable_name') or row['key']} reset to the Palworld default.", "success")
                self.refresh_settings_page(silent=True)
            self._run_action_async(f"reset-setting-{row['key']}", "Reset Setting", lambda: self.manager.reset_defaults([row["key"]]), on_success=done)
        self._confirm_then(f"confirm-reset-setting-{row['key']}", f"Reset {row.get('readable_name') or row['key']} to the Palworld default?", execute)

    def reset_all_settings(self):
        def execute():
            def done(result):
                self.show_notice(f"Reset {len(result.get('changes', []))} settings to defaults.", "success")
                self.refresh_settings_page(silent=True)
                self.show_report("Settings Reset", "Verified reset results", result)
            self._run_action_async("reset-all-settings", "Reset All Settings", lambda: self.manager.reset_defaults(None), on_success=done)
        self._confirm_then("confirm-reset-all-settings", "Reset every setting present in DefaultPalWorldSettings.ini? A configuration backup is created first.", execute)

    def show_compare_defaults(self):
        self._prepare_report("Non-default Settings", "Loading settings that differ from DefaultPalWorldSettings.ini…")
        loading = self._make_kv_card("Compare Defaults", {"Status": "Loading…"})
        self.report_layout.addWidget(loading)
        self.report_layout.addStretch(1)
        self.show_named_page("report")
        self._run_async(
            "compare-defaults",
            "Compare Defaults",
            self.manager.compare_defaults,
            self._show_nondefault_settings_report,
            on_error=lambda message: self.show_report("Compare Defaults", "Unable to compare Palworld settings", {"Error": message}),
            silent=False,
        )

    def apply_profile(self):
        name = self.profile_combo.currentText().strip()
        if not name:
            self.show_notice("No profile is selected.", "warning")
            return
        def execute():
            def done(result):
                self.show_notice(f"Profile {name} applied.", "success")
                self.refresh_settings_page(silent=True)
                self.show_report("Profile Applied", f"Changes made by {name}", result)
            self._run_action_async(f"profile-{name}", "Apply Profile", lambda: self.manager.profile_apply(name), on_success=done)
        self._confirm_then(f"confirm-profile-{name}", f"Apply the {name} configuration profile?", execute)

    def _apply_automation_data(self, data):
        data = data or {}
        b = data.get("backup", {}) or {}
        u = data.get("updates", {}) or {}
        self.auto_backup_enabled.setChecked(bool(b.get("enabled", True)))
        self.auto_backup_interval.setValue(int(b.get("interval_minutes", 120)))
        self.auto_retention.setValue(int(b.get("retention_count", 30)))
        self.auto_update_check.setChecked(bool(u.get("auto_check", True)))
        self.auto_update_interval.setValue(int(u.get("check_interval_minutes", 60)))
        self.auto_install_updates.setChecked(bool(u.get("auto_install", False)))
        self.auto_only_empty.setChecked(bool(u.get("only_when_empty", True)))
        self.auto_window_start.setText(str(u.get("maintenance_start", "04:00")))
        self.auto_window_end.setText(str(u.get("maintenance_end", "05:00")))

    def refresh_automation_page(self, silent=False):
        if self._overview_cache:
            self._apply_automation_data(self._overview_cache.get("scheduler", {}) or {})
        self._run_async("automation-page", "Automation", self.manager.scheduler_config, self._apply_automation_data, silent=silent)

    def save_automation_page(self):
        payload = {
            "backup": {
                "enabled": self.auto_backup_enabled.isChecked(),
                "interval_minutes": self.auto_backup_interval.value(),
                "retention_count": self.auto_retention.value(),
            },
            "updates": {
                "auto_check": self.auto_update_check.isChecked(),
                "check_interval_minutes": self.auto_update_interval.value(),
                "auto_install": self.auto_install_updates.isChecked(),
                "only_when_empty": self.auto_only_empty.isChecked(),
                "maintenance_start": self.auto_window_start.text().strip(),
                "maintenance_end": self.auto_window_end.text().strip(),
            },
        }
        def done(result):
            self.show_notice("Automation settings saved to the server agent.", "success")
            self._apply_automation_data(result)
            self.refresh_dashboard(silent=True)
        self._run_action_async("save-automation", "Automation", lambda: self.manager.scheduler_update(payload), on_success=done)

    def refresh_health_page(self, silent=False):
        # Paint cached values instantly, then refresh with the lightweight
        # watchdog endpoint. Do not launch the full dashboard/overview request
        # just to populate this page.
        if self._overview_cache:
            try:
                self._apply_health_data(
                    self._overview_cache.get("health", {}) or {},
                    self._overview_cache.get("status", {}) or {},
                )
            except Exception as exc:
                if not silent:
                    self.show_notice(f"Health: cached UI update failed: {exc}", "error", 10000)

        def apply(snapshot):
            snapshot = snapshot or {}
            status = snapshot.get("status", {}) or {}
            health = snapshot.get("health", {}) or {}
            if status:
                self._apply_header_status(status)
            self._apply_health_data(health, status)

        self._run_async(
            "health-page",
            "Server Health",
            self.manager.watchdog_snapshot,
            apply,
            on_error=lambda message: self.show_notice(f"Server Health: {message}", "error", 10000),
            silent=silent,
        )

    def _apply_health_data(self, health, status):
        health = health or {}
        status = status or {}
        overall = str(health.get("overall", "unknown")).lower()
        tone = "good" if overall == "healthy" else ("warning" if overall == "warning" else "bad")
        self.health_page_badge.set_state(overall.upper(), tone)
        checks = {str(row.get("name")): row for row in health.get("checks", [])}
        problem_rows = [row for row in health.get("checks", []) if str(row.get("state")) in {"warning", "critical"}]
        if overall == "healthy":
            self.health_page_text.setText("All systems operating normally")
            self.health_page_reason.setText("Every configured resource, service, port, backup, and FPS check is currently healthy.")
        else:
            critical = [str(r.get("name")) for r in problem_rows if str(r.get("state")) == "critical"]
            warnings = [str(r.get("name")) for r in problem_rows if str(r.get("state")) == "warning"]
            self.health_page_text.setText("Critical attention required" if critical else "One or more checks need attention")
            parts = []
            if critical: parts.append("Critical: " + ", ".join(critical))
            if warnings: parts.append("Warning: " + ", ".join(warnings))
            self.health_page_reason.setText(" • ".join(parts) if parts else "A server health check is not healthy.")

        threshold_data = health.get("thresholds", {}) or {}
        thresholds = {
            "cpu": (threshold_data.get("cpu", {}).get("warning"), threshold_data.get("cpu", {}).get("critical")),
            "ram": (threshold_data.get("ram", {}).get("warning"), threshold_data.get("ram", {}).get("critical")),
            "disk": (threshold_data.get("disk", {}).get("warning"), threshold_data.get("disk", {}).get("critical")),
        }
        for key, pct in (
            ("cpu", float(health.get("cpu_percent", 0) or 0)),
            ("ram", float(health.get("memory_percent", 0) or 0)),
            ("disk", float(health.get("disk_percent", 0) or 0)),
        ):
            value_label, progress, threshold_label = self.health_metric_cards[key]
            progress.setValue(int(max(0, min(100, pct))))
            value_label.setText(f"{pct:.1f}%")
            warn, crit = thresholds[key]
            if warn is None or crit is None:
                threshold_label.setText("Uses this server's configured warning / critical thresholds")
            else:
                threshold_label.setText(f"Warning ≥ {warn}%  •  Critical ≥ {crit}%")

        def set_check(card_key, value_text, state_text, detail_text):
            value_label, detail_label, state_badge = self.health_check_cards[card_key]
            state_text = str(state_text or "warning").lower()
            tone_name = "good" if state_text == "healthy" else ("warning" if state_text == "warning" else "bad")
            value_label.setText(str(value_text))
            detail_label.setText(str(detail_text))
            state_badge.set_state(state_text.upper(), tone_name)

        service = checks.get("Service", {})
        set_check("Service", (status.get("service", {}) or {}).get("state", service.get("value", "-")), service.get("state", "warning"), "Palworld operating-system service/process state")
        port = checks.get("Game Port", {})
        set_check("Game Port", "Listening" if status.get("udp_listening") else port.get("value", "Not listening"), port.get("state", "warning"), f"UDP {status.get('game_port', '-')} game listener")
        rest_online = status.get("server_fps") is not None or status.get("current_players") is not None
        active_row = self._active_instance_row()
        rest_port = status.get("rest_api_port") or active_row.get("rest_api_port") or 8212
        set_check("REST API", "Online" if rest_online else "Unavailable", "healthy" if rest_online else "warning", f"Private Palworld REST API on port {rest_port}")
        backup = checks.get("Backup", {})
        backup_value = backup.get("value", "-")
        if isinstance(backup_value, (int, float)):
            backup_text = f"{self._format_age(backup_value)} old"
        elif backup_value == "none":
            backup_text = "No backup"
        else:
            backup_text = str(backup_value)
        set_check("Backup", backup_text, backup.get("state", "warning"), "Age of the newest manager backup")
        fps = checks.get("Server FPS", {})
        fps_value = fps.get("value", status.get("server_fps", "-"))
        fps_state = fps.get("state", "healthy" if status.get("server_fps") is not None else "warning")
        set_check("Server FPS", f"{fps_value} FPS" if fps_value not in (None, "-") else "Unavailable", fps_state, "Live dedicated-server frame rate")
        mode = str(self._current_connection_mode() or "local").upper()
        host_name = (status.get("host") or {}).get("hostname") or status.get("lan_ip") or "server"
        set_check("Manager Link", f"{mode} • {host_name}", "healthy", "Current local/direct/SSH management path")

    def _human_label(self, key):
        text = str(key).replace("_", " ").strip()
        return " ".join(
            word.upper() if word.lower() in {"api", "ip", "cpu", "ram", "udp", "tcp", "pid", "tls", "ssh"}
            else word.capitalize()
            for word in text.split()
        )

    def _friendly_value(self,value):
        if isinstance(value,bool): return "Yes" if value else "No"
        if value is None: return "-"
        if isinstance(value,(list,tuple)): return ", ".join(self._friendly_value(v) for v in value) if value else "None"
        if isinstance(value,dict): return "; ".join(f"{self._human_label(k)}: {self._friendly_value(v)}" for k,v in value.items())
        return str(value)

    def _clear_layout(self, layout):
        """Remove every child widget/layout from a dynamic UI container."""
        while layout.count():
            item = layout.takeAt(0)
            widget = item.widget()
            child = item.layout()
            if widget is not None:
                widget.deleteLater()
            elif child is not None:
                self._clear_layout(child)

    def _make_kv_card(self, title, data):
        """Render structured diagnostic/report data using the normal UI cards."""
        card = QFrame(); card.setObjectName("detailCard")
        card.setMinimumHeight(105); card.setMaximumHeight(230)
        cl = QVBoxLayout(card); cl.setContentsMargins(16,13,16,13); cl.setSpacing(5)
        heading = QLabel(str(title)); heading.setObjectName("sectionTitle"); cl.addWidget(heading)
        if isinstance(data, dict):
            for key, value in data.items():
                row = QHBoxLayout(); row.setSpacing(12)
                lab = QLabel(self._human_label(key)); lab.setObjectName("rowLabel"); lab.setMinimumWidth(120)
                val = QLabel(self._friendly_value(value)); val.setObjectName("rowValue")
                val.setWordWrap(True); val.setTextInteractionFlags(Qt.TextSelectableByMouse)
                row.addWidget(lab); row.addWidget(val, 1); cl.addLayout(row)
        else:
            val = QLabel(self._friendly_value(data)); val.setObjectName("detailDescription")
            val.setWordWrap(True); val.setTextInteractionFlags(Qt.TextSelectableByMouse); cl.addWidget(val)
        return card

    def _render_network_diagnostics(self, data):
        self._clear_layout(self.network_diagnostics_layout)
        if not data:
            empty = self._make_kv_card("Network", {"Status": "No network diagnostic data returned"})
            self.network_diagnostics_layout.addWidget(empty, 0, 0)
            return
        cards = [
            ("Addressing", {
                "Local IP": data.get("local_ip", "-"),
                "Default gateway": data.get("default_gateway", "-"),
                "Private LAN address": data.get("private_lan_address", "-"),
                "Public IP": data.get("public_ip", "Not checked"),
            }),
            ("Palworld Game Port", {
                "Port": f"{data.get('game_port', '-')} / UDP",
                "Local listener": "Listening" if data.get("game_udp_listening") else "Not listening",
            }),
            ("Palworld REST API", {
                "Port": data.get("rest_api_port", "-"),
                "Private / loopback only": "Yes" if data.get("rest_api_local_only") else "No",
            }),
            ("Remote Access", {
                "NAT likely": data.get("nat_likely", "-"),
                "Note": data.get("remote_note", "-"),
            }),
        ]
        for i, (title, values) in enumerate(cards):
            self.network_diagnostics_layout.addWidget(self._make_kv_card(title, values), i // 2, i % 2)
        self.network_diagnostics_layout.setColumnStretch(0, 1)
        self.network_diagnostics_layout.setColumnStretch(1, 1)

    def _diagnostics_error_card(self, layout, title, message):
        self._clear_layout(layout)
        card = self._make_kv_card(title, {
            "Status": "Unable to load",
            "Details": message,
            "Action": "Use Refresh to retry. If the problem continues, confirm the Ubuntu PalServer Manager agent is running and matches the Windows manager version.",
        })
        layout.addWidget(card, 0, 0)
        layout.setColumnStretch(0, 1)

    def _apply_installation_diagnostics(self, data):
        self._clear_layout(self.diagnostics_layout)
        cards = []
        paths = (data or {}).get("paths", {}) or {}
        for name, info in paths.items():
            info = info or {}
            exists = bool(info.get("exists"))
            cards.append((
                self._human_label(name),
                {"Path": info.get("path", "-"), "Status": "Found" if exists else "Missing"},
            ))
        service = (data or {}).get("service", {}) or {}
        cards.append(("Palworld Service", {
            "State": service.get("state", "unknown"),
            "Enabled": service.get("enabled", "-"),
            "PID": service.get("pid", "-"),
            "Started": service.get("started", "-"),
        }))
        cards.append(("Configuration & API", {
            "REST API available": (data or {}).get("api_available"),
            "Config readable": (data or {}).get("config_readable"),
            "Config writable": (data or {}).get("config_writable"),
        }))
        if not cards:
            cards.append(("Installation & Service", {"Status": "No diagnostic fields were returned"}))
        for i, (title, values) in enumerate(cards):
            self.diagnostics_layout.addWidget(self._make_kv_card(title, values), i // 2, i % 2)
        self.diagnostics_layout.setColumnStretch(0, 1)
        self.diagnostics_layout.setColumnStretch(1, 1)

    def _apply_diagnostics_bundle(self, bundle):
        bundle = bundle or {}
        install = bundle.get("installation")
        network = bundle.get("network")
        if install is None:
            self._diagnostics_error_card(
                self.diagnostics_layout,
                "Installation & Service",
                "The manager did not receive installation diagnostic data from the server agent.",
            )
        else:
            self._apply_installation_diagnostics(install)
        if network is None:
            self._diagnostics_error_card(
                self.network_diagnostics_layout,
                "Network Diagnostics",
                "The manager did not receive network diagnostic data from the server agent.",
            )
        else:
            self._render_network_diagnostics(network)

    def refresh_diagnostics_page(self, silent=False):
        # LocalManager.diagnostics() already includes its network snapshot, so
        # one agent request can populate both sections. This is faster and
        # avoids one failed network request blanking the whole page.
        def load():
            installation = self.manager.diagnostics() or {}
            return {
                "installation": installation,
                "network": installation.get("network"),
            }
        self._run_async(
            "diagnostics-page",
            "Diagnostics",
            load,
            self._apply_diagnostics_bundle,
            on_error=lambda message: (
                self._diagnostics_error_card(self.diagnostics_layout, "Installation & Service", message),
                self._diagnostics_error_card(self.network_diagnostics_layout, "Network Diagnostics", message),
            ),
            silent=silent,
        )

    def show_network_diagnostics(self):
        if self.current_page_name() != "diagnostics":
            self.stack.setCurrentIndex(self.pages["diagnostics"])
            for page, button in self.nav_buttons.items():
                button.setChecked(page == "diagnostics")

        def failed(message):
            self._diagnostics_error_card(self.network_diagnostics_layout, "Network Diagnostics", message)

        def done(data):
            self._render_network_diagnostics(data)
            self.show_notice("Network diagnostics refreshed.", "success", 3500)

        self._run_async(
            "network-diagnostics",
            "Network Diagnostics",
            lambda: self.manager.network_diagnostics(False),
            done,
            failed,
            silent=False,
        )

    def show_crash_history(self):
        self._prepare_report("Crash History", "Loading recent warning, error, and crash activity…")
        self.report_layout.addWidget(self._make_kv_card("Crash Log Scan", {"Status": "Loading…"}))
        self.report_layout.addStretch(1)
        self.show_named_page("report")

        def load():
            return {
                "summary": self.manager.crash_summary(),
                "lines": self.manager.logs_tail(350, True),
            }
        def done(data):
            self._show_crash_history_report((data or {}).get("summary", {}) or {}, (data or {}).get("lines", []) or [])
        self._run_async(
            "crash-history",
            "Crash History",
            load,
            done,
            on_error=lambda message: self.show_report("Crash History", "Unable to load crash history", {"Error": message}),
            silent=False,
        )

    def refresh_connection_page(self, silent=False):
        host = self._active_host_config()
        if host is not None:
            address = host.ssh_host or host.remote_url or "-"
            self.connection_context_label.setText(
                f"Selected node: {host.name} ({host.id}) • {address}. Changes on this page apply only to this node."
            )
            self.conn_mode.setCurrentText(str(host.mode or "ssh"))
            self.conn_remote_url.setText(str(host.remote_url or ""))
            self.conn_token.setText(str(host.agent_token or ""))
            self.conn_verify_tls.setChecked(bool(host.verify_tls))
            self.conn_ssh_host.setText(str(host.ssh_host or ""))
            self.conn_ssh_user.setText(str(host.ssh_user or ""))
            self.conn_ssh_port.setValue(int(host.ssh_port or 22))
            self.conn_ssh_key.setText(str(host.ssh_key_file or ""))
            self.conn_local_port.setValue(int(host.local_tunnel_port or 18765))
            self.conn_remote_port.setValue(int(host.remote_agent_port or 8765))
            self._sync_connection_fields(str(host.mode or "ssh"))
            return

        self.connection_context_label.setText("Connection settings for the current local/single-server manager connection.")
        c = self.cfg.connection
        self.conn_mode.setCurrentText(c.mode)
        self.conn_remote_url.setText(c.remote_url)
        self.conn_token.setText(c.remote_token)
        self.conn_verify_tls.setChecked(c.verify_tls)
        self.conn_ssh_host.setText(c.ssh_host)
        self.conn_ssh_user.setText(c.ssh_user)
        self.conn_ssh_port.setValue(c.ssh_port)
        self.conn_ssh_key.setText(c.ssh_key_file)
        self.conn_local_port.setValue(c.ssh_local_port)
        self.conn_remote_port.setValue(c.ssh_remote_agent_port)
        self._sync_connection_fields(c.mode)

    def _sync_connection_fields(self,mode):
        direct=mode=="direct"; ssh=mode=="ssh"; self.conn_remote_url.setEnabled(direct); self.conn_verify_tls.setEnabled(direct); self.conn_token.setEnabled(direct or ssh)
        for widget in (self.conn_ssh_host,self.conn_ssh_user,self.conn_ssh_port,self.conn_ssh_key,self.conn_local_port,self.conn_remote_port): widget.setEnabled(ssh)
        if ssh: self.conn_hint.setText("SSH Tunnel mode is recommended for off-network management. Direct Agent URL is ignored. The agent and Palworld REST API can remain bound to loopback/private interfaces.")
        elif direct: self.conn_hint.setText("Direct mode requires a TLS-protected manager agent URL and careful firewall configuration. Do not expose Palworld's built-in REST API directly to the Internet.")
        else: self.conn_hint.setText("Local mode treats this Windows/Linux computer as the Palworld server host.")

    def save_connection_page(self):
        host = self._active_host_config()
        if host is not None:
            host.mode = self.conn_mode.currentText()
            host.remote_url = self.conn_remote_url.text().strip()
            host.agent_token = self.conn_token.text()
            host.verify_tls = self.conn_verify_tls.isChecked()
            host.ssh_host = self.conn_ssh_host.text().strip()
            host.ssh_user = self.conn_ssh_user.text().strip()
            host.ssh_port = self.conn_ssh_port.value()
            host.ssh_key_file = self.conn_ssh_key.text().strip()
            host.local_tunnel_port = self.conn_local_port.value()
            host.remote_agent_port = self.conn_remote_port.value()
            save_config(self.cfg)

            reset = getattr(self.manager, "reset_host_connection", None)
            if reset is not None:
                try:
                    reset(host.id)
                except Exception:
                    pass

            def connected(info):
                hostname = str((info or {}).get("hostname") or host.ssh_host or host.name)
                self.show_notice(f"Connection settings saved for {host.name}; node connection verified ({hostname}).", "success", 6000)
                self.refresh_connection_page()
                if self._server_context_available():
                    self.refresh_header_status(silent=True)

            def failed(message):
                self.show_notice(
                    f"Connection settings were saved for {host.name}, but the node could not be verified: {message}",
                    "warning", 10000,
                )

            remote_for_host = getattr(self.manager, "remote_for_host", None)
            if remote_for_host is None:
                connected({})
                return
            self.show_notice(f"Saving and verifying connection for {host.name}…", "info", 0)
            self._run_async(
                f"reconnect-{host.id}",
                "Connection",
                lambda: remote_for_host(host.id).host_info(),
                connected,
                failed,
                silent=False,
            )
            return

        c = self.cfg.connection
        c.mode = self.conn_mode.currentText()
        c.remote_url = self.conn_remote_url.text().strip()
        c.remote_token = self.conn_token.text()
        c.verify_tls = self.conn_verify_tls.isChecked()
        c.ssh_host = self.conn_ssh_host.text().strip()
        c.ssh_user = self.conn_ssh_user.text().strip()
        c.ssh_port = self.conn_ssh_port.value()
        c.ssh_key_file = self.conn_ssh_key.text().strip()
        c.ssh_local_port = self.conn_local_port.value()
        c.ssh_remote_agent_port = self.conn_remote_port.value()
        save_config(self.cfg)
        old_manager = self.manager

        def connected(new_manager):
            self.manager = new_manager
            if hasattr(old_manager, "close"):
                try:
                    old_manager.close()
                except Exception:
                    pass
            self.show_notice("Connection settings saved and manager connection reopened.", "success")
            self.refresh_dashboard(silent=True)

        self.show_notice("Opening the manager connection…", "info", 0)
        self._run_async("reconnect", "Connection", lambda: manager_from_config(self.cfg), connected, silent=False)

    def _prepare_report(self, title, subtitle):
        if self.report_title_labels: self.report_title_labels[0].setText(title)
        if self.report_subtitle_labels: self.report_subtitle_labels[0].setText(subtitle)
        self._clear_layout(self.report_layout)
        try: self.report_action.clicked.disconnect()
        except Exception: pass
        self.report_action.hide()

    def _show_crash_history_report(self, data, recent_lines):
        self._prepare_report("Crash History", "Recent warning, error, and crash activity from the Palworld server logs.")
        grid = QGridLayout(); grid.setHorizontalSpacing(12); grid.setVerticalSpacing(12)
        metrics = (
            ("Error Lines", int(data.get("error_lines",0) or 0), "#ff7b86"),
            ("Warning Lines", int(data.get("warning_lines",0) or 0), "#ffc966"),
            ("Crash Markers", int(data.get("crash_markers",0) or 0), "#66d6ff"),
        )
        for i, (label, value, color) in enumerate(metrics):
            card = QFrame(); card.setObjectName("detailCard"); card.setMinimumHeight(125); card.setMaximumHeight(145)
            cl = QVBoxLayout(card); cl.setContentsMargins(18,14,18,14)
            v = QLabel(str(value)); v.setObjectName("reportMetricValue"); v.setStyleSheet(f"color:{color}; font-size:30px; font-weight:850;")
            l = QLabel(label); l.setObjectName("reportMetricLabel")
            cl.addWidget(v); cl.addWidget(l); cl.addStretch(1)
            grid.addWidget(card,0,i); grid.setColumnStretch(i,1)
        self.report_layout.addLayout(grid)
        crashes = int(data.get("crash_markers",0) or 0)
        status = QFrame(); status.setObjectName("panel"); sl = QHBoxLayout(status); sl.setContentsMargins(16,12,16,12)
        badge = PillBadge("NO CRASH MARKERS" if crashes == 0 else "CRASH MARKERS FOUND", "good" if crashes == 0 else "bad")
        sl.addWidget(badge); msg = QLabel("No fatal/crash/OOM markers were found in the inspected log window." if crashes == 0 else "Crash-related markers were detected. Review the recent log entries below."); msg.setObjectName("detailDescription"); msg.setWordWrap(True); sl.addWidget(msg,1)
        self.report_layout.addWidget(status)
        log_card = QFrame(); log_card.setObjectName("detailCard"); ll = QVBoxLayout(log_card); ll.setContentsMargins(14,12,14,12)
        title = QLabel("Recent Warning / Error Log Entries"); title.setObjectName("sectionTitle"); ll.addWidget(title)
        log = QPlainTextEdit(); log.setReadOnly(True); log.setLineWrapMode(QPlainTextEdit.NoWrap); log.setFixedHeight(300)
        log.setPlainText("\n".join(recent_lines[-80:]) if recent_lines else "No warning/error log entries were returned.")
        ll.addWidget(log); self.report_layout.addWidget(log_card)
        self.report_layout.addStretch(1); self.show_named_page("report")

    def _show_nondefault_settings_report(self, rows):
        self._prepare_report("Non-default Settings", "Human-readable view of settings that differ from DefaultPalWorldSettings.ini.")
        count_panel = QFrame(); count_panel.setObjectName("panel"); cp = QHBoxLayout(count_panel); cp.setContentsMargins(16,11,16,11)
        badge = PillBadge(f"{len(rows)} CHANGED", "warning" if rows else "good"); cp.addWidget(badge)
        msg = QLabel("These values override the Palworld defaults. Descriptions and accepted values are included so the differences are understandable."); msg.setObjectName("detailDescription"); msg.setWordWrap(True); cp.addWidget(msg,1)
        self.report_layout.addWidget(count_panel)
        table = QTableWidget(len(rows), 6)
        table.setHorizontalHeaderLabels(["Setting", "Technical Key", "What It Controls", "Default", "Current", "Available Values"])
        table.setEditTriggers(QTableWidget.NoEditTriggers); table.setSelectionBehavior(QTableWidget.SelectRows); table.verticalHeader().setVisible(False); table.setAlternatingRowColors(True); table.setWordWrap(True)
        for r, row in enumerate(rows):
            key = str(row.get("key", "")); current = row.get("current", "-"); default = row.get("default", "-")
            if key in SECRET_KEYS:
                current = "••••••••" if current not in (None, "", '""') else "Not set"
                default = "••••••••" if default not in (None, "", '""') else "Not set"
            vals = (
                display_name_for(key), key, description_for(key), default, current,
                allowed_values_for(key, str(row.get("current", "-"))),
            )
            for c, value in enumerate(vals): table.setItem(r,c,QTableWidgetItem(str(value)))
            table.setRowHeight(r, 54)
        hdr = table.horizontalHeader()
        hdr.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(2, QHeaderView.Stretch)
        hdr.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(4, QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(5, QHeaderView.Stretch)
        table.setMinimumHeight(280); table.setMaximumHeight(min(620, 58 + max(4,len(rows))*54))
        self.report_layout.addWidget(table)
        self.report_layout.addStretch(1); self.show_named_page("report")

    def show_report(self,title,subtitle,data,action_text=None,action_fn=None):
        self._prepare_report(title, subtitle)
        if isinstance(data, list):
            if data and all(isinstance(x,dict) for x in data):
                keys = []
                for item in data:
                    for key in item:
                        if key not in keys: keys.append(key)
                table = QTableWidget(len(data), len(keys)); table.setHorizontalHeaderLabels([self._human_label(k) for k in keys]); table.setEditTriggers(QTableWidget.NoEditTriggers); table.verticalHeader().setVisible(False); table.setAlternatingRowColors(True)
                for r,item in enumerate(data):
                    for c,key in enumerate(keys): table.setItem(r,c,QTableWidgetItem(self._friendly_value(item.get(key))))
                for c in range(len(keys)): table.horizontalHeader().setSectionResizeMode(c, QHeaderView.Stretch if c == 0 else QHeaderView.ResizeToContents)
                table.setMinimumHeight(260); self.report_layout.addWidget(table)
            else:
                grid = QGridLayout(); grid.setHorizontalSpacing(12); grid.setVerticalSpacing(12)
                for i,item in enumerate(data): grid.addWidget(self._make_kv_card(f"Item {i+1}", item), i//2, i%2)
                grid.setColumnStretch(0,1); grid.setColumnStretch(1,1); self.report_layout.addLayout(grid)
        elif isinstance(data, dict):
            grid = QGridLayout(); grid.setHorizontalSpacing(12); grid.setVerticalSpacing(12); card_index = 0
            scalar = {k:v for k,v in data.items() if not isinstance(v,(dict,list))}
            nested = [(k,v) for k,v in data.items() if isinstance(v,(dict,list))]
            if scalar:
                # Split large summaries into smaller readable cards.
                items = list(scalar.items())
                chunk_size = 5
                for start in range(0,len(items),chunk_size):
                    chunk = dict(items[start:start+chunk_size]); title_text = "Summary" if start == 0 else "Summary (continued)"
                    grid.addWidget(self._make_kv_card(title_text, chunk), card_index//2, card_index%2); card_index += 1
            for key,value in nested:
                if isinstance(value,list) and value and all(isinstance(x,dict) for x in value):
                    keys=[]
                    for item in value:
                        for k in item:
                            if k not in keys: keys.append(k)
                    card=QFrame(); card.setObjectName("detailCard"); cl=QVBoxLayout(card); cl.setContentsMargins(14,12,14,12)
                    t=QLabel(self._human_label(key)); t.setObjectName("sectionTitle"); cl.addWidget(t)
                    table=QTableWidget(len(value),len(keys)); table.setHorizontalHeaderLabels([self._human_label(k) for k in keys]); table.verticalHeader().setVisible(False); table.setEditTriggers(QTableWidget.NoEditTriggers); table.setAlternatingRowColors(True)
                    for r,item in enumerate(value):
                        for c,k in enumerate(keys): table.setItem(r,c,QTableWidgetItem(self._friendly_value(item.get(k))))
                    for c in range(len(keys)): table.horizontalHeader().setSectionResizeMode(c,QHeaderView.Stretch if c==0 else QHeaderView.ResizeToContents)
                    table.setMinimumHeight(230); cl.addWidget(table)
                    self.report_layout.addWidget(card)
                else:
                    grid.addWidget(self._make_kv_card(self._human_label(key),value), card_index//2, card_index%2); card_index += 1
            if card_index:
                grid.setColumnStretch(0,1); grid.setColumnStretch(1,1); self.report_layout.insertLayout(0, grid)
        else:
            self.report_layout.addWidget(self._make_kv_card(title,data))
        self.report_layout.addStretch(1)
        if action_text and action_fn:
            self.report_action.setText(action_text); self.report_action.clicked.connect(action_fn); self.report_action.show()
        self.show_named_page("report")

    def refresh_watchdog_page(self, silent=False):
        if self._overview_cache:
            self._apply_watchdog_overview(self._overview_cache)

        def done(data):
            self._apply_watchdog_overview(data)
            self._apply_header_status((data or {}).get("status", {}) or {})

        self._run_async("watchdog-snapshot", "Live Watchdog", self.manager.watchdog_snapshot, done, silent=silent)

    def _apply_watchdog_overview(self, data):
        data = data or {}
        status = data.get("status", {}) or {}
        health = data.get("health", {}) or {}
        service = status.get("service", {}) or {}
        process = status.get("process", {}) or {}
        self.watchdog_values["Server"].setText(self._display(status.get("server_name")))
        self.watchdog_values["Service"].setText(
            f"{service.get('state','-')}  •  Service PID {service.get('pid','-')}"
        )
        self.watchdog_values["Endpoint"].setText(
            f"{status.get('lan_ip','-')}:{status.get('game_port','-')}/UDP"
        )
        self.watchdog_values["Players"].setText(
            f"{self._display(status.get('current_players'))}/{self._display(status.get('max_players'))}"
            f"  •  Server FPS {self._display(status.get('server_fps'))}"
        )
        self.watchdog_values["Health"].setText(str(health.get("overall", "-")).upper())
        self.watchdog_values["Host CPU Load"].setText(
            f"{float(health.get('cpu_percent', 0) or 0):.1f}%  •  Entire Ubuntu server/VM across all logical CPUs"
        )
        self.watchdog_values["Host Memory"].setText(
            f"{human_bytes(health.get('memory_used'))} / {human_bytes(health.get('memory_total'))}"
            f"  •  {float(health.get('memory_percent', 0) or 0):.1f}% used"
        )
        self.watchdog_values["Storage"].setText(
            f"{human_bytes(health.get('disk_used'))} / {human_bytes(health.get('disk_total'))}"
            f"  •  {float(health.get('disk_percent', 0) or 0):.1f}% used"
        )
        pal_cpu = float(process.get("cpu_percent", 0) or 0)
        pal_pid = process.get("pid", "-")
        self.watchdog_values["PalServer Process"].setText(
            f"CPU {pal_cpu:.1f}%  •  Palworld process only  •  RAM {human_bytes(process.get('rss'))}  •  PID {pal_pid}  •  100% = one logical CPU core"
        )
        self.watchdog_values["Server Time"].setText(
            datetime.now().astimezone().strftime("%Y-%m-%d %I:%M:%S %p %Z")
        )
        self.watchdog_logs.setPlainText("\n".join((data.get("logs", []) or [])[-120:]))
        self.watchdog_logs.verticalScrollBar().setValue(self.watchdog_logs.verticalScrollBar().maximum())

    def _apply_server_setup_data(self, data):
        data = data or {}
        self.setup_install_dir.setText(str(data.get("install_dir","")))
        self.setup_steamcmd.setText(str(data.get("steamcmd_path","")))
        self.setup_steam_user.setText(str(data.get("steam_user","palworld")))
        self.setup_service_name.setText(str(data.get("service_name","")))
        self.setup_game_port.setValue(int(data.get("game_port",8211)))
        self.setup_rest_host.setText(str(data.get("rest_api_host","127.0.0.1")))
        self.setup_rest_port.setValue(int(data.get("rest_api_port",8212)))
        self.setup_rest_user.setText(str(data.get("rest_api_username","admin")))
        self.setup_admin_password.clear()
        self.setup_sync_rest.setChecked(True)

    def refresh_server_setup_page(self, silent=False):
        self._run_async("server-setup-page", "Server Setup", self.manager.server_config, self._apply_server_setup_data, silent=silent)

    def save_server_setup_page(self):
        payload = {
            "install_dir": self.setup_install_dir.text().strip(),
            "steamcmd_path": self.setup_steamcmd.text().strip(),
            "steam_user": self.setup_steam_user.text().strip(),
            "service_name": self.setup_service_name.text().strip(),
            "game_port": self.setup_game_port.value(),
            "rest_api_host": self.setup_rest_host.text().strip(),
            "rest_api_port": self.setup_rest_port.value(),
            "rest_api_username": self.setup_rest_user.text().strip(),
            "sync_palworld_rest": self.setup_sync_rest.isChecked(),
        }
        if self.setup_admin_password.text():
            payload["admin_password"] = self.setup_admin_password.text()

        def done(result):
            self.show_notice("Server configuration saved.", "success")
            self._apply_server_setup_data(result)
            if self._current_connection_mode() == "local":
                self.cfg = load_config()
            self.refresh_dashboard(silent=True)

        self._run_action_async("save-server-setup", "Server Setup", lambda: self.manager.update_server_config(payload), on_success=done)

    def run_tool(self,tool_id):
        # Report/diagnostic tools explicitly populate their destination. They
        # must not route through another page first.
        if tool_id == "compare":
            self.show_compare_defaults()
            return
        if tool_id == "crashes":
            self.show_crash_history()
            return
        if tool_id == "diagnostics":
            self.show_named_page("diagnostics")
            return
        if tool_id == "network":
            self.show_network_diagnostics()
            return
        target=self._tool_page_target(tool_id)
        if target:
            self.show_named_page(target)
            if tool_id=="broadcast": self.broadcast_text.setFocus()
            elif tool_id=="profiles": self.profile_combo.setFocus()
            elif tool_id=="search": self.setting_search.setFocus()
            return
        if tool_id in {"start","stop","restart"}: self.service(tool_id)
        elif tool_id=="save": self.save_world()
        elif tool_id=="update-check": self.update_check()
        elif tool_id=="update":
            def execute():
                data=self.call("Update Server",lambda:self.manager.update_server(True,True))
                if data is not None:self.show_notice("Palworld server update completed.","success");self.show_report("Update Complete","Backup, SteamCMD update, and restart result",data)
            self._confirm_then("server-update","Back up, stop, update/validate with SteamCMD, and restart the Palworld server?",execute)
        elif tool_id=="profiles": self.show_named_page("settings"); self.profile_combo.setFocus()
        elif tool_id=="crashes": self.show_crash_history()
        elif tool_id=="network": self.show_network_diagnostics()
        elif tool_id=="self-update": self.manager_self_update_check()
        else:self.show_notice(f"Tool '{tool_id}' is available from the integrated pages.","info")

    def manager_self_update_check(self):
        updater=SelfUpdater(self.cfg); data=self.call("Manager Update",updater.check)
        if data is None:return
        if data.get("state")=="available":
            def install():
                result=self.call("Manager Update",updater.install_latest)
                if result is not None:self.show_notice("PalServer Manager update installed. Restart the application to use it.","success",10000);self.show_report("Manager Updated","Self-update installation result",result)
            self.show_report("Manager Update",f"Version {data.get('latest')} is available",data,"Install Manager Update",lambda:self._confirm_then("manager-self-update","Install the available PalServer Manager update?",install))
        else:self.show_report("Manager Update","Self-update status",data)


def main() -> None:
    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setStyle("Fusion")
    window = MainWindow()
    window.show()
    if QApplication.instance() is app:
        raise SystemExit(app.exec())


if __name__ == "__main__":
    main()
