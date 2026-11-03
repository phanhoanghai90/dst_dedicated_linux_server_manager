"""
DST Launcher - app Windows (PyQt5) thay cho vox-launcher.

Quản lý Don't Starve Together Dedicated Server ngay trên máy (cùng lõi với web panel: app.py)
và liên kết với web panel trên VPS (remote_link.py):
  - "Đồng bộ": lấy cấu hình + thế giới của các server trên web về máy.
  - Mở app: dừng server trên VPS, đồng bộ thế giới 2 chiều theo chu kỳ (mặc định 10 phút).
  - Đóng app: lưu, đồng bộ lần cuối, bật lại server trên VPS.

    python dst_desktop.py
"""

import json
import os
import sys
import threading
import time
from datetime import datetime
from pathlib import Path


def _base_dir():
    if os.environ.get("DST_BASE"):
        return Path(os.environ["DST_BASE"])
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA", Path.home())) / "DSTLauncher"
    return Path.home() / ".dstlauncher"


os.environ.setdefault("DST_BASE", str(_base_dir()))
BASE = Path(os.environ["DST_BASE"])
BASE.mkdir(parents=True, exist_ok=True)

# Bản exe --windowed không có console: stdout/stderr là None, ghi ra file log.
if sys.stdout is None or sys.stderr is None:
    _logfile = open(BASE / "launcher.log", "a", encoding="utf-8", buffering=1)
    sys.stdout = sys.stdout or _logfile
    sys.stderr = sys.stderr or _logfile

from fastapi import HTTPException  # noqa: E402
from PyQt5.QtCore import QObject, Qt, QTimer, QUrl, pyqtSignal  # noqa: E402
from PyQt5.QtGui import QColor, QDesktopServices, QFont, QIcon, QPalette  # noqa: E402
from PyQt5.QtWidgets import (  # noqa: E402
    QAbstractItemView, QApplication, QCheckBox, QComboBox, QDialog, QFileDialog, QFormLayout, QFrame, QGridLayout,
    QGroupBox, QHBoxLayout, QHeaderView, QInputDialog, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMainWindow,
    QMessageBox, QPlainTextEdit, QPushButton, QScrollArea, QSpinBox, QSplitter, QStackedWidget, QTableWidget,
    QTableWidgetItem, QTabWidget, QVBoxLayout, QWidget,
)

import app as core  # noqa: E402
import remote_link  # noqa: E402

APP_NAME = "DST Launcher"
# Bản exe giải nén assets/ vào sys._MEIPASS.
ICON_FILE = Path(getattr(sys, "_MEIPASS", Path(__file__).parent)) / "assets" / "icon.png"

STATUS_TEXT = {"online": "Online", "starting": "Đang khởi động", "stopping": "Đang tắt", "offline": "Offline"}
STATUS_COLOR = {"online": "#6fbf73", "starting": "#e0a03a", "stopping": "#e0a03a", "offline": "#a59a87"}
SEASON_TEXT = {"autumn": "Thu", "winter": "Đông", "spring": "Xuân", "summer": "Hạ"}
PHASE_TEXT = {"day": "Ban ngày", "dusk": "Chạng vạng", "night": "Ban đêm"}

STYLE = """
* { font-size: 10pt; }
QMainWindow, QWidget { background: #1b1814; color: #ece3d3; }
QGroupBox { border: 1px solid #433b2f; border-radius: 8px; margin-top: 14px; padding: 10px; background: #26221c; }
QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 4px; color: #d9a441; font-weight: bold; }
QGroupBox QWidget { background: transparent; }
QLineEdit, QSpinBox, QComboBox, QPlainTextEdit, QListWidget, QTableWidget {
    background: #2f2a22; border: 1px solid #433b2f; border-radius: 5px; padding: 4px; color: #ece3d3;
    selection-background-color: #5a4a2a; }
QPlainTextEdit#log { background: #110f0c; font-family: Consolas, "DejaVu Sans Mono", monospace; font-size: 9pt; }
QPushButton { background: #2f2a22; border: 1px solid #433b2f; border-radius: 5px; padding: 6px 14px; }
QPushButton:hover { border-color: #d9a441; }
QPushButton:disabled { color: #6b6253; }
QPushButton[kind="primary"] { background: #d9a441; color: #1b1814; border-color: #d9a441; font-weight: bold; }
QPushButton[kind="primary"]:disabled { background: #6b5a35; color: #2f2a22; }
QPushButton[kind="danger"] { color: #e06a5a; border-color: #e06a5a; }
QListWidget#nav { background: #211d18; border: none; border-right: 1px solid #433b2f; padding: 6px; font-size: 11pt; }
QListWidget#nav::item { padding: 8px 10px; border-radius: 6px; }
QListWidget#nav::item:selected { background: #2f2a22; color: #d9a441; }
QTabWidget::pane { border: 1px solid #433b2f; border-radius: 6px; top: -1px; }
QTabBar::tab { background: transparent; color: #a59a87; padding: 7px 14px; border: 1px solid transparent; }
QTabBar::tab:selected { color: #ece3d3; background: #26221c; border-color: #433b2f; border-bottom-color: #26221c; }
QHeaderView::section { background: #26221c; color: #a59a87; border: none; border-bottom: 1px solid #433b2f; padding: 5px; }
QLabel[kind="muted"] { color: #a59a87; }
QLabel[kind="warn"] { color: #e0a03a; }
QLabel[kind="title"] { font-size: 14pt; font-weight: bold; color: #d9a441; }
QStatusBar { background: #211d18; color: #a59a87; }
QScrollArea { border: none; }
QCheckBox::indicator, QListWidget::indicator { width: 14px; height: 14px; border: 1px solid #8a7d68; border-radius: 3px; background: #2f2a22; }
QCheckBox::indicator:checked, QListWidget::indicator:checked { background: #d9a441; border-color: #d9a441; }
"""


# ------------------------------------------------------------------------------------ #
# Tiện ích

class Bridge(QObject):
    call = pyqtSignal(object)


bridge = Bridge()
bridge.call.connect(lambda fn: fn())
main_window = None


def err_text(e):
    if isinstance(e, HTTPException):
        return str(e.detail)
    return str(e) or e.__class__.__name__


def show_error(e):
    QMessageBox.warning(main_window, APP_NAME, err_text(e))


def toast(text):
    if main_window:
        main_window.statusBar().showMessage(text, 8000)


def guard(fn, ok=None):
    """Gọi fn ngay (việc nhanh), báo lỗi bằng hộp thoại."""
    try:
        result = fn()
    except Exception as e:  # noqa: BLE001
        show_error(e)
        return None
    if ok:
        toast(ok)
    return result


def run_async(fn, on_done=None, on_error=None):
    """Chạy fn ở thread nền, gọi lại on_done/on_error trên thread giao diện."""

    def work():
        try:
            result = fn()
        except Exception as e:  # noqa: BLE001
            bridge.call.emit(lambda: (on_error or show_error)(e))
            return
        if on_done:
            bridge.call.emit(lambda: on_done(result))

    threading.Thread(target=work, daemon=True).start()


