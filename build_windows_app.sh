#!/usr/bin/env bash
# Build DST Launcher (dst_desktop.py) thành DSTLauncher.exe cho Windows, chạy ngay trên Linux
# bằng Wine + Python bản Windows + PyInstaller. Không cần máy Windows hay GitHub Actions.
#
#   ./build_windows_app.sh            # build -> dist/DSTLauncher.exe
#   ./build_windows_app.sh --reset    # xoá môi trường Wine đã tạo rồi build lại từ đầu
#
# Lần đầu: cài wine (apt) và tải Python Windows + thư viện (~1.5 GB trong $CACHE), mất 5–15 phút.
# Các lần sau chỉ vài phút. Biến môi trường tuỳ chọn:
#   PY_VERSION       bản Python Windows (mặc định 3.12.8)
#   DST_BUILD_CACHE  thư mục cache (mặc định ~/.cache/dst-winbuild)

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY_VERSION="${PY_VERSION:-3.12.8}"
CACHE="${DST_BUILD_CACHE:-$HOME/.cache/dst-winbuild}"
OUT="$ROOT/dist"
SOURCES=(app.py remote_link.py dst_desktop.py)

export WINEPREFIX="$CACHE/wineprefix"
export WINEARCH=win64
export WINEDEBUG=-all
export WINEDLLOVERRIDES="mscoree,mshtml="   # không hỏi cài Mono/Gecko

log() { printf '\033[1;33m==> %s\033[0m\n' "$*"; }
die() { printf '\033[1;31mLỗi: %s\033[0m\n' "$*" >&2; exit 1; }

[[ "${1:-}" == "--reset" ]] && { log "Xoá $WINEPREFIX"; rm -rf "$WINEPREFIX"; }

for f in "${SOURCES[@]}" requirements-desktop.txt assets/icon.ico assets/icon.png; do
    [[ -f "$ROOT/$f" ]] || die "Thiếu $ROOT/$f"
done

# --- 1. Wine ------------------------------------------------------------------------- #
if ! command -v wine >/dev/null; then
    command -v apt-get >/dev/null || die "Chưa có wine. Hãy cài wine (64-bit) rồi chạy lại."
    SUDO=""; [[ $EUID -ne 0 ]] && SUDO="sudo"
    log "Cài wine + xvfb (apt)..."
    $SUDO dpkg --add-architecture i386
    $SUDO apt-get update -qq
    DEBIAN_FRONTEND=noninteractive $SUDO apt-get install -y -qq --no-install-recommends wine wine64 xvfb xauth >/dev/null
fi
command -v xvfb-run >/dev/null || die "Thiếu xvfb-run (apt install xvfb)."
log "$(wine --version)"

# Wine cần một màn hình ảo cho trình cài Python.
WINE=(xvfb-run -a wine)

# --- 2. Python bản Windows ----------------------------------------------------------- #
PY_DIR_WIN='C:\Python'
PY_EXE="$WINEPREFIX/drive_c/Python/python.exe"

if [[ ! -f "$PY_EXE" ]]; then
    mkdir -p "$CACHE"
    installer="$CACHE/python-$PY_VERSION-amd64.exe"

    if [[ ! -f "$installer" ]]; then
        log "Tải Python $PY_VERSION cho Windows..."
        curl -fL --retry 3 -o "$installer.part" "https://www.python.org/ftp/python/$PY_VERSION/python-$PY_VERSION-amd64.exe"
        mv "$installer.part" "$installer"
    fi

    log "Khởi tạo Wine ($WINEPREFIX)..."
    xvfb-run -a wineboot --init >/dev/null 2>&1 || true
    wineserver -w

    log "Cài Python $PY_VERSION vào Wine..."
    "${WINE[@]}" "$installer" /quiet InstallAllUsers=0 TargetDir="$PY_DIR_WIN" PrependPath=0 \
        Include_test=0 Include_launcher=0 Include_doc=0 Include_tcltk=0 Shortcuts=0 >/dev/null 2>&1 || true
    wineserver -w
    [[ -f "$PY_EXE" ]] || die "Cài Python trong Wine thất bại. Thử: $0 --reset"
fi

PY=(wine "$PY_EXE")
log "$("${PY[@]}" --version 2>/dev/null | tr -d '\r')"

# --- 3. Thư viện ----------------------------------------------------------------------- #
REQ_HASH="$(sha256sum "$ROOT/requirements-desktop.txt" | cut -c1-16)"
STAMP="$WINEPREFIX/.deps-$REQ_HASH"

if [[ ! -f "$STAMP" ]]; then
    log "Cài thư viện (requirements-desktop.txt)..."
    "${PY[@]}" -m pip install --disable-pip-version-check -q --upgrade pip
    "${PY[@]}" -m pip install --disable-pip-version-check -q -r "$(winepath -w "$ROOT/requirements-desktop.txt")"
    rm -f "$WINEPREFIX"/.deps-*
    touch "$STAMP"
fi

# --- 4. Build -------------------------------------------------------------------------- #
WORK="$CACHE/work"
rm -rf "$WORK"
mkdir -p "$WORK" "$OUT"
rm -f "$OUT/DSTLauncher.exe"
cp "${SOURCES[@]/#/$ROOT/}" "$WORK/"
cp -r "$ROOT/assets" "$WORK/"

log "Build DSTLauncher.exe bằng PyInstaller..."
(
    cd "$WORK"
    "${PY[@]}" -m PyInstaller --noconfirm --clean --onefile --windowed --name DSTLauncher \
        --collect-all lupa --hidden-import remote_link \
        --icon assets/icon.ico --add-data "assets/icon.png;assets" \
        --distpath "$(winepath -w "$OUT")" --workpath "$(winepath -w "$WORK/build")" --specpath "$(winepath -w "$WORK")" \
        dst_desktop.py 2>&1 | grep -E "ERROR|Error|WARNING: lib not found|Building EXE|completed successfully" || true
)
wineserver -w

[[ -f "$OUT/DSTLauncher.exe" ]] || die "Không tạo được exe. Xem log: chạy lại với lệnh PyInstaller trong $WORK."

log "Xong: $OUT/DSTLauncher.exe ($(du -h "$OUT/DSTLauncher.exe" | cut -f1))"