def confirm(text):
    return QMessageBox.question(main_window, APP_NAME, text) == QMessageBox.Yes


def button(text, slot=None, kind=None):
    b = QPushButton(text)
    if kind:
        b.setProperty("kind", kind)
    if slot:
        b.clicked.connect(slot)
    return b


def label(text="", kind=None, wrap=True):
    lb = QLabel(text)
    lb.setWordWrap(wrap)
    lb.setTextInteractionFlags(Qt.TextSelectableByMouse)
    if kind:
        lb.setProperty("kind", kind)
    return lb


def row(*widgets, stretch=True):
    box = QHBoxLayout()
    for w in widgets:
        if w is None:
            box.addStretch(1)
        elif isinstance(w, QWidget):
            box.addWidget(w)
        else:
            box.addLayout(w)
    if stretch and None not in widgets:
        box.addStretch(1)
    return box


def log_view(height=None):
    view = QPlainTextEdit()
    view.setObjectName("log")
    view.setReadOnly(True)
    view.setMaximumBlockCount(5000)
    if height:
        view.setMinimumHeight(height)
    return view


def append_lines(view, lines):
    if not lines:
        return
    bar = view.verticalScrollBar()
    at_bottom = bar.value() >= bar.maximum() - 4
    view.appendPlainText("\n".join(lines))
    if at_bottom:
        bar.setValue(bar.maximum())


def status_html(status):
    return f'<span style="color:{STATUS_COLOR.get(status, "#a59a87")};font-weight:bold">{STATUS_TEXT.get(status, status)}</span>'


def fmt_time(ts):
    return datetime.fromtimestamp(ts).strftime("%H:%M %d/%m/%Y") if ts else "—"


def same(a, b):
    """So sánh giá trị tuỳ chọn mod giống Lua: true khác 1, 1 bằng 1.0."""
    if isinstance(a, bool) or isinstance(b, bool):
        return type(a) is type(b) and a == b
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return a == b
    return type(a) is type(b) and a == b


def text_editor_dialog(title, content, parent=None):
    dlg = QDialog(parent or main_window)
    dlg.setWindowTitle(title)
    dlg.resize(760, 560)
    edit = QPlainTextEdit(content)
    edit.setObjectName("log")
    lay = QVBoxLayout(dlg)
    lay.addWidget(edit)
    ok, cancel = button("Lưu", dlg.accept, "primary"), button("Huỷ", dlg.reject)
    lay.addLayout(row(None, cancel, ok))
    return edit.toPlainText() if dlg.exec_() == QDialog.Accepted else None


# ------------------------------------------------------------------------------------ #
# Trang chủ

class HomePage(QWidget):
    open_cluster = pyqtSignal(str)
    clusters_changed = pyqtSignal()

    def __init__(self):
        super().__init__()
        self.install_seq = 0
        lay = QVBoxLayout(self)

        self.warn = label(kind="warn")
        lay.addWidget(self.warn)

        # Danh sách server
        box = QGroupBox("Server trên máy này")
        b = QVBoxLayout(box)
        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(["Tên", "Trạng thái", "Người chơi", "Thế giới", "Port", "Mod"])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.doubleClicked.connect(lambda _: self._open())
        b.addWidget(self.table)
        self.mem = label(kind="muted", wrap=False)
        b.addLayout(row(button("Bật", self._start, "primary"), button("Tắt", self._stop), button("Mở chi tiết", self._open),
                        button("Xoá", self._delete, "danger"), None, self.mem))
        lay.addWidget(box, 2)

        bottom = QHBoxLayout()

        create = QGroupBox("Tạo server mới")
        c = QVBoxLayout(create)
        self.new_name = QLineEdit()
        self.new_name.setPlaceholderText("Ví dụ: Server bạn bè")
        self.copy_token = QCheckBox("Dùng cluster token của server đã có")
        self.copy_token.setChecked(True)
        c.addWidget(label("Tên server"))
        c.addWidget(self.new_name)
        c.addWidget(self.copy_token)
        c.addWidget(label("Server mới được tự cấp port riêng. Muốn lấy server từ VPS: vào mục Liên kết VPS.", "muted"))
        c.addLayout(row(button("Tạo server", self._create, "primary")))
        c.addStretch(1)
        bottom.addWidget(create, 1)

        game = QGroupBox("Game server (dùng chung)")
        g = QVBoxLayout(game)
        self.install_state = label()
        g.addWidget(self.install_state)
        g.addWidget(label("Cài hoặc cập nhật DST Dedicated Server qua steamcmd (khoảng 3 GB). Phải tắt tất cả server trước.", "muted"))
        self.btn_install = button("Cài đặt / Cập nhật", self._install, "primary")
        g.addLayout(row(self.btn_install, button("Mở thư mục dữ liệu", lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(core.BASE_DIR))))))
        self.install_log = log_view(120)
        g.addWidget(self.install_log)
        bottom.addWidget(game, 1)

        lay.addLayout(bottom, 1)

    def selected(self):
        r = self.table.currentRow()
        if r < 0:
            toast("Chọn một server trong danh sách trước.")
            return None
        return core.registry.clusters.get(self.table.item(r, 0).data(Qt.UserRole))

    def refresh(self):
        clusters = core.registry.all()
        current = self.table.currentRow()
        self.table.setRowCount(len(clusters))

        for i, cluster in enumerate(clusters):
            s = cluster.summary()
            m, cv = s["shards"]["Master"], s["shards"]["Caves"]
            stats = s["stats"] if m["status"] == "online" else None
            status = STATUS_TEXT.get(m["status"], m["status"]) + (" · đang xử lý" if s["busy"] else "")
            err = m["error"] or cv["error"] or ("Chưa có token" if not s["token_set"] else "")
            values = [
                s["name"], status + (f" · {err}" if err else ""),
                f"{len(stats['players'])}/{stats['max']}" if stats else "—",
                f"Ngày {stats['day']} · {SEASON_TEXT.get(stats['season'], stats['season'])}" if stats else "—",
                f"{s['ports']['Master']}" + (f" / {s['ports']['Caves']}" if s["caves_enabled"] else ""),
                str(s["mods_count"]),
            ]
            for j, v in enumerate(values):
                item = self.table.item(i, j)
                if item is None:
                    item = QTableWidgetItem()
                    self.table.setItem(i, j, item)
                item.setText(v)
                if j == 0:
                    item.setData(Qt.UserRole, cluster.id)
                if j == 1:
                    item.setForeground(QColor("#e06a5a" if err else STATUS_COLOR.get(m["status"], "#a59a87")))

        if 0 <= current < len(clusters):
            self.table.selectRow(current)

        mem = core.memory_info()
        if mem.get("available_mb"):
            self.mem.setText(f"RAM máy: còn trống {mem['available_mb']} / {mem['total_mb']} MB")

        installed = core.SERVER_EXE.exists()
        job = core.install_job
        self.install_state.setText(
            "Trạng thái: " + ("<b>Đang cài...</b>" if job.running else f"<b>Đã cài</b> (build {core.installed_build() or '?'})" if installed else "<b>Chưa cài</b>"))
        self.btn_install.setEnabled(not job.running)
        new = [(seq, text) for seq, text in list(job.lines) if seq > self.install_seq]
        if new:
            self.install_seq = new[-1][0]
            append_lines(self.install_log, [t for _, t in new])

        warns = []
        if not installed and not job.running:
            warns.append("Chưa cài game server: bấm “Cài đặt / Cập nhật” bên dưới.")
        running = sum(1 for c in clusters if c.any_running())
        if running > 1:
            warns.append(f"Đang chạy {running} server cùng lúc, chú ý RAM (mỗi server có Caves cần khoảng 1–1.5 GB).")
        self.warn.setText("\n".join("• " + w for w in warns))
        self.warn.setVisible(bool(warns))

    def _start(self):
        cluster = self.selected()
        if cluster:
            guard(lambda: core.server_start(cluster), "Đang bật server...")

    def _stop(self):
        cluster = self.selected()
        if cluster:
            guard(lambda: core.server_stop(cluster), "Đang tắt server...")

    def _open(self):
        cluster = self.selected()
        if cluster:
            self.open_cluster.emit(cluster.id)

    def _delete(self):
        cluster = self.selected()
        if cluster and confirm(f"Xoá server “{cluster.name()}”? Thế giới được sao lưu trước khi xoá."):
            backup = guard(lambda: core.registry.delete(cluster))
            if backup:
                toast(f"Đã xoá. Bản sao lưu: {backup}")
                self.clusters_changed.emit()

    def _create(self):
        name = self.new_name.text().strip()
        if not name:
            toast("Nhập tên server.")
            return
        cluster = guard(lambda: core.registry.create(name, self.copy_token.isChecked()))
        if cluster:
            self.new_name.clear()
            self.clusters_changed.emit()
            self.open_cluster.emit(cluster.id)

    def _install(self):
        if guard(core.install_job.start, "Đang cài đặt game server..."):
            pass


# ------------------------------------------------------------------------------------ #
# Trang liên kết VPS

class RemotePage(QWidget):
    clusters_changed = pyqtSignal()

    def __init__(self):
        super().__init__()
        self.log_seq = 0
        self.last_names = None
        lay = QVBoxLayout(self)

        intro = label(
            "Nhập địa chỉ và tài khoản web panel trên VPS. Bấm <b>Đồng bộ</b> để lấy cấu hình (token, mod, Caves...) "
            "và thế giới của mọi server trên web về máy này, khỏi phải tạo lại.<br>"
            "Khi mở app: dừng các server đó trên VPS (bỏ qua nếu đang có người chơi), rồi đồng bộ thế giới 2 chiều theo chu kỳ. "
            "Khi đóng app: lưu, đồng bộ lần cuối rồi bật lại server trên VPS.", "muted")
        intro.setTextFormat(Qt.RichText)
        lay.addWidget(intro)

        box = QGroupBox("Kết nối")
        form = QFormLayout(box)
        self.url = QLineEdit()
        self.url.setPlaceholderText("http://1.2.3.4:8080")
        self.user = QLineEdit()
        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.Password)
        self.interval = QSpinBox()
        self.interval.setRange(1, 1440)
        self.interval.setSuffix(" phút")
        self.stop_remote = QCheckBox("Dừng server trên VPS trước khi bật server ở máy này (không dừng nếu đang có người chơi)")
        self.restart_remote = QCheckBox("Bật lại server trên VPS khi đóng app (hoặc khi app mất kết nối quá 5 phút)")
        form.addRow("Địa chỉ web panel", self.url)
        form.addRow("Tài khoản", self.user)
        form.addRow("Mật khẩu", self.password)
        form.addRow("Đồng bộ mỗi", self.interval)
        form.addRow("", self.stop_remote)
        form.addRow("", self.restart_remote)
        self.btn_save = button("Lưu && kiểm tra kết nối", self._save)
        form.addRow("", row(self.btn_save))
        lay.addWidget(box)

        act = QGroupBox("Đồng bộ")
        a = QVBoxLayout(act)
        self.badge = label()
        self.info = label()
        self.info.setTextFormat(Qt.RichText)
        self.btn_pull = button("Đồng bộ (lấy cấu hình + thế giới)", lambda: self._job(remote_link.pull_configs, "Lấy cấu hình và đồng bộ"), "primary")
        self.btn_sync = button("Đồng bộ thế giới ngay", lambda: self._job(remote_link.sync_all, "Đồng bộ thế giới"))
        self.btn_stop = button("Dừng server VPS", self._stop_remote)
        self.btn_start = button("Bật lại server VPS", lambda: self._job(remote_link.restart_remote_servers, "Bật lại server trên VPS"))
        a.addWidget(self.badge)
        a.addLayout(row(self.btn_pull, self.btn_sync, self.btn_stop, self.btn_start))
        a.addWidget(self.info)
        a.addWidget(label("Nhật ký đồng bộ", "muted"))
        self.log = log_view(160)
        a.addWidget(self.log, 1)
        lay.addWidget(act, 1)

        self.load_form()

    def load_form(self):
        r = remote_link.status_payload()
        self.url.setText(r["url"])
        self.user.setText(r["username"])
        self.password.clear()
        self.password.setPlaceholderText("(giữ nguyên)" if r["has_password"] else "")
        self.interval.setValue(r["interval_min"])
        self.stop_remote.setChecked(r["stop_remote"])
        self.restart_remote.setChecked(r["restart_remote_on_exit"])

    def refresh(self):
        r = remote_link.status_payload(self.log_seq)
        if r["job"]:
            self.badge.setText(f'<b style="color:#e0a03a">{r["job"]}...</b>')
        elif r["clusters"]:
            self.badge.setText(f'<b style="color:#6fbf73">Đã liên kết {len(r["clusters"])} server</b>')
        else:
            self.badge.setText("<b>Chưa lấy cấu hình</b>" if r["configured"] else "<b>Chưa liên kết</b>")

        lines = []
        for c in r["clusters"]:
            if c.get("time"):
                color = "#ece3d3" if c["ok"] else "#e06a5a"
                lines.append(f'<b>{c["name"]}</b>: <span style="color:{color}">{"✓" if c["ok"] else "✗"} {c["message"]}</span> '
                             f'<span style="color:#a59a87">({fmt_time(c["time"])})</span>')
            else:
                lines.append(f'<b>{c["name"]}</b>: chưa đồng bộ')
        if r["next_sync"] and not r["job"]:
            lines.append(f"Lần đồng bộ tới: {fmt_time(r['next_sync'])}")
        if r["stopped_by_app"]:
            lines.append("Server VPS đang được app dừng: " + ", ".join(r["stopped_by_app"]))
        self.info.setText("<br>".join(lines))

        busy = bool(r["job"])
        for b in (self.btn_pull, self.btn_sync, self.btn_stop, self.btn_start):
            b.setEnabled(r["configured"] and not busy)
        self.btn_save.setEnabled(not busy)

        if r["log"]:
            append_lines(self.log, [item["text"] for item in r["log"]])
        self.log_seq = r["seq"]

        names = tuple(sorted(core.registry.clusters))
        if names != self.last_names:
            self.last_names = names
            self.clusters_changed.emit()

    def _save(self):
        args = (self.url.text(), self.user.text(), self.password.text(), self.interval.value(),
                self.stop_remote.isChecked(), self.restart_remote.isChecked())
        self.btn_save.setEnabled(False)

        def done(count):
            self.btn_save.setEnabled(True)
            self.load_form()
            toast(f"Kết nối được! Trên VPS có {count} server.")

        def fail(e):
            self.btn_save.setEnabled(True)
            show_error(e)

        run_async(lambda: remote_link.apply_settings(*args), done, fail)

    def _job(self, fn, name):
        guard(lambda: remote_link.run_job(name, fn), name + "...")

    def _stop_remote(self):
        if confirm("Dừng các server liên kết trên VPS? (Bỏ qua server đang có người chơi.)"):
            self._job(remote_link.stop_remote_servers, "Dừng server trên VPS")


# ------------------------------------------------------------------------------------ #
# Trang một server

class DashboardTab(QWidget):
    def __init__(self, page):
        super().__init__()
        self.page = page
        self.log_seq = 0
        self.log_shard = "Master"
        lay = QVBoxLayout(self)

        top = QGroupBox("Trạng thái")
        t = QGridLayout(top)
        self.st_master, self.st_caves, self.st_info, self.st_players = label(), label(), label(kind="muted"), label()
        self.caves = QCheckBox("Bật Caves")
        self.caves.setToolTip("Tắt Caves tiết kiệm khoảng 1–1.5 GB RAM")
        self.caves.clicked.connect(self._caves)
        self.btn_start = button("Bật", lambda: guard(lambda: core.server_start(self.page.cluster), "Đang bật server..."), "primary")
        self.btn_stop = button("Tắt", lambda: guard(lambda: core.server_stop(self.page.cluster), "Đang tắt server..."))
        self.btn_restart = button("Khởi động lại", lambda: guard(lambda: core.server_restart(self.page.cluster), "Đang khởi động lại..."))
        t.addWidget(label("Master:"), 0, 0)
        t.addWidget(self.st_master, 0, 1)
        t.addWidget(label("Caves:"), 0, 2)
        t.addWidget(self.st_caves, 0, 3)
        t.addWidget(self.caves, 0, 4)
        t.addLayout(row(self.btn_start, self.btn_stop, self.btn_restart), 0, 5)
        t.setColumnStretch(5, 1)
        t.addWidget(self.st_players, 1, 0, 1, 6)
        t.addWidget(self.st_info, 2, 0, 1, 6)
        lay.addWidget(top)

        act = QGroupBox("Thao tác thế giới")
        a = QHBoxLayout(act)
        self.rollback_n = QSpinBox()
        self.rollback_n.setRange(1, 10)
        self.announce = QLineEdit()
        self.announce.setPlaceholderText("Thông báo tới người chơi...")
        self.announce.returnPressed.connect(self._announce)
        a.addWidget(button("Lưu (c_save)", lambda: self._action("save", None, "Đã lưu thế giới")))
        a.addWidget(label("Rollback"))
        a.addWidget(self.rollback_n)
        a.addWidget(button("bản", lambda: self._rollback()))
        a.addWidget(button("Tạo lại thế giới", self._regenerate, "danger"))
        a.addWidget(self.announce, 1)
        a.addWidget(button("Gửi thông báo", self._announce))
        lay.addWidget(act)

        logbox = QGroupBox("Log và console")
        lg = QVBoxLayout(logbox)
        self.shard_pick = QComboBox()
        self.shard_pick.addItems(["Master", "Caves"])
        self.shard_pick.currentTextChanged.connect(self._switch_log)
        self.log = log_view()
        self.cmd = QLineEdit()
        self.cmd.setPlaceholderText("Lệnh console, ví dụ: c_listallplayers()")
        self.cmd.returnPressed.connect(self._send)
        lg.addLayout(row(label("Shard:"), self.shard_pick))
        lg.addWidget(self.log, 1)
        lg.addLayout(row(self.cmd, button("Gửi", self._send), stretch=False))
        lay.addWidget(logbox, 1)

    def reset(self):
        self.log.clear()
        self.log_seq = 0

    def _switch_log(self, shard):
        self.log_shard = shard
        self.reset()
        self.refresh()

    def refresh(self):
        cluster = self.page.cluster
        s = cluster.summary()
        m, c = s["shards"]["Master"], s["shards"]["Caves"]
        self.st_master.setText(status_html(m["status"]) + (f' · {m["memory_mb"]} MB' if m["memory_mb"] else ""))
        self.st_caves.setText(status_html(c["status"]) + (f' · {c["memory_mb"]} MB' if c["memory_mb"] else ""))
        self.caves.setChecked(s["caves_enabled"])
        running = m["status"] != "offline" or c["status"] != "offline"
        self.btn_start.setEnabled(not running and not s["busy"])
        self.btn_stop.setEnabled(running)
        self.btn_restart.setEnabled(running and not s["busy"])

        stats = s["stats"] if m["status"] == "online" else None
        if stats:
            names = ", ".join(stats["players"]) or "không có ai"
            self.st_players.setText(
                f"👥 {len(stats['players'])}/{stats['max']} ({names}) · Ngày {stats['day']} · "
                f"{SEASON_TEXT.get(stats['season'], stats['season'])} · {PHASE_TEXT.get(stats['phase'], stats['phase'])}")
        else:
            self.st_players.setText("")
        errors = [e for e in (m["error"], c["error"]) if e]
        if not s["token_set"]:
            errors.append("Chưa có cluster token → tab Token.")
        info = f"Port {s['ports']['Master']}" + (f" / {s['ports']['Caves']}" if s["caves_enabled"] else "") + f" · thư mục {s['id']}"
        self.st_info.setText(info + "".join(f'<br><span style="color:#e06a5a">{e}</span>' for e in errors))
        self.st_info.setTextFormat(Qt.RichText)

        lines, seq = cluster.shards[self.log_shard].get_lines(self.log_seq)
        self.log_seq = seq
        append_lines(self.log, [line["text"] for line in lines])

    def _caves(self, checked):
        guard(lambda: core.caves_toggle(core.CavesToggle(enabled=checked), self.page.cluster), "Đã bật Caves" if checked else "Đã tắt Caves")

    def _action(self, name, value, ok):
        guard(lambda: core.action(core.Action(action=name, value=value), self.page.cluster), ok)

    def _rollback(self):
        n = self.rollback_n.value()
        if confirm(f"Rollback {n} bản lưu? Tiến độ sau bản đó sẽ mất."):
            self._action("rollback", n, f"Đang rollback {n} bản...")

    def _regenerate(self):
        if confirm("Xoá thế giới hiện tại và tạo thế giới mới? Không thể hoàn tác!"):
            self._action("regenerate", None, "Đang tạo lại thế giới...")

    def _announce(self):
        text = self.announce.text().strip()
        if text:
            self._action("announce", text, "Đã gửi thông báo")
            self.announce.clear()

    def _send(self):
        command = self.cmd.text().strip()
        if command and guard(lambda: core.console(core.ConsoleCommand(shard=self.log_shard, command=command), self.page.cluster)):
            self.cmd.clear()


class ConfigTab(QWidget):
    def __init__(self, page):
        super().__init__()
        self.page = page
        lay = QVBoxLayout(self)
        box = QGroupBox("Cấu hình server (cluster.ini)")
        form = QFormLayout(box)
        self.f = {
            "cluster_name": QLineEdit(), "cluster_description": QLineEdit(), "cluster_password": QLineEdit(),
            "max_players": QSpinBox(), "game_mode": QComboBox(), "cluster_intention": QComboBox(),
            "cluster_language": QLineEdit(), "max_snapshots": QSpinBox(), "master_port": QSpinBox(), "caves_port": QSpinBox(),
            "pvp": QCheckBox("Bật PvP"), "pause_when_empty": QCheckBox("Tạm dừng khi không có ai"),
            "caves_enabled": QCheckBox("Bật Caves (hang động)"),
        }
        self.f["max_players"].setRange(1, 64)
        self.f["max_snapshots"].setRange(1, 20)
        for key in ("master_port", "caves_port"):
            self.f[key].setRange(1024, 65535)
        self.f["game_mode"].addItems(["survival", "endless", "wilderness"])
        self.f["cluster_intention"].addItems(["cooperative", "social", "competitive", "madness"])
        self.f["cluster_password"].setPlaceholderText("để trống nếu không cần")
        for text, key in (("Tên server", "cluster_name"), ("Mô tả", "cluster_description"), ("Mật khẩu", "cluster_password"),
                          ("Số người tối đa", "max_players"), ("Chế độ chơi", "game_mode"), ("Phong cách", "cluster_intention"),
                          ("Ngôn ngữ", "cluster_language"), ("Số bản lưu giữ lại", "max_snapshots"),
                          ("Port UDP Master", "master_port"), ("Port UDP Caves", "caves_port")):
            form.addRow(text, self.f[key])
        for key in ("pvp", "pause_when_empty", "caves_enabled"):
            form.addRow("", self.f[key])
        form.addRow("", label("Thay đổi có hiệu lực sau khi khởi động lại server.", "muted"))
        form.addRow("", row(button("Lưu", self._save, "primary")))
        lay.addWidget(box)
        lay.addStretch(1)

    def load(self):
        data = core.get_settings(self.page.cluster)
        for key, w in self.f.items():
            value = data.get(key)
            if isinstance(w, QLineEdit):
                w.setText(str(value or ""))
            elif isinstance(w, QSpinBox):
                w.setValue(int(value or 0))
            elif isinstance(w, QComboBox):
                if w.findText(str(value)) < 0:
                    w.addItem(str(value))
                w.setCurrentText(str(value))
            else:
                w.setChecked(bool(value))

    def _save(self):
        data = {}
        for key, w in self.f.items():
            data[key] = (w.text() if isinstance(w, QLineEdit) else w.value() if isinstance(w, QSpinBox)
                         else w.currentText() if isinstance(w, QComboBox) else w.isChecked())
        if guard(lambda: core.put_settings(core.ClusterSettings(**data), self.page.cluster), "Đã lưu cấu hình"):
            self.page.title_changed()


class TokenTab(QWidget):
    def __init__(self, page):
        super().__init__()
        self.page = page
        lay = QVBoxLayout(self)
        box = QGroupBox("Cluster token")
        b = QVBoxLayout(box)
        self.state = label()
        self.token = QLineEdit()
        self.token.setPlaceholderText("Dán token vào đây (pds-g^...)")
        b.addWidget(self.state)
        b.addWidget(self.token)
        b.addWidget(label("Lấy token: trong game vào Account → Generate Server Token, hoặc trang "
                          "accounts.klei.com → Games → Game Servers. Server lấy từ VPS đã có sẵn token.", "muted"))
        b.addLayout(row(button("Lưu token", self._save, "primary")))
        lay.addWidget(box)
        lay.addStretch(1)

    def load(self):
        t = core.get_token(self.page.cluster)
        self.state.setText(f"Đã có token: <code>{t['preview']}</code>" if t["set"] else '<span style="color:#e06a5a">Chưa có token.</span>')
        self.token.clear()

    def _save(self):
        if guard(lambda: core.put_token(core.Token(token=self.token.text()), self.page.cluster), "Đã lưu token"):
            self.load()


class ModsTab(QWidget):
    def __init__(self, page):
        super().__init__()
        self.page = page
        self.mods, self.edits, self.current = [], {}, None
        lay = QVBoxLayout(self)

        self.add_input = QLineEdit()
        self.add_input.setPlaceholderText("ID mod hoặc link Workshop, có thể nhập nhiều, cách nhau bởi dấu phẩy")
        self.add_input.returnPressed.connect(self._add)
        lay.addLayout(row(self.add_input, button("Thêm mod", self._add), button("Tải mod (steamcmd)", self._download),
                          button("Sửa modoverrides.lua", self._raw), stretch=False))
        self.note = label(kind="muted")
        lay.addWidget(self.note)

        split = QSplitter()
        left = QWidget()
        l = QVBoxLayout(left)
        l.setContentsMargins(0, 0, 0, 0)
        self.list = QListWidget()
        self.list.currentRowChanged.connect(self._select)
        self.list.itemChanged.connect(self._toggle)
        l.addWidget(self.list, 1)
        l.addLayout(row(button("Xoá khỏi danh sách", self._remove, "danger")))
        split.addWidget(left)

        right = QWidget()
        r = QVBoxLayout(right)
        r.setContentsMargins(8, 0, 0, 0)
        self.title = label(kind="title")
        self.meta = label(kind="muted")
        self.desc = QPlainTextEdit()
        self.desc.setReadOnly(True)
        self.desc.setMaximumHeight(90)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        r.addWidget(self.title)
        r.addWidget(self.meta)
        r.addWidget(self.desc)
        r.addWidget(self.scroll, 1)
        split.addWidget(right)
        split.setSizes([320, 680])
        lay.addWidget(split, 1)

        self.btn_save = button("Lưu cấu hình mod", self._save, "primary")
        lay.addLayout(row(self.btn_save, label("Bật/tắt mod bằng ô đánh dấu. Khởi động lại server để áp dụng.", "muted", wrap=False)))

    def load(self, keep_edits=False):
        data = core.get_modconfig(self.page.cluster)
        self.mods = data["mods"]
        if not keep_edits:
            self.edits = {}
        missing = sum(1 for m in self.mods if not m["installed"])
        notes = [data["error"]] if data["error"] else []
        if missing:
            notes.append(f"{missing} mod chưa tải: bấm “Tải mod” để đọc được tuỳ chọn cấu hình.")
        self.note.setText(" ".join(notes))

        self.list.blockSignals(True)
        current = self.current
        self.list.clear()
        for m in self.mods:
            item = QListWidgetItem(m["name"] + ("" if m["installed"] else "  (chưa tải)"))
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Checked if self._enabled(m) else Qt.Unchecked)
            item.setData(Qt.UserRole, m["folder"])
            item.setToolTip(m["folder"])
            self.list.addItem(item)
        self.list.blockSignals(False)

        folders = [m["folder"] for m in self.mods]
        self.list.setCurrentRow(folders.index(current) if current in folders else (0 if folders else -1))
        if not folders:
            self._select(-1)
        self._update_save()

    def _mod(self, folder):
        return next((m for m in self.mods if m["folder"] == folder), None)

    def _edit(self, m):
        return self.edits.setdefault(m["folder"], {"enabled": m["enabled"], "config": {}})

    def _enabled(self, m):
        return self.edits.get(m["folder"], {}).get("enabled", m["enabled"])

    def _value(self, m, opt):
        config = self.edits.get(m["folder"], {}).get("config", {})
        if opt["name"] in config:
            return config[opt["name"]]
        return m["config"].get(opt["name"], opt["default"])

    def _update_save(self):
        self.btn_save.setText(f"Lưu cấu hình mod ({len(self.edits)} thay đổi)" if self.edits else "Lưu cấu hình mod")

    def _toggle(self, item):
        m = self._mod(item.data(Qt.UserRole))
        if m:
            self._edit(m)["enabled"] = item.checkState() == Qt.Checked
            self._update_save()

    def _select(self, index):
        m = self.mods[index] if 0 <= index < len(self.mods) else None
        self.current = m["folder"] if m else None
        holder = QWidget()
        form = QFormLayout(holder)

        if not m:
            self.title.setText("Chưa có mod")
            self.meta.setText("Thêm mod theo ID Workshop ở ô phía trên.")
            self.desc.clear()
            self.scroll.setWidget(holder)
            return

        self.title.setText(m["name"])
        flags = [f"Tác giả: {m['author']}" if m["author"] else "", f"v{m['version']}" if m["version"] else "",
                 m["folder"], "chỉ client" if m["client_only"] else "", "mọi người chơi cần tải" if m["all_clients_require"] else ""]
        self.meta.setText(" · ".join(f for f in flags if f))
        self.desc.setPlainText(m["description"] or "")

        if m.get("error"):
            form.addRow(label(f"Lỗi đọc modinfo.lua: {m['error']}", "warn"))
        if not m["installed"]:
            form.addRow(label("Mod chưa được tải nên chưa đọc được tuỳ chọn. Bấm “Tải mod (steamcmd)”.", "muted"))
        elif not m["options"]:
            form.addRow(label("Mod này không có tuỳ chọn cấu hình.", "muted"))

        for opt in m["options"]:
            if opt["header"] or not opt["name"]:
                head = QLabel(opt["label"])
                head.setStyleSheet("color:#d9a441;font-weight:bold;margin-top:10px")
                form.addRow(head)
                continue
            combo = QComboBox()
            values = [c["data"] for c in opt["options"]]
            for c in opt["options"]:
                combo.addItem(c["description"] or str(c["data"]))
                if c["hover"]:
                    combo.setItemData(combo.count() - 1, c["hover"], Qt.ToolTipRole)
            value = self._value(m, opt)
            idx = next((i for i, v in enumerate(values) if same(v, value)), -1)
            if idx < 0:
                combo.addItem(f"Giá trị hiện tại: {json.dumps(value, ensure_ascii=False)}")
                values.append(value)
                idx = len(values) - 1
            combo.setCurrentIndex(idx)
            combo.currentIndexChanged.connect(lambda i, m=m, opt=opt, values=values: self._set(m, opt, values[i]))
            name = QLabel(opt["label"] + ("" if same(value, opt["default"]) else "  •"))
            name.setToolTip(opt["hover"])
            name.setWordWrap(True)
            form.addRow(name, combo)

        self.scroll.setWidget(holder)

    def _set(self, m, opt, value):
        self._edit(m)["config"][opt["name"]] = value
        self._update_save()

    def _save(self):
        mods = {}
        for folder, edit in self.edits.items():
            m = self._mod(folder)
            if not m:
                continue
            config = {opt["name"]: self._value(m, opt) for opt in m["options"] if opt["name"] and not opt["header"]}
            mods[folder] = core.ModConfig(enabled=edit["enabled"], config=config)
        if not mods:
            toast("Chưa có thay đổi nào.")
            return
        if guard(lambda: core.put_modconfig(core.ModConfigs(mods=mods), self.page.cluster), "Đã lưu. Khởi động lại server để áp dụng."):
            self.load()

    def _ids(self):
        return core.get_mods(self.page.cluster)["mods"]

    def _add(self):
        items = [x.strip() for x in self.add_input.text().replace("\n", ",").split(",") if x.strip()]
        if not items:
            return
        result = guard(lambda: core.put_mods(core.Mods(mods=self._ids() + items), self.page.cluster))
        if result is not None:
            self.add_input.clear()
            toast(f"Danh sách có {len(result['mods'])} mod. Bấm “Tải mod” để tải về.")
            self.load(keep_edits=True)

    def _remove(self):
        m = self._mod(self.current) if self.current else None
        if not m or not m["workshop_id"]:
            toast("Chọn một mod Workshop trong danh sách.")
            return
        if not confirm(f"Bỏ mod “{m['name']}” khỏi danh sách tải và tắt mod?"):
            return
        ids = [i for i in self._ids() if i != m["workshop_id"]]
        if guard(lambda: core.put_mods(core.Mods(mods=ids), self.page.cluster)) is None:
            return
        if m["in_overrides"]:
            guard(lambda: core.put_modconfig(core.ModConfigs(mods={m["folder"]: core.ModConfig(enabled=False)}), self.page.cluster))
        self.edits.pop(m["folder"], None)
        self.load(keep_edits=True)

    def _download(self):
        if guard(lambda: core.download_mods(self.page.cluster), "Đang tải mod, xem tiến độ ở log Master (tab Bảng điều khiển)..."):
            pass

    def _raw(self):
        f = core.get_file("modoverrides", self.page.cluster)
        content = text_editor_dialog("modoverrides.lua", f["content"])
        if content is not None and guard(lambda: core.put_file("modoverrides", core.FileContent(content=content), self.page.cluster), "Đã lưu modoverrides.lua"):
            self.load()


class WorldTab(QWidget):
    def __init__(self, page):
        super().__init__()
        self.page = page
        lay = QVBoxLayout(self)

        imp = QGroupBox("Nhập thế giới từ file zip")
        i = QVBoxLayout(imp)
        i.addWidget(label("Nén cả thư mục Cluster_N (Documents\\Klei\\DoNotStarveTogether\\<dãy số>\\Cluster_N) thành .zip. "
                          "Server phải đang tắt; thế giới hiện tại được sao lưu trước.", "muted"))
        self.use_ini = QCheckBox("Dùng luôn cấu hình (tên, mật khẩu, chế độ chơi...) trong file")
        self.btn_import = button("Chọn file zip...", self._import, "primary")
        i.addLayout(row(self.btn_import, self.use_ini))
        lay.addWidget(imp)

        bk = QGroupBox("Bản sao lưu (giữ 20 bản gần nhất)")
        b = QVBoxLayout(bk)
        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["Tên", "Thời gian", "MB"])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        b.addWidget(self.table)
        b.addLayout(row(button("Sao lưu ngay", self._backup, "primary"), button("Khôi phục", self._restore),
                        button("Xoá", self._delete, "danger"), button("Mở thư mục sao lưu", self._open_dir),
                        button("Mở thư mục thế giới", lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.page.cluster.dir))))))
        lay.addWidget(bk, 1)

        gen = QGroupBox("Tạo thế giới (chỉ có hiệu lực khi tạo thế giới mới)")
        g = QHBoxLayout(gen)
        g.addWidget(button("Sửa Master/worldgenoverride.lua", lambda: self._edit("master_leveldata")))
        g.addWidget(button("Sửa Caves/worldgenoverride.lua", lambda: self._edit("caves_worldgen")))
        g.addStretch(1)
        lay.addWidget(gen)

    def load(self):
        backups = core.list_backups(self.page.cluster)["backups"]
        self.table.setRowCount(len(backups))
        for r, item in enumerate(backups):
            for c, v in enumerate((item["name"], item["time"], str(item["size_mb"]))):
                self.table.setItem(r, c, QTableWidgetItem(v))

    def _selected(self):
        r = self.table.currentRow()
        if r < 0:
            toast("Chọn một bản sao lưu.")
            return None
        return self.table.item(r, 0).text()

    def _after_import(self, res):
        self.btn_import.setEnabled(True)
        parts = [f"Đã sao lưu bản cũ: {res['backup']}", "Có Caves" if res["caves"] else "Không có Caves"]
        if res["mods"]:
            parts.append(f"{len(res['mods'])} mod được thêm vào danh sách tải")
        QMessageBox.information(main_window, APP_NAME, "Xong!\n" + "\n".join(parts))
        self.load()

    def _run_import(self, fn):
        self.btn_import.setEnabled(False)
        toast("Đang nhập thế giới...")

        def fail(e):
            self.btn_import.setEnabled(True)
            show_error(e)

        run_async(fn, self._after_import, fail)

    def _import(self):
        path, _ = QFileDialog.getOpenFileName(self, "Chọn file thế giới", str(Path.home()), "Zip (*.zip)")
        if not path or not confirm(f"Thay thế thế giới hiện tại bằng “{Path(path).name}”?"):
            return
        cluster, use_ini = self.page.cluster, self.use_ini.isChecked()

        def work():
            cluster.require_stopped()
            return core.import_cluster(cluster, Path(path), use_ini)

        self._run_import(work)

    def _backup(self):
        cluster = self.page.cluster
        toast("Đang sao lưu...")
        run_async(lambda: core.make_backup(cluster), lambda name: (toast(f"Đã sao lưu: {name}"), self.load()))

    def _restore(self):
        name = self._selected()
        if name and confirm(f"Khôi phục “{name}”? Thế giới hiện tại sẽ được sao lưu trước."):
            cluster = self.page.cluster
            self._run_import(lambda: core.restore_backup(name, cluster))

    def _delete(self):
        name = self._selected()
        if name and confirm(f"Xoá “{name}”?"):
            if guard(lambda: core.delete_backup(name, self.page.cluster), "Đã xoá"):
                self.load()

    def _open_dir(self):
        self.page.cluster.backup_dir.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.page.cluster.backup_dir)))

    def _edit(self, name):
        f = core.get_file(name, self.page.cluster)
        content = text_editor_dialog(f["path"], f["content"])
        if content is not None:
            guard(lambda: core.put_file(name, core.FileContent(content=content), self.page.cluster), "Đã lưu")


class ListsTab(QWidget):
    FILES = (("adminlist", "Admin (adminlist.txt)"), ("blocklist", "Chặn (blocklist.txt)"), ("whitelist", "Giữ chỗ (whitelist.txt)"))

    def __init__(self, page):
        super().__init__()
        self.page = page
        lay = QVBoxLayout(self)
        lay.addWidget(label("Mỗi dòng một Klei ID (KU_xxxxxxxx). Xem ID người chơi bằng lệnh c_listallplayers() hoặc trong log.", "muted"))
        grid = QHBoxLayout()
        self.edits = {}
        for name, title in self.FILES:
            box = QGroupBox(title)
            b = QVBoxLayout(box)
            edit = QPlainTextEdit()
            self.edits[name] = edit
            b.addWidget(edit)
            b.addLayout(row(button("Lưu", lambda _=False, n=name: self._save(n), "primary")))
            grid.addWidget(box)
        lay.addLayout(grid, 1)

    def load(self):
        for name, edit in self.edits.items():
            edit.setPlainText(core.get_file(name, self.page.cluster)["content"])

    def _save(self, name):
        content = self.edits[name].toPlainText()
        guard(lambda: core.put_file(name, core.FileContent(content=content), self.page.cluster), "Đã lưu")


class ServerPage(QWidget):
    renamed = pyqtSignal()

    def __init__(self):
        super().__init__()
        self.cluster = None
        lay = QVBoxLayout(self)
        self.title = label(kind="title")
        lay.addWidget(self.title)
        self.tabs = QTabWidget()
        self.dashboard, self.config, self.token = DashboardTab(self), ConfigTab(self), TokenTab(self)
        self.mods, self.world, self.lists = ModsTab(self), WorldTab(self), ListsTab(self)
        for tab, name in ((self.dashboard, "Bảng điều khiển"), (self.config, "Cấu hình"), (self.token, "Token"),
                          (self.mods, "Mods"), (self.world, "Thế giới"), (self.lists, "Admin / Chặn")):
            self.tabs.addTab(tab, name)
        self.tabs.currentChanged.connect(lambda _: self.load_tab())
        lay.addWidget(self.tabs, 1)

    def set_cluster(self, cluster):
        changed = cluster is not self.cluster
        self.cluster = cluster
        self.title.setText(cluster.name())
        if changed:
            self.dashboard.reset()
        self.load_tab()

    def title_changed(self):
        self.title.setText(self.cluster.name())
        self.renamed.emit()

    def load_tab(self):
        tab = self.tabs.currentWidget()
        if self.cluster and hasattr(tab, "load"):
            guard(tab.load)

    def refresh(self):
        if self.cluster and self.tabs.currentWidget() is self.dashboard:
            self.dashboard.refresh()


# ------------------------------------------------------------------------------------ #
# Cửa sổ chính

class ShutdownDialog(QDialog):
    progress = pyqtSignal(str)

    def __init__(self, parent):
        super().__init__(parent)
        self.setWindowTitle(APP_NAME)
        self.setWindowFlags(Qt.Dialog | Qt.CustomizeWindowHint | Qt.WindowTitleHint)
        self.setModal(True)
        self.text = QLabel("Đang lưu và đồng bộ...")
        self.text.setMinimumWidth(380)
        lay = QVBoxLayout(self)
        lay.addWidget(self.text)
        lay.addWidget(label("Vui lòng chờ, app sẽ tự đóng khi xong.", "muted"))
        self.progress.connect(self.text.setText)

    def closeEvent(self, event):
        event.ignore()


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.resize(1240, 860)
        self.closing_done = False
        self.shutdown_started = False

        self.nav = QListWidget()
        self.nav.setObjectName("nav")
        self.nav.setFixedWidth(230)
        self.nav.currentRowChanged.connect(self._navigate)

        self.home, self.remote, self.server = HomePage(), RemotePage(), ServerPage()
        self.stack = QStackedWidget()
        for page in (self.home, self.remote, self.server):
            self.stack.addWidget(page)

        self.home.open_cluster.connect(self.open_cluster)
        self.home.clusters_changed.connect(self.rebuild_nav)
        self.remote.clusters_changed.connect(self.rebuild_nav)
        self.server.renamed.connect(self.rebuild_nav)

        central = QWidget()
        lay = QHBoxLayout(central)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.nav)
        content = QWidget()
        cl = QVBoxLayout(content)
        cl.addWidget(self.stack)
        lay.addWidget(content, 1)
        self.setCentralWidget(central)
        self.statusBar().showMessage(f"Dữ liệu: {core.BASE_DIR}")

        self.rebuild_nav()
        self.nav.setCurrentRow(0)

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.tick)
        self.timer.start(1500)

    def rebuild_nav(self):
        current = self.nav.currentItem().data(Qt.UserRole) if self.nav.currentItem() else "home"
        self.nav.blockSignals(True)
        self.nav.clear()
        for key, text in (("home", "🏠  Trang chủ"), ("remote", "🔗  Liên kết VPS")):
            item = QListWidgetItem(text)
            item.setData(Qt.UserRole, key)
            self.nav.addItem(item)
        sep = QListWidgetItem("SERVER")
        sep.setFlags(Qt.NoItemFlags)
        sep.setForeground(QColor("#6b6253"))
        self.nav.addItem(sep)
        for cluster in core.registry.all():
            item = QListWidgetItem(f"●  {cluster.name()}")
            item.setData(Qt.UserRole, "cluster:" + cluster.id)
            self.nav.addItem(item)
        self.nav.blockSignals(False)

        keys = [self.nav.item(i).data(Qt.UserRole) for i in range(self.nav.count())]
        self.nav.setCurrentRow(keys.index(current) if current in keys else 0)
        self._color_nav()

    def _color_nav(self):
        for i in range(self.nav.count()):
            key = self.nav.item(i).data(Qt.UserRole) or ""
            if key.startswith("cluster:"):
                cluster = core.registry.clusters.get(key[8:])
                status = cluster.shards["Master"].status if cluster else "offline"
                self.nav.item(i).setForeground(QColor(STATUS_COLOR.get(status, "#ece3d3")) if status != "offline" else QColor("#ece3d3"))

    def open_cluster(self, cid):
        for i in range(self.nav.count()):
            if self.nav.item(i).data(Qt.UserRole) == "cluster:" + cid:
                self.nav.setCurrentRow(i)
                return

    def _navigate(self, index):
        item = self.nav.item(index)
        key = item.data(Qt.UserRole) if item else "home"
        if key == "home":
            self.stack.setCurrentWidget(self.home)
        elif key == "remote":
            self.remote.load_form()
            self.stack.setCurrentWidget(self.remote)
        elif key and key.startswith("cluster:"):
            cluster = core.registry.clusters.get(key[8:])
            if cluster:
                self.server.set_cluster(cluster)
                self.stack.setCurrentWidget(self.server)
        self.tick()

    def tick(self):
        page = self.stack.currentWidget()
        try:
            if page is self.home:
                self.home.refresh()
            elif page is self.server:
                if self.server.cluster and self.server.cluster.id not in core.registry.clusters:
                    self.nav.setCurrentRow(0)
                    return
                self.server.refresh()
            self.remote.refresh() if page is self.remote else self._watch_remote_clusters()
            self._color_nav()
        except Exception as e:  # noqa: BLE001
            print(f"tick: {e!r}", flush=True)

    def _watch_remote_clusters(self):
        # "Đồng bộ" có thể tạo server mới ở thread nền: cập nhật thanh bên.
        names = tuple(sorted(core.registry.clusters))
        if names != self.remote.last_names:
            self.remote.last_names = names
            self.rebuild_nav()

    def closeEvent(self, event):
        if self.closing_done:
            event.accept()
            return
        event.ignore()
        if self.shutdown_started:
            return
        self.shutdown_started = True
        self.timer.stop()
        dialog = ShutdownDialog(self)
        dialog.show()

        def work():
            remote_link.shutdown(dialog.progress.emit)
            for t in [t for c in core.registry.all() for t in c.stop_all()]:
                t.join()

        def finish(_=None):
            self.closing_done = True
            dialog.hide()
            self.close()

        def failed(e):
            QMessageBox.warning(self, APP_NAME, f"Lỗi khi đóng: {err_text(e)}")
            finish()

        run_async(work, finish, failed)


def apply_theme(app):
    app.setStyle("Fusion")
    pal = QPalette()
    for role, color in ((QPalette.Window, "#1b1814"), (QPalette.WindowText, "#ece3d3"), (QPalette.Base, "#2f2a22"),
                        (QPalette.AlternateBase, "#26221c"), (QPalette.Text, "#ece3d3"), (QPalette.Button, "#2f2a22"),
                        (QPalette.ButtonText, "#ece3d3"), (QPalette.Highlight, "#d9a441"), (QPalette.HighlightedText, "#1b1814"),
                        (QPalette.ToolTipBase, "#2f2a22"), (QPalette.ToolTipText, "#ece3d3")):
        pal.setColor(role, QColor(color))
    app.setPalette(pal)
    app.setStyleSheet(STYLE)


def main():
    global main_window

    QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)
    if os.name == "nt":
        # Để taskbar dùng icon của app thay vì icon của python.exe khi chạy từ mã nguồn.
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("DSTLauncher")
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setWindowIcon(QIcon(str(ICON_FILE)))
    app.setFont(QFont("Segoe UI", 10) if os.name == "nt" else app.font())
    apply_theme(app)

    core.registry.load()
    core.write_mods_setup() if core.SERVER_EXE.exists() else None
    threading.Thread(target=core.stats_loop, daemon=True).start()
    threading.Thread(target=core.auto_update_loop, daemon=True).start()
    remote_link.start()

    main_window = MainWindow()
    main_window.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
