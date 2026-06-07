"""
Browser + Claude  —  범용 개발 도구  (v5: 에이전트→브라우저 조작 MCP)
왼쪽: 진짜 Chrome 임베드  |  오른쪽: claude / codex 터미널 + 콘솔 패널

실행: python myah.py
필요: pip install customtkinter pywin32 websocket-client mcp   (+ Google Chrome 설치)
구성: 같은 폴더에 myah_mcp_server.py 가 있어야 함(에이전트용 브라우저 MCP 서버)

[v5 에서 바뀐 것 — 에이전트가 페이지를 조작]
 · 오른쪽 에이전트(Claude Code)가 왼쪽 크롬 페이지를 직접 만질 수 있게 MCP 다리 연결
     - myah_mcp_server.py: stdio MCP 서버. browser_snapshot/type/click 툴 제공
     - 그 서버가 앱이 띄운 크롬에 CDP 로 붙어 명령 실행(앱과 별프로세스, CDP 다중 클라이언트 OK)
     - 연결 다리: 앱이 크롬 포트를 임시폴더/myah-cdp.json 에 기록 → MCP 서버가 매 호출마다 읽음
       (⟳ 로 크롬 재시작해 포트가 바뀌어도 자동 추종)
     - 자동 등록: ▶시작 시 프로젝트 폴더 .mcp.json 에 절대경로로 bc-browser 병합 등록
       (Claude Code 가 프로젝트 스코프로 자동 로드. 첫 실행 시 승인 프롬프트가 뜰 수 있음)
       → 이제 "입력칸에 안녕하세요 써줘" 같은 요청이 실제로 페이지에서 동작

[v4 — 유지] CDP: ⟳=진짜 reload / 콘솔 에러·예외·네트워크 수집 → 오른쪽 콘솔 패널(개별·전체 복사)
 · 📷 캡처: 전체 페이지 / 구간 직접 선택(페이지 내 드래그 오버레이) → "폴더/캡처/이름.png" 저장
   + 저장 경로를 클립보드에 복사(오른쪽 에이전트에 붙여넣어 "이 이미지 봐줘")
[v3 — 유지] 왼쪽=진짜 Chrome 임베드(전용 프로필·스냅샷 차이 탐지) / 엔진 토글(claude·codex)
[v2 — 유지] SetParent 진짜 자식창 + AttachThreadInput / 터미널 동적 컬럼(헬퍼 격리)
 ※ 터미널은 owner 모드(테두리만 제거한 top-level)로 박음 — conhost 한글 IME 가 자식창에선
   안 붙고 top-level 에서만 정상 동작하기 때문. (왼쪽 크롬은 자식창 그대로)
[v1 — 유지] 메인스레드 sync / SetWindowLongPtr / 세대 토큰 / PowerShell 이스케이프 / 원자적 저장

 ※ 고DPI 정렬은 환경 의존이라 강제하지 않음.
 ※ win_webview.py 는 더 이상 사용하지 않음(삭제 가능).
"""

import customtkinter as ctk
import tkinter as tk
from tkinter import filedialog
import subprocess, threading, time, os, sys, json, shutil, tempfile, socket, urllib.request, base64, re

try:
    import win32gui, win32con, win32process, win32api
    HAS_WIN32 = True
except ImportError:
    HAS_WIN32 = False

try:
    import websocket          # pip install websocket-client
    HAS_WS = True
except ImportError:
    HAS_WS = False

HERE       = os.path.dirname(os.path.abspath(__file__))
SETTINGS_F = os.path.join(HERE, "settings.json")
PYTHON     = sys.executable
MAX_HIST   = 10
ENGINES    = ["claude", "codex"]      # 오른쪽 패널에서 띄울 코딩 에이전트 CLI

# 왼쪽 패널에 통째로 박을 진짜 크롬
CHROME_CLASS   = "Chrome_WidgetWin_1"                       # 크롬 메인 프레임 창 클래스
CHROME_PROFILE = os.path.join(HERE, ".myah-chrome-profile")   # 전용 프로필(독립 인스턴스 보장)
PORT_FILE      = os.path.join(tempfile.gettempdir(), "myah-cdp.json")  # 크롬 CDP 포트 → MCP 서버가 읽음
MCP_SERVER     = os.path.join(HERE, "myah_mcp_server.py")     # 에이전트용 브라우저 조작 MCP 서버

def find_chrome() -> str:
    """chrome.exe 경로를 찾는다. 못 찾으면 빈 문자열."""
    cand = []
    for env in ("ProgramFiles", "ProgramFiles(x86)", "LocalAppData"):
        base = os.environ.get(env)
        if base:
            cand.append(os.path.join(base, r"Google\Chrome\Application\chrome.exe"))
    w = shutil.which("chrome")
    if w:
        cand.append(w)
    for p in cand:
        if p and os.path.exists(p):
            return p
    return ""

CHROME_EXE = find_chrome()

# 헬퍼 서브프로세스에 콘솔이 안 뜨도록: 가능하면 pythonw.exe
PYTHONW = os.path.join(os.path.dirname(PYTHON), "pythonw.exe")
if not os.path.exists(PYTHONW):
    PYTHONW = PYTHON
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
DETACHED_PROCESS = getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
CREATE_NEW_CONSOLE = getattr(subprocess, "CREATE_NEW_CONSOLE", 0x00000010)


def list_chrome_windows() -> set:
    """현재 화면의 크롬 '메인 프레임' top-level 창 HWND 집합.
    (보이고, 제목이 있고, owner 가 없는 Chrome_WidgetWin_1 만 — 툴팁/팝업 헬퍼 제외)"""
    found = set()
    if not HAS_WIN32:
        return found
    def cb(hwnd, _):
        try:
            if (win32gui.IsWindowVisible(hwnd)
                    and win32gui.GetClassName(hwnd) == CHROME_CLASS
                    and win32gui.GetWindowText(hwnd).strip()
                    and win32gui.GetWindow(hwnd, win32con.GW_OWNER) == 0):
                found.add(hwnd)
        except Exception:
            pass
        return True
    try:
        win32gui.EnumWindows(cb, None)
    except Exception:
        pass
    return found


def list_top_windows(exclude_pid=None) -> dict:
    """보이고, 제목이 있고, owner 가 없는 top-level 창 {hwnd: pid}.
    exclude_pid(보통 우리 자신 프로세스) 의 창은 제외 — myah 자체 창을 잡지 않도록."""
    found = {}
    if not HAS_WIN32:
        return found
    def cb(hwnd, _):
        try:
            if (win32gui.IsWindowVisible(hwnd)
                    and win32gui.GetWindowText(hwnd).strip()
                    and win32gui.GetWindow(hwnd, win32con.GW_OWNER) == 0):
                _tid, pid = win32process.GetWindowThreadProcessId(hwnd)
                if not (exclude_pid and pid == exclude_pid):
                    found[hwnd] = pid
        except Exception:
            pass
        return True
    try:
        win32gui.EnumWindows(cb, None)
    except Exception:
        pass
    return found


def free_port() -> int:
    """비어있는 로컬 포트 하나 잡아서 반환."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


# ── CDP (Chrome DevTools Protocol) 클라이언트 ─────────────────────────────
class CDPClient:
    """크롬 원격 디버깅 포트에 WebSocket 으로 붙어
       (1) 모든 page 타깃의 콘솔 에러/예외/네트워크 실패를 on_entry 콜백으로 흘리고
       (2) reload() 로 활성 탭을 진짜 새로고침한다.
       전부 백그라운드 스레드에서 동작. on_entry 의 메인스레드 마샬링은 호출측 책임."""

    def __init__(self, port, on_entry):
        self.port = port
        self.on_entry = on_entry
        self._ws = None
        self._lock = threading.Lock()
        self._id = 0
        self._sessions = set()      # page sessionId 집합
        self._active = None         # 활성(최근 활동) page sessionId
        self._pending = {}          # id -> [Event, result_holder]  (요청-응답 상관)
        self._running = True
        threading.Thread(target=self._run, daemon=True).start()

    def _next_id(self):
        self._id += 1
        return self._id

    def _send(self, method, params=None, session=None):
        if not self._ws:
            return
        msg = {"id": self._next_id(), "method": method, "params": params or {}}
        if session:
            msg["sessionId"] = session
        try:
            with self._lock:
                self._ws.send(json.dumps(msg))
        except Exception:
            pass

    def _browser_ws_url(self):
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/json/version", timeout=2) as r:
            return json.loads(r.read().decode("utf-8")).get("webSocketDebuggerUrl")

    def _run(self):
        ws_url = None
        for _ in range(40):                 # 디버깅 포트 열릴 때까지 ~12초 대기
            if not self._running:
                return
            try:
                ws_url = self._browser_ws_url()
                if ws_url:
                    break
            except Exception:
                pass
            time.sleep(0.3)
        if not ws_url:
            return
        try:
            # Chrome 111+ Origin 검사 회피용 suppress_origin (런치 플래그와 함께)
            self._ws = websocket.create_connection(ws_url, suppress_origin=True)
        except Exception:
            self._ws = None
            return
        self._send("Target.setAutoAttach",
                   {"autoAttach": True, "waitForDebuggerOnStart": False, "flatten": True})
        while self._running:
            try:
                raw = self._ws.recv()
            except Exception:
                break
            if not raw:
                continue
            try:
                self._handle(json.loads(raw))
            except Exception:
                pass
        self.close()

    def _handle(self, msg):
        method = msg.get("method")
        # 요청-응답 상관: id 가 있으면 대기 중인 call() 을 깨운다
        mid = msg.get("id")
        if mid is not None and mid in self._pending:
            ev, holder = self._pending.get(mid, (None, None))
            if ev is not None:
                holder["result"] = msg.get("result")
                holder["error"] = msg.get("error")
                ev.set()
            return
        if method == "Target.attachedToTarget":
            p = msg.get("params", {})
            sid = p.get("sessionId")
            if p.get("targetInfo", {}).get("type") == "page" and sid:
                self._sessions.add(sid)
                self._active = sid
                for m in ("Runtime.enable", "Log.enable", "Network.enable"):
                    self._send(m, {}, sid)
            return
        if method == "Target.detachedFromTarget":
            sid = msg.get("params", {}).get("sessionId")
            self._sessions.discard(sid)
            if self._active == sid:
                self._active = next(iter(self._sessions), None)
            return

        sess = msg.get("sessionId")
        if sess and sess in self._sessions:
            self._active = sess                     # 활동 있는 탭을 활성으로 추적
        params = msg.get("params", {})
        text = None
        if method == "Runtime.exceptionThrown":
            text = self._fmt_exception(params.get("exceptionDetails", {}))
        elif method == "Runtime.consoleAPICalled":
            if params.get("type") in ("error", "warning", "assert"):
                text = self._fmt_console(params)
        elif method == "Log.entryAdded":
            e = params.get("entry", {})
            if e.get("level") in ("error", "warning"):
                text = self._fmt_log(e)
        elif method == "Network.loadingFailed":
            text = self._fmt_netfail(params)
        if text:
            try:
                self.on_entry(text)
            except Exception:
                pass

    @staticmethod
    def _loc(url, line, col):
        loc = url or ""
        if line is not None:
            loc += f":{line}"
            if col is not None:
                loc += f":{col}"
        return loc

    def _fmt_exception(self, det):
        ex = det.get("exception", {})
        desc = ex.get("description") or det.get("text") or "Uncaught exception"
        loc = self._loc(det.get("url"), det.get("lineNumber"), det.get("columnNumber"))
        return f"[exception] {desc}" + (f"\n  @ {loc}" if loc else "")

    def _fmt_console(self, params):
        parts = []
        for a in params.get("args", []):
            parts.append(a.get("description") if a.get("description") is not None
                         else a.get("value", a.get("type", "")))
        body = " ".join(str(x) for x in parts if x != "")
        st = params.get("stackTrace", {}).get("callFrames", [])
        loc = ""
        if st:
            f0 = st[0]
            loc = self._loc(f0.get("url"), f0.get("lineNumber"), f0.get("columnNumber"))
        return f"[console.{params.get('type')}] {body}" + (f"\n  @ {loc}" if loc else "")

    def _fmt_log(self, e):
        loc = self._loc(e.get("url"), e.get("lineNumber"), None)
        return f"[{e.get('source','log')}/{e.get('level')}] {e.get('text','')}" + (f"\n  @ {loc}" if loc else "")

    def _fmt_netfail(self, params):
        return f"[network] FAILED {params.get('errorText','')} ({params.get('type','')})"

    def reload(self):
        sid = self._active or next(iter(self._sessions), None)
        if sid:
            self._send("Page.reload", {"ignoreCache": True}, sid)
            return True
        return False

    def call(self, method, params=None, session="__active__", timeout=12):
        """요청-응답형 CDP 호출. 결과(dict) 또는 None."""
        if not self._ws:
            return None
        if session == "__active__":
            session = self._active or next(iter(self._sessions), None)
        mid = self._next_id()
        ev = threading.Event()
        holder = {"result": None, "error": None}
        self._pending[mid] = (ev, holder)
        msg = {"id": mid, "method": method, "params": params or {}}
        if session:
            msg["sessionId"] = session
        try:
            with self._lock:
                self._ws.send(json.dumps(msg))
        except Exception:
            self._pending.pop(mid, None)
            return None
        ok = ev.wait(timeout)
        self._pending.pop(mid, None)
        if not ok or holder["error"]:
            return None
        return holder["result"]

    def eval_js(self, expression, timeout=12):
        """활성 페이지에서 JS 실행 후 반환값(returnByValue)."""
        res = self.call("Runtime.evaluate",
                        {"expression": expression, "returnByValue": True, "awaitPromise": True},
                        timeout=timeout)
        if not res:
            return None
        return res.get("result", {}).get("value")

    def capture(self, clip=None, full=False, timeout=20):
        """활성 페이지 스크린샷 → PNG bytes. clip 지정 시 그 영역만, full=True 면 스크롤 포함 전체."""
        params = {"format": "png"}
        if clip:
            params["clip"] = clip
        elif full:
            params["captureBeyondViewport"] = True
        res = self.call("Page.captureScreenshot", params, timeout=timeout)
        if not res or not res.get("data"):
            return None
        try:
            return base64.b64decode(res["data"])
        except Exception:
            return None

    def close(self):
        self._running = False
        try:
            if self._ws:
                self._ws.close()
        except Exception:
            pass
        self._ws = None


# ── 설정 ──────────────────────────────────────────────────────────────
def load_settings() -> dict:
    default = {
        "theme": "dark",
        "sash_ratio": 0.65,
        "url_history": [],
        "folder_history": [],
        "bookmarks": [],
        "engine": "claude",       # "claude" | "codex"
        "presets": {},            # name -> {folder, engine, urls:[...]}
        "last_preset": "",
        "auto_yes": False,        # 에이전트 권한 확인 자동 허용(YOLO)
        "models": {               # 엔진별 모델 목록 (사용자가 추가·수정)
            "claude": ["opus", "sonnet", "haiku"],   # 별칭=항상 최신
            "codex": ["gpt-5.3-codex", "gpt-5"],
        },
        "model_sel": {},          # 엔진별 마지막 선택 모델 라벨
        "ui_autodetect": True,    # 폴더 열면 UI(.bat/.py GUI) 자동 감지·실행
        "auto_reload": False,     # 파일 바뀌고 6초 잠잠하면 앱 자동 재실행
    }
    if os.path.exists(SETTINGS_F):
        try:
            with open(SETTINGS_F, "r", encoding="utf-8") as f:
                d = json.load(f)
                default.update(d)
        except Exception:
            pass
    return default


def save_settings(s: dict):
    """원자적 저장: temp 파일에 쓰고 교체 (쓰는 중 크래시로 깨지는 것 방지)."""
    try:
        tmp = SETTINGS_F + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(s, f, ensure_ascii=False, indent=2)
        os.replace(tmp, SETTINGS_F)
    except Exception:
        pass


def push_history(lst: list, val: str) -> list:
    if val in lst:
        lst.remove(val)
    lst.insert(0, val)
    return lst[:MAX_HIST]


# ── 창 핸들 유틸 ───────────────────────────────────────────────────────
def set_owner(hwnd, owner):
    """64비트 안전하게 owner 창을 지정 (폴백 경로용)."""
    try:
        win32gui.SetWindowLongPtr(hwnd, win32con.GWL_HWNDPARENT, owner)
    except AttributeError:
        win32gui.SetWindowLong(hwnd, win32con.GWL_HWNDPARENT, owner)


def move_to(hwnd, x, y, w, h):
    if hwnd and win32gui.IsWindow(hwnd):
        win32gui.MoveWindow(hwnd, x, y, w, h, True)


def make_borderless(hwnd):
    """창을 top-level 로 둔 채 테두리/타이틀바만 제거한다.
    자식창(WS_CHILD)으로 만들지 않으므로 conhost 의 한글 IME 가 정상 동작한다.
    (자식창화하면 영문만 입력되고 한글 조합이 안 붙는 문제를 피하려는 용도)"""
    try:
        style = win32gui.GetWindowLong(hwnd, win32con.GWL_STYLE)
        style &= ~(win32con.WS_CAPTION | win32con.WS_THICKFRAME |
                   win32con.WS_MINIMIZEBOX | win32con.WS_MAXIMIZEBOX |
                   win32con.WS_SYSMENU | win32con.WS_BORDER | win32con.WS_DLGFRAME)
        win32gui.SetWindowLong(hwnd, win32con.GWL_STYLE, style)
        ex = win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE)
        ex &= ~win32con.WS_EX_APPWINDOW          # 작업표시줄/alt-tab 에서 분리
        win32gui.SetWindowLong(hwnd, win32con.GWL_EXSTYLE, ex)
        win32gui.SetWindowPos(
            hwnd, 0, 0, 0, 0, 0,
            win32con.SWP_FRAMECHANGED | win32con.SWP_NOMOVE |
            win32con.SWP_NOSIZE | win32con.SWP_NOZORDER | win32con.SWP_NOACTIVATE)
    except Exception:
        pass


def embed_as_child(hwnd, parent_hwnd, main_tid) -> bool:
    """외부 프로세스의 top-level 창을 부모 프레임의 진짜 자식(WS_CHILD)으로 만든다.
    성공하면 부모를 따라 자동 이동·클리핑되어 폴링이 불필요해진다.
    실패하면 False 를 반환 → 호출부에서 owner 폴백."""
    try:
        style = win32gui.GetWindowLong(hwnd, win32con.GWL_STYLE)
        style &= ~(win32con.WS_POPUP | win32con.WS_CAPTION | win32con.WS_THICKFRAME |
                   win32con.WS_MINIMIZEBOX | win32con.WS_MAXIMIZEBOX |
                   win32con.WS_SYSMENU | win32con.WS_BORDER | win32con.WS_DLGFRAME)
        style |= (win32con.WS_CHILD | win32con.WS_VISIBLE)
        win32gui.SetWindowLong(hwnd, win32con.GWL_STYLE, style)

        ex = win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE)
        ex &= ~win32con.WS_EX_APPWINDOW          # 작업표시줄/alt-tab 에서 분리
        win32gui.SetWindowLong(hwnd, win32con.GWL_EXSTYLE, ex)

        win32gui.SetParent(hwnd, parent_hwnd)
        win32gui.SetWindowPos(
            hwnd, 0, 0, 0, 0, 0,
            win32con.SWP_FRAMECHANGED | win32con.SWP_NOMOVE |
            win32con.SWP_NOSIZE | win32con.SWP_NOZORDER | win32con.SWP_NOACTIVATE)

        # NOTE: AttachThreadInput 은 쓰지 않는다.
        # 입력큐를 메인 스레드에 강제로 엮으면 메인 Tk 입력큐가 꼬여서,
        # 박힌 크롬과 (메인에 종속된) owner 터미널 양쪽 모두 키보드 입력이 먹통이 된다.
        # 대신 클릭하면 자식창은 스스로 포커스를 받고, owner 창은 top-level 이라 정상 활성화된다.
        return True
    except Exception:
        return False


# ── 콘솔 cell grid 리사이즈 (단명 헬퍼 프로세스에서 실행) ──────────────────
# 메인 앱이 자기 콘솔에서 떨어져 나가는 부작용을 피하려고 별도 프로세스로 격리한다.
# argv: pid(콘솔에 붙은 프로세스), px_w, px_h
CONSOLE_FIT_CODE = r'''
import sys, ctypes
from ctypes import wintypes
pid = int(sys.argv[1]); pw = int(sys.argv[2]); ph = int(sys.argv[3])
k = ctypes.windll.kernel32

class COORD(ctypes.Structure):      _fields_=[("X",ctypes.c_short),("Y",ctypes.c_short)]
class SMALL_RECT(ctypes.Structure): _fields_=[("L",ctypes.c_short),("T",ctypes.c_short),("R",ctypes.c_short),("B",ctypes.c_short)]
class CSBI(ctypes.Structure):       _fields_=[("size",COORD),("cur",COORD),("attr",ctypes.c_ushort),("win",SMALL_RECT),("maxwin",COORD)]
class CFI(ctypes.Structure):        _fields_=[("nFont",wintypes.DWORD),("dwFontSize",COORD)]

k.CreateFileW.restype  = wintypes.HANDLE
k.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                          wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]

k.FreeConsole()
if not k.AttachConsole(pid):
    sys.exit(1)
try:
    GENERIC_RW = 0xC0000000; SHARE = 3; OPEN = 3
    h = k.CreateFileW("CONOUT$", GENERIC_RW, SHARE, None, OPEN, 0, None)
    if not h or h == wintypes.HANDLE(-1).value:
        sys.exit(1)
    fi = CFI()
    if not k.GetCurrentConsoleFont(h, False, ctypes.byref(fi)) or fi.dwFontSize.X == 0 or fi.dwFontSize.Y == 0:
        sys.exit(1)
    cw, ch = fi.dwFontSize.X, fi.dwFontSize.Y
    info = CSBI()
    if not k.GetConsoleScreenBufferInfo(h, ctypes.byref(info)):
        sys.exit(1)
    cols = max(20, pw // cw)
    rows = max(5,  (ph // ch) - 1)             # 1행 여유: 하단 입력줄이 잘리거나 밀리지 않게
    cols = min(cols, info.maxwin.X, 120)       # 모니터 한도 + 상한 120 클램프
    rows = min(rows, info.maxwin.Y)
    # 리사이즈 댄스: 창을 최소로 → 버퍼 설정 → 창을 목표로
    k.SetConsoleWindowInfo(h, True, ctypes.byref(SMALL_RECT(0, 0, 0, 0)))
    k.SetConsoleScreenBufferSize(h, COORD(cols, 9000))     # 스크롤백 넉넉히
    k.SetConsoleWindowInfo(h, True, ctypes.byref(SMALL_RECT(0, 0, cols - 1, rows - 1)))
finally:
    k.FreeConsole()
'''


def fit_console_async(pid, px_w, px_h):
    """콘솔 cell grid 를 컨테이너 픽셀크기에 맞춤. 헬퍼 프로세스로 격리 실행."""
    if px_w < 40 or px_h < 40:
        return
    try:
        subprocess.run(
            [PYTHONW, "-c", CONSOLE_FIT_CODE, str(pid), str(px_w), str(px_h)],
            creationflags=CREATE_NO_WINDOW, timeout=4,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except Exception:
        pass


# ── 메인 앱 ───────────────────────────────────────────────────────────
class App(ctk.CTk):
    def __init__(self):
        self.cfg = load_settings()
        ctk.set_appearance_mode(self.cfg["theme"])
        ctk.set_default_color_theme("blue")

        super().__init__()
        self.title("myah")
        self.geometry("1600x900")
        self.minsize(800, 500)

        self._main_tid   = win32process.GetWindowThreadProcessId(self.winfo_id())[0] if HAS_WIN32 else 0

        self._web_hwnd    = 0
        self._web_proc    = None
        self._web_gen     = 0          # 재탐색 레이스 방지 + 유니크 제목
        self._web_mode    = None       # "child" | "owner" | None
        self._web_port    = 0          # 크롬 원격 디버깅 포트
        self._cdp         = None       # CDPClient
        self._console_entries = []     # [(text, rowframe), ...]
        self._console_open = False
        self._term_hwnd   = 0
        self._term_proc   = None
        self._term_gen    = 0
        self._term_mode   = None
        self._term_fit_job = None      # 콘솔 fit 디바운스 핸들
        self._rgn_cache   = {}         # owner 창 클리핑 리전 캐시 {hwnd: (w,h)}
        self._pos_cache   = {}         # owner 창 위치 캐시 {hwnd: (x,y,w,h)} — 불필요한 MoveWindow 방지
        self._start_ts    = time.time()  # 시작 직후 10초는 떨림 임계값을 크게(둔감) 둔다
        self._preset_urls = []         # 현재 선택/폼의 URL 목록 (프리셋용)
        self._preview_mode = "web"     # "web"(크롬) | "app"(실행 명령으로 띄운 앱 창)
        self._app_cmd = ""             # app 모드의 마지막 실행 명령
        # 자동 재실행(파일 감시) 상태 — AI 무관, 순수 폴링 로직
        self._ar_sig = None            # 마지막으로 본 .py 수정시각 시그니처
        self._ar_last_change = 0.0     # 마지막 변화 감지 시각
        self._ar_pending = False       # 변화 후 '잠잠해지길' 기다리는 중
        self._running     = True

        self._build_ui()
        self._refresh_preset_dropdown()
        self._update_status()
        self._ensure_quickedit_default()    # conhost 가 항상 QuickEdit(복붙) 켜진 채 뜨도록
        # Ctrl+Shift+V → 붙여넣기 (myah 창에 포커스가 있을 때. 버튼과 동일 동작)
        self.bind_all("<Control-Shift-v>", lambda e: self._paste_to_term())
        self.bind_all("<Control-Shift-V>", lambda e: self._paste_to_term())
        # owner 폴백 창만 따라다니게 하는 동기화 루프 (자식창은 자동 이동 → 불필요)
        self.after(33, self._sync_tick)
        self.after(1000, self._auto_reload_tick)   # 파일 감시 → 잠잠해지면 자동 재실행
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        # 마지막 상태 자동 복원
        # 좌측 미리보기는 '폴더의 .myah/run'(또는 자동감지)로 복원한다. _auto_preview 가
        # _looks_like_command 로 웹/명령을 갈라 띄우므로, 명령에 https:// 가 붙는 버그가 없다.
        # 시작 인자로 폴더가 오면(run_ai.bat 런처) 그 폴더를 우선 — "그 프로젝트로 바로 열기".
        start_folder = ""
        try:
            for a in sys.argv[1:]:
                if a and os.path.isdir(a):
                    start_folder = os.path.abspath(a)
                    break
        except Exception:
            start_folder = ""

        if start_folder:
            self.folder_var.set(start_folder)
            self.after(700, lambda f=start_folder: self._auto_preview(f))
            self.after(2600, self._start_claude)       # UI(왼쪽) 먼저 자리잡은 뒤 AI(오른쪽)
            self.after(6000, self._refresh_web)
        elif self.cfg["folder_history"]:
            self.folder_var.set(self.cfg["folder_history"][0])
            self.after(700, lambda: self._auto_preview(self.folder_var.get().strip()))
            self.after(2600, self._start_claude)       # 한 번에 두 창을 안 띄움 — 순차
            self.after(6000, self._refresh_web)        # 타이밍 빗나가도 한 번 더(모드별 처리)
        elif self.cfg["url_history"]:
            # 폴더가 없을 때만 마지막 URL 복원 — 단, '명령'은 제외(웹 주소일 때만)
            last = self.cfg["url_history"][0].strip()
            if last and not self._looks_like_command(last):
                self.url_var.set(last)
                self.after(600, self._go_url)
                self.after(5000, self._refresh_web)

    # ── UI 구성 ───────────────────────────────────────────────────────
    def _build_ui(self):
        self.grid_rowconfigure(1, weight=1)
        self.grid_columnconfigure(0, weight=1)

        top = ctk.CTkFrame(self, height=36)
        top.grid(row=0, column=0, sticky="ew", padx=8, pady=(6, 0))
        top.grid_propagate(False)
        ctk.CTkLabel(top, text="myah",
                     font=ctk.CTkFont(size=13, weight="bold")).pack(side="left", padx=(12, 14))

        # ── 프로젝트 프리셋 (폴더+엔진+URL목록 한 묶음) ──
        ctk.CTkLabel(top, text="프리셋").pack(side="left", padx=(0, 4))
        self.preset_var = tk.StringVar(value="")
        self.preset_combo = ctk.CTkComboBox(top, variable=self.preset_var, values=[""],
                                            width=150, height=26, state="readonly",
                                            command=self._on_preset_selected)
        self.preset_combo.pack(side="left", padx=2, pady=4)
        ctk.CTkButton(top, text="+ 저장", width=64, height=26,
                      command=self._save_preset).pack(side="left", padx=2, pady=4)
        ctk.CTkButton(top, text="▶ 실행", width=64, height=26,
                      command=self._run_preset).pack(side="left", padx=2, pady=4)
        ctk.CTkButton(top, text="🗑", width=34, height=26, fg_color="transparent",
                      border_width=1, command=self._delete_preset).pack(side="left", padx=2, pady=4)

        # ── 스냅샷(되돌리기 안전판) — '모두 허용' 작업 전후로 사용 ──
        ctk.CTkButton(top, text="git백업", width=62, height=26, fg_color="transparent",
                      border_width=1, command=self._backup_git).pack(side="left", padx=(10, 2), pady=4)
        ctk.CTkButton(top, text="폴더백업", width=68, height=26, fg_color="transparent",
                      border_width=1, command=self._backup_folder).pack(side="left", padx=2, pady=4)
        ctk.CTkButton(top, text="되돌리기", width=68, height=26,
                      command=self._open_restore).pack(side="left", padx=2, pady=4)

        # 런처생성: 지금 작업 폴더에 run_ai.bat 생성 → 그 폴더에서 더블클릭하면
        #           부모 myah 가 '이 폴더 환경'으로 열려 작업을 이어감.
        ctk.CTkButton(top, text="런처생성", width=68, height=26, fg_color="transparent",
                      border_width=1, command=self._make_launcher).pack(side="left", padx=(10, 2), pady=4)

        self._theme_btn = ctk.CTkButton(top, text=self._theme_label(),
                                         width=90, height=26,
                                         command=self._toggle_theme)
        self._theme_btn.pack(side="right", padx=8, pady=4)
        self._status_lbl = ctk.CTkLabel(top, text="", anchor="e",
                                        font=ctk.CTkFont(size=11), text_color="#888")
        self._status_lbl.pack(side="right", padx=8, pady=4)

        self._paned = tk.PanedWindow(self, orient="horizontal",
                                     sashwidth=6, bg="#444", handlesize=0)
        self._paned.grid(row=1, column=0, sticky="nsew", padx=8, pady=6)

        self._left  = self._build_browser_panel(self._paned)
        self._right = self._build_claude_panel(self._paned)

        self._paned.add(self._left,  minsize=300)
        self._paned.add(self._right, minsize=280)

        self.after(120, self._init_sash)

    def _build_browser_panel(self, parent):
        frame = ctk.CTkFrame(parent)
        frame.grid_rowconfigure(1, weight=1)
        frame.grid_columnconfigure(0, weight=1)

        bar = ctk.CTkFrame(frame, height=40)
        bar.grid(row=0, column=0, sticky="ew", padx=4, pady=(4, 2))
        bar.grid_propagate(False)
        bar.grid_columnconfigure(0, weight=1)

        self.url_var = tk.StringVar()
        self.url_combo = ctk.CTkComboBox(bar, variable=self.url_var,
                                          values=self.cfg["url_history"],
                                          width=300, height=28)
        self.url_combo.grid(row=0, column=0, sticky="ew", padx=(6, 2), pady=5)
        self.url_combo.bind("<Return>", lambda _: self._go_smart())

        ctk.CTkButton(bar, text="이동", width=44, height=28,
                      command=self._go_url).grid(row=0, column=1, padx=2, pady=5)
        ctk.CTkButton(bar, text="▶앱", width=44, height=28,
                      command=self._go_app).grid(row=0, column=2, padx=2, pady=5)
        ctk.CTkButton(bar, text="📂", width=34, height=28,
                      command=self._pick_run_file).grid(row=0, column=3, padx=2, pady=5)
        # UI 자동 감지: 폴더 열면 .myah/run(있으면) → 없으면 .bat/.py(GUI) 자동 탐지 실행
        self.autodetect_var = tk.BooleanVar(value=bool(self.cfg.get("ui_autodetect", True)))
        ctk.CTkCheckBox(bar, text="자동", variable=self.autodetect_var,
                        width=54, checkbox_width=18, checkbox_height=18,
                        font=ctk.CTkFont(size=11),
                        command=self._on_autodetect_toggle).grid(row=0, column=4, padx=(4, 2), pady=5)
        ctk.CTkButton(bar, text="⟳", width=34, height=28,
                      command=self._refresh_web).grid(row=0, column=5, padx=2, pady=5)
        # 자동 재실행: 켜면 .py 가 바뀐 뒤 6초간 잠잠하면(=AI 작업 끝) 앱을 자동 재실행
        self.auto_reload_var = tk.BooleanVar(value=bool(self.cfg.get("auto_reload", False)))
        ctk.CTkCheckBox(bar, text="자동⟳", variable=self.auto_reload_var,
                        width=62, checkbox_width=18, checkbox_height=18,
                        font=ctk.CTkFont(size=11),
                        command=self._on_auto_reload_toggle).grid(row=0, column=6, padx=(4, 2), pady=5)
        ctk.CTkButton(bar, text="★", width=34, height=28,
                      fg_color="transparent", border_width=1,
                      command=self._add_bookmark).grid(row=0, column=7, padx=2, pady=5)
        ctk.CTkButton(bar, text="북마크▼", width=68, height=28,
                      fg_color="transparent", border_width=1,
                      command=self._show_bookmarks).grid(row=0, column=8, padx=(2, 2), pady=5)
        ctk.CTkButton(bar, text="📷", width=34, height=28,
                      command=self._open_capture).grid(row=0, column=9, padx=(2, 6), pady=5)

        # 웹뷰 컨테이너 (이 프레임의 HWND 가 자식창의 부모가 된다)
        self._web_container = tk.Frame(frame, bg="#111")
        self._web_container.grid(row=1, column=0, sticky="nsew", padx=2, pady=(0, 2))
        self._web_container.bind("<Configure>", self._on_web_configure)

        # 로드 실패/안내 라벨 (#3). 자식창이 뜨면 그 위를 덮으므로 평소엔 숨김.
        self._web_msg = tk.Label(self._web_container, bg="#111", fg="#bbb",
                                 font=("Segoe UI", 11))
        return frame

    def _build_claude_panel(self, parent):
        frame = ctk.CTkFrame(parent)
        frame.grid_rowconfigure(1, weight=1)
        frame.grid_columnconfigure(0, weight=1)

        bar = ctk.CTkFrame(frame, height=40)
        bar.grid(row=0, column=0, sticky="ew", padx=4, pady=(4, 2))
        bar.grid_propagate(False)
        bar.grid_columnconfigure(0, weight=1)

        self.folder_var = tk.StringVar()
        self.folder_combo = ctk.CTkComboBox(bar, variable=self.folder_var,
                                             values=self.cfg["folder_history"],
                                             width=160, height=28,
                                             command=self._on_folder_pick)
        self.folder_combo.grid(row=0, column=0, sticky="ew", padx=(6, 2), pady=5)
        self.folder_combo.bind("<Return>", lambda _: self._start_claude(confirm=True))

        ctk.CTkButton(bar, text="📁", width=34, height=28,
                      command=self._browse_folder).grid(row=0, column=1, padx=2, pady=5)

        # 엔진 선택 (claude / codex). 마지막 선택은 settings 에 기억.
        self.engine_var = tk.StringVar(
            value=self.cfg.get("engine", "claude") if self.cfg.get("engine") in ENGINES else "claude")
        self.engine_combo = ctk.CTkComboBox(bar, variable=self.engine_var,
                                            values=ENGINES, width=78, height=28,
                                            state="readonly",
                                            command=self._on_engine_change)
        self.engine_combo.grid(row=0, column=2, padx=2, pady=5)

        # 모델 선택 (엔진별 목록, settings 에서 추가·수정). "(기본)"=플래그 없이 CLI 기본.
        eng0 = self.engine_var.get()
        self.model_var = tk.StringVar(value=self.cfg.get("model_sel", {}).get(eng0, "(기본)"))
        self.model_combo = ctk.CTkComboBox(bar, variable=self.model_var,
                                           values=self._model_values(eng0), width=104, height=28,
                                           state="readonly", command=self._on_model_change)
        self.model_combo.grid(row=0, column=3, padx=2, pady=5)
        # 모델변경: 실행 중인 세션에 /model 전송 → 세션 유지한 채 claude 화면에서 모델 선택
        ctk.CTkButton(bar, text="모델변경", width=60, height=28,
                      command=self._send_model_cmd).grid(row=0, column=4, padx=2, pady=5)
        # 붙여넣기: 클립보드를 claude 입력창에 주입(우클릭/Ctrl+V 가 막힌 TUI 대응)
        ctk.CTkButton(bar, text="붙여넣기", width=64, height=28,
                      command=self._paste_to_term).grid(row=0, column=5, padx=2, pady=5)

        # 모두 허용(YOLO): 켜면 에이전트가 권한 확인 없이 진행. 켤 때 경고 확인.
        self.auto_yes_var = tk.BooleanVar(value=bool(self.cfg.get("auto_yes", False)))
        ctk.CTkCheckBox(bar, text="모두허용", variable=self.auto_yes_var,
                        width=86, checkbox_width=18, checkbox_height=18,
                        font=ctk.CTkFont(size=11),
                        command=self._on_auto_yes_toggle).grid(row=0, column=6, padx=(4, 2), pady=5)

        ctk.CTkButton(bar, text="▶ 시작", width=64, height=28,
                      command=lambda: self._start_claude(confirm=True)).grid(row=0, column=7, padx=2, pady=5)
        ctk.CTkButton(bar, text="⟳", width=34, height=28,
                      command=self._restart_claude).grid(row=0, column=8, padx=(2, 6), pady=5)

        self._term_container = tk.Frame(frame, bg="#111")
        self._term_container.grid(row=1, column=0, sticky="nsew", padx=2, pady=(0, 2))
        self._term_container.bind("<Configure>", self._on_term_configure)

        # ── 콘솔 패널 (오른쪽 패널 아래, 접이식) ───────────────────────
        cpanel = ctk.CTkFrame(frame, fg_color="transparent")
        cpanel.grid(row=2, column=0, sticky="ew", padx=2, pady=(0, 2))
        cpanel.grid_columnconfigure(0, weight=1)

        chdr = ctk.CTkFrame(cpanel, height=30)
        chdr.grid(row=0, column=0, sticky="ew")
        chdr.grid_columnconfigure(0, weight=1)
        self._console_btn = ctk.CTkButton(chdr, text="콘솔 ▸  (0)", height=24, anchor="w",
                                          fg_color="transparent", command=self._toggle_console)
        self._console_btn.grid(row=0, column=0, sticky="ew", padx=(4, 2), pady=3)
        ctk.CTkButton(chdr, text="전체 복사", width=72, height=24,
                      command=self._copy_all_console).grid(row=0, column=1, padx=2, pady=3)
        ctk.CTkButton(chdr, text="비우기", width=56, height=24,
                      fg_color="transparent", border_width=1,
                      command=self._clear_console).grid(row=0, column=2, padx=(2, 4), pady=3)

        self._console_body = ctk.CTkScrollableFrame(cpanel, height=170)
        self._console_body.grid(row=1, column=0, sticky="ew", pady=(2, 0))
        self._console_body.grid_columnconfigure(0, weight=1)
        self._console_body.grid_remove()        # 기본 접힘 (에러 들어오면 자동 펼침)
        self._update_console_header()
        return frame

    def _init_sash(self):
        w = self.winfo_width() - 16
        if w > 0:
            self._paned.sash_place(0, int(w * self.cfg.get("sash_ratio", 0.65)), 0)

    # ── 동기화 루프 (owner 폴백 창만 따라다니게) ─────────────────────────
    def _sync_tick(self):
        if not self._running:
            return
        if HAS_WIN32:
            try:
                if self._web_mode == "owner":
                    self._sync(self._web_hwnd, self._web_container)
                if self._term_mode == "owner":
                    self._sync(self._term_hwnd, self._term_container)
            except Exception:
                pass
        self.after(33, self._sync_tick)

    def _sync(self, hwnd, container):
        if not hwnd or not win32gui.IsWindow(hwnd):
            return
        x = container.winfo_rootx()
        y = container.winfo_rooty()
        w = container.winfo_width()
        h = container.winfo_height()
        if w <= 10 or h <= 10:
            return
        # 1px 미세 진동(배율/패널 재계산)으로 매 틱 MoveWindow 가 일어나면 창이 떨린다.
        # 시작 직후 10초는 레이아웃이 자리잡으며 흔들림이 크므로 임계값을 크게(≤4px 무시),
        # 그 뒤엔 평소대로(≤1px 무시) 정상 추적.
        thr = 4 if (time.time() - self._start_ts) < 10 else 1
        prev = self._pos_cache.get(hwnd)
        moved = (prev is None
                 or abs(prev[0] - x) > thr or abs(prev[1] - y) > thr
                 or abs(prev[2] - w) > thr or abs(prev[3] - h) > thr)
        if moved:
            move_to(hwnd, x, y, w, h)
            self._pos_cache[hwnd] = (x, y, w, h)
        # owner 창은 부모에 자동 클리핑되지 않는다. 콘솔(conhost)은 셀 단위라
        # 패널보다 작아지지 못하고, 게다가 자기 리사이즈/이동 때 우리가 건 리전을 리셋한다.
        # → 실제 창이 컨테이너보다 크면(= 못 줄어든 상태) 리전을 걸어 넘침을 잘라낸다.
        #   (이동이 일어난 틱엔 리셋됐을 수 있으니 다시 건다. 가만히 있을 땐 아무것도 안 해 repaint 0)
        try:
            l, t, r, b = win32gui.GetWindowRect(hwnd)
            actual_w, actual_h = r - l, b - t
            oversize = (actual_w > w + 6) or (actual_h > h + 6)
            if oversize:
                if moved or self._rgn_cache.get(hwnd) is None:
                    rgn = win32gui.CreateRectRgn(0, 0, max(1, w), max(1, h))
                    win32gui.SetWindowRgn(hwnd, rgn, True)   # 시스템이 rgn 소유 → 따로 삭제 X
                    self._rgn_cache[hwnd] = (w, h)
            elif self._rgn_cache.get(hwnd) is not None:
                win32gui.SetWindowRgn(hwnd, 0, True)         # 패널에 맞으면 리전 제거(전체 표시)
                self._rgn_cache[hwnd] = None
        except Exception:
            pass

    # ── 컨테이너 리사이즈 → 자식창 채우기 (이벤트 구동) ──────────────────
    def _on_web_configure(self, _evt=None):
        if not self._web_hwnd or not win32gui.IsWindow(self._web_hwnd):
            return
        w = self._web_container.winfo_width()
        h = self._web_container.winfo_height()
        if w <= 10 or h <= 10:
            return
        if self._web_mode == "child":
            win32gui.MoveWindow(self._web_hwnd, 0, 0, w, h, True)
        elif self._web_mode == "owner":
            self._sync(self._web_hwnd, self._web_container)   # 드래그 즉시 위치+클립 재적용

    def _on_term_configure(self, _evt=None):
        if not self._term_hwnd or not win32gui.IsWindow(self._term_hwnd):
            return
        w = self._term_container.winfo_width()
        h = self._term_container.winfo_height()
        if w <= 10 or h <= 10:
            return
        if self._term_mode == "child":
            win32gui.MoveWindow(self._term_hwnd, 0, 0, w, h, True)
        elif self._term_mode == "owner":
            self._sync(self._term_hwnd, self._term_container)  # 드래그 즉시 위치+클립 재적용
        if self._term_mode in ("child", "owner"):
            self._schedule_term_fit(w, h)   # 픽셀→컬럼 동적 환산 (디바운스)

    def _schedule_term_fit(self, w, h):
        if self._term_fit_job is not None:
            try:
                self.after_cancel(self._term_fit_job)
            except Exception:
                pass
        self._term_fit_job = self.after(160, lambda: self._run_term_fit(w, h))

    def _run_term_fit(self, w, h):
        self._term_fit_job = None
        if self._term_mode in ("child", "owner") and self._term_proc and self._term_proc.poll() is None:
            pid = self._term_proc.pid
            # 컨테이너에 거의 꽉 맞추되, 즉시 클리핑이 패널 밖 넘침을 잡아준다.
            threading.Thread(target=fit_console_async,
                             args=(pid, max(160, w - 10), h), daemon=True).start()

    # ── 웹 브라우저 (진짜 크롬을 통째로 박는다) ─────────────────────────
    #  · 박힌 크롬이 살아있으면 URL 입력 = 그 크롬에 "새 탭"으로 열기
    #    (탭·뒤로가기·새탭·devtools 는 크롬 자기 UI 가 다 처리)
    #  · 죽어있으면 전용 프로필로 새 인스턴스 띄워서 왼쪽 칸에 임베드
    def _go_url(self):
        url = self.url_var.get().strip()
        if not url:
            return
        if self._looks_like_command(url):     # 명령이면 절대 https:// 붙이지 말고 앱으로
            self._go_app()
            return
        if not url.startswith(("http://", "https://")):
            url = "https://" + url
            self.url_var.set(url)

        self.cfg["url_history"] = push_history(self.cfg["url_history"], url)
        self.url_combo.configure(values=self.cfg["url_history"])
        self._save_run_file(self._cur_folder(), self.url_var.get().strip())  # 기억 → 다음엔 자동

        if not CHROME_EXE:
            self._show_web_msg("⚠ Chrome 을 찾지 못했습니다.\n"
                               "Google Chrome 설치 여부를 확인하세요.")
            return
        self._hide_web_msg()

        if self._web_chrome_alive():
            self._open_tab(url)          # 이미 박힌 크롬에 새 탭으로
        else:
            self._launch_chrome(url)     # 새 인스턴스 띄워 임베드

    def _web_chrome_alive(self) -> bool:
        return bool(self._web_hwnd) and HAS_WIN32 and win32gui.IsWindow(self._web_hwnd)

    def _open_tab(self, url):
        """살아있는 임베드 크롬에 새 탭으로 URL 열기. (단명 런처 프로세스가 인스턴스에 전달)"""
        try:
            subprocess.Popen([CHROME_EXE, f"--user-data-dir={CHROME_PROFILE}", url],
                             creationflags=CREATE_NO_WINDOW)
        except Exception:
            pass

    def _launch_chrome(self, url):
        self._preview_mode = "web"
        self._web_gen += 1
        gen = self._web_gen
        threading.Thread(target=self._chrome_worker, args=(url, gen), daemon=True).start()

    def _chrome_worker(self, url, gen):
        """(백그라운드) 기존 인스턴스 종료 대기 → 스냅샷 → 크롬 실행 → 새 창 탐지 → 임베드 예약."""
        # 1) 이전 인스턴스 정리 후 창이 사라질 때까지 대기 (전용 프로필 attach 레이스 방지)
        old = self._web_hwnd
        self._stop_cdp()
        self._kill(self._web_proc)
        if old and HAS_WIN32:
            for _ in range(20):                      # 최대 ~2초
                if not win32gui.IsWindow(old):
                    break
                time.sleep(0.1)

        if not self._running or gen != self._web_gen:
            return

        # 2) 실행 직전 스냅샷 (이후 '새로 생긴' 크롬 창이 우리 것)
        port = free_port()
        before = list_chrome_windows()
        try:
            proc = subprocess.Popen(
                [CHROME_EXE,
                 f"--user-data-dir={CHROME_PROFILE}",
                 "--no-first-run", "--no-default-browser-check",
                 f"--remote-debugging-port={port}",     # CDP
                 "--remote-allow-origins=*",            # Chrome 111+ WS Origin 검사 회피
                 "--new-window", url],
                creationflags=CREATE_NO_WINDOW,
            )
        except Exception:
            self.after(0, lambda g=gen: self._web_failed(g))
            return
        self._web_proc = proc
        self._web_port = port
        try:                                           # MCP 서버가 읽을 포트 파일
            with open(PORT_FILE, "w", encoding="utf-8") as f:
                json.dump({"port": port}, f)
        except Exception:
            pass
        self._start_cdp(port)                          # 콘솔 수집 + reload 채널

        # 3) 새 메인 프레임 창 탐지
        for _ in range(60):                          # 최대 ~18초
            time.sleep(0.3)
            if not HAS_WIN32 or not self._running or gen != self._web_gen:
                return
            new = list_chrome_windows() - before
            if new:
                hwnd = next(iter(new))
                self.after(0, lambda h=hwnd, g=gen: self._embed_web(h, g))
                return
        if self._running and gen == self._web_gen:
            self.after(0, lambda g=gen: self._web_failed(g))

    def _embed_web(self, hwnd, gen):
        if not self._running or gen != self._web_gen or not win32gui.IsWindow(hwnd):
            return
        self._hide_web_msg()
        # 크롬도 터미널과 같은 owner(top-level) 방식으로 박는다.
        # 자식창(WS_CHILD)이면 다른 프로세스라 키보드 포커스가 안 넘어가 입력이 안 됨.
        # top-level 이면 클릭 시 정상 포커스. 위치는 _sync_tick 가, 경계는 리전이 잡음.
        # (크롬은 픽셀 단위 리사이즈라 콘솔 같은 최소폭 넘침 문제도 없음)
        make_borderless(hwnd)
        set_owner(hwnd, self.winfo_id())
        self._web_hwnd, self._web_mode = hwnd, "owner"
        self._sync(hwnd, self._web_container)

    def _web_failed(self, gen):
        if gen != self._web_gen:
            return
        self._web_hwnd, self._web_mode = 0, None
        self._show_web_msg("⚠ 크롬 창을 불러오지 못했습니다.\n"
                           "URL 또는 Chrome 실행 상태를 확인하세요.")

    # ── 앱 실행 미리보기 (실행 명령으로 임의 앱 창을 왼쪽에 임베드) ──────────
    def _go_app(self):
        """URL 칸의 텍스트를 '실행 명령'으로 보고 그 앱을 왼쪽에 띄운다.
        예: python main.py  /  python -m app  /  npm run dev 등."""
        cmd = self.url_var.get().strip()
        if not cmd:
            self._show_web_msg("실행 명령을 입력하세요 (예: python main.py)")
            self.after(2500, self._hide_web_msg)
            return
        self.cfg["url_history"] = push_history(self.cfg["url_history"], cmd)
        self.url_combo.configure(values=self.cfg["url_history"])
        save_settings(self.cfg)
        self._save_run_file(self._cur_folder(), cmd)   # 수동 실행도 기억 → 다음엔 자동
        self._close_chrome()
        self._run_app(cmd)

    # ── UI 자동 감지 / .myah/run 기억 ────────────────────────────────
    def _on_autodetect_toggle(self):
        self.cfg["ui_autodetect"] = bool(self.autodetect_var.get())
        save_settings(self.cfg)

    # ── 자동 재실행 (파일 감시 → 잠잠해지면 재실행) ──────────────────
    _AR_QUIET_SEC = 6.0               # 마지막 변화 후 이만큼 잠잠하면 'AI 작업 끝'으로 판정
    _AR_SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__",
                     ".myah", ".mypy_cache", "dist", "build", "캡처"}

    def _on_auto_reload_toggle(self):
        on = bool(self.auto_reload_var.get())
        self.cfg["auto_reload"] = on
        save_settings(self.cfg)
        # 켤 때 현재 상태를 기준선으로 잡아, 켜자마자 재실행되는 일이 없게 한다.
        self._ar_sig = self._app_py_sig(self._cur_folder()) if on else None
        self._ar_pending = False
        if on:
            self._toast("자동 재실행 ON — 파일 바뀌고 6초 잠잠하면 앱 재실행", ms=3000)

    def _app_py_sig(self, folder):
        """폴더 안 .py 파일들의 (경로, 수정시각) 시그니처. 변화 감지용. 제외 폴더는 건너뜀."""
        if not folder or not os.path.isdir(folder):
            return None
        total = 0.0
        count = 0
        try:
            for root, dirs, files in os.walk(folder):
                dirs[:] = [d for d in dirs if d not in self._AR_SKIP_DIRS]
                for fn in files:
                    if fn.endswith((".py", ".pyw")):
                        try:
                            total += os.path.getmtime(os.path.join(root, fn))
                            count += 1
                        except Exception:
                            pass
        except Exception:
            return None
        return (count, round(total, 2))

    def _auto_reload_tick(self):
        try:
            if (self.auto_reload_var.get() and self._preview_mode == "app"
                    and self._term_gen >= 0):
                folder = self._cur_folder()
                sig = self._app_py_sig(folder)
                if sig is not None:
                    if self._ar_sig is None:
                        self._ar_sig = sig            # 첫 기준선 — 재실행 안 함
                    elif sig != self._ar_sig:
                        self._ar_sig = sig            # 변화 감지 → 잠잠 타이머 시작
                        self._ar_last_change = time.time()
                        self._ar_pending = True
                    elif self._ar_pending and (time.time() - self._ar_last_change) >= self._AR_QUIET_SEC:
                        self._ar_pending = False      # 6초 잠잠 → 작업 끝으로 보고 재실행
                        self._refresh_web()
        except Exception:
            pass
        self.after(1000, self._auto_reload_tick)

    def _run_file_path(self, folder):
        return os.path.join(folder, ".myah", "run")

    def _save_run_file(self, folder, spec):
        if not folder or not os.path.isdir(folder) or not spec:
            return
        try:
            os.makedirs(os.path.join(folder, ".myah"), exist_ok=True)
            with open(self._run_file_path(folder), "w", encoding="utf-8") as f:
                f.write(spec.strip())
        except Exception:
            pass

    @staticmethod
    def _looks_like_command(spec):
        """입력이 '실행 명령'처럼 보이면 True(=앱), 아니면 False(=웹 주소)."""
        s = spec.strip()
        if not s:
            return False
        if " " in s:                                   # 'python main.py' 등
            return True
        low = s.lower()
        if low.endswith((".py", ".pyw", ".bat", ".cmd", ".exe", ".ps1")):
            return True
        if low.startswith(("python", "py ", "pyw", "npm", "node", "pnpm",
                           "yarn", "cmd", "powershell", ".\\", "./")):
            return True
        return False

    def _go_smart(self):
        """엔터: 내용을 보고 웹 주소면 이동, 실행 명령이면 앱으로 — 무조건 URL 변환 금지."""
        s = self.url_var.get().strip()
        if not s:
            return
        if self._looks_like_command(s):
            self._go_app()
        else:
            self._go_url()

    def _spec_from_file(self, path, folder):
        """선택한 파일 → 실행 사양. 폴더 안이면 상대경로로(작업폴더 기준 실행)."""
        rel = path
        try:
            if folder and os.path.commonpath([os.path.normpath(path),
                                              os.path.normpath(folder)]) == os.path.normpath(folder):
                rel = os.path.relpath(path, folder)
        except Exception:
            rel = path
        q = f'"{rel}"'
        low = path.lower()
        if low.endswith((".py", ".pyw")):
            return f"python {q}"
        return q                                        # .bat/.cmd/.exe/기타 → 그대로 실행

    def _pick_run_file(self):
        """📂 작업 폴더에서 실행할 파일을 골라 바로 띄운다."""
        folder = self._cur_folder()
        init = folder or os.path.expanduser("~")
        path = filedialog.askopenfilename(
            initialdir=init, title="실행할 파일 선택",
            filetypes=[("실행 가능 (py/bat/cmd/exe)", "*.py *.pyw *.bat *.cmd *.exe"),
                       ("모든 파일", "*.*")])
        if not path:
            return
        if not folder:
            folder = os.path.dirname(path)
            self.folder_var.set(folder)
        spec = self._spec_from_file(path, folder)
        self.url_var.set(spec)
        self.cfg["url_history"] = push_history(self.cfg["url_history"], spec)
        self.url_combo.configure(values=self.cfg["url_history"])
        save_settings(self.cfg)
        self._save_run_file(folder, spec)              # 기억 → 다음엔 자동
        self._close_chrome()
        self._run_app(spec)

    def _launch_from_spec(self, spec):
        """저장/감지된 실행 사양을 보고 웹(크롬)인지 앱(명령)인지 갈라 실행."""
        spec = spec.strip()
        if not spec:
            return
        self.url_var.set(spec)
        self._close_chrome()
        if self._looks_like_command(spec):
            self._run_app(spec)
        else:
            url = spec if spec.startswith(("http://", "https://")) else "http://" + spec
            self._launch_chrome(url)

    def _detect_run(self, folder):
        """폴더 최상위에서 실행 진입점을 내용 기반으로 탐지.
        .bat/.cmd(통째 실행) 와 GUI .py(tkinter/PyQt + __main__) 후보 중 최근 수정된 것."""
        cands = []   # (mtime, spec)
        try:
            names = os.listdir(folder)
        except Exception:
            return None
        for name in names:
            full = os.path.join(folder, name)
            if not os.path.isfile(full):
                continue
            low = name.lower()
            if low in ("run_ai.bat", "publish.bat", "run.bat", "install.bat"):
                continue                                       # myah 자체 런처/배치는 실행 대상 아님
            try:
                mt = os.path.getmtime(full)
            except Exception:
                mt = 0
            if low.endswith((".bat", ".cmd")):
                cands.append((mt, name))                       # .bat 은 통째로 실행
            elif low.endswith(".py"):
                try:
                    with open(full, encoding="utf-8", errors="ignore") as f:
                        txt = f.read()
                except Exception:
                    continue
                has_gui = any(k in txt for k in
                              ("tkinter", "customtkinter", "PyQt5", "PyQt6",
                               "PySide", "import wx", "kivy"))
                has_main = "__main__" in txt
                if has_gui and has_main:
                    cands.append((mt, f'python "{name}"'))
        if not cands:
            return None
        cands.sort(key=lambda x: x[0], reverse=True)           # 최근 수정 먼저
        return cands[0][1]

    def _auto_preview(self, folder):
        """폴더 지정 시: .myah/run 있으면 그걸로, 없고 자동감지 ON 이면 탐지해서 자동 실행."""
        if not folder or not os.path.isdir(folder):
            return
        rf = self._run_file_path(folder)
        if os.path.exists(rf):
            try:
                spec = open(rf, encoding="utf-8").read().strip()
            except Exception:
                spec = ""
            if spec:
                self._launch_from_spec(spec)
                return
        if not self.autodetect_var.get():
            return
        spec = self._detect_run(folder)
        if spec:
            self._save_run_file(folder, spec)                  # 다음부턴 추측 안 함
            self._launch_from_spec(spec)

    def _run_app(self, cmd):
        self._preview_mode = "app"
        self._app_cmd = cmd
        self._web_gen += 1
        self._show_web_msg("앱 실행 중…")
        threading.Thread(target=self._app_worker, args=(cmd, self._web_gen), daemon=True).start()

    def _app_worker(self, cmd, gen):
        """(백그라운드) 기존 정리 → 스냅샷 → 명령 실행(작업 폴더 기준) → 새 창 탐지 → 임베드."""
        old = self._web_hwnd
        self._stop_cdp()
        self._kill(self._web_proc)
        if old and HAS_WIN32:
            for _ in range(20):
                if not win32gui.IsWindow(old):
                    break
                time.sleep(0.1)
        if not self._running or gen != self._web_gen:
            return
        folder = self.folder_var.get().strip()
        if not folder or not os.path.isdir(folder):
            folder = os.path.expanduser("~")
        before = set(list_top_windows(os.getpid()).keys())
        try:
            proc = subprocess.Popen(cmd, cwd=folder, shell=True, creationflags=CREATE_NO_WINDOW)
        except Exception:
            self.after(0, lambda g=gen: self._app_failed(g))
            return
        self._web_proc = proc
        self._web_port = 0
        # 새로 생긴 top-level 창 탐지 (proc pid 우선, 없으면 스냅샷 diff)
        for _ in range(60):                          # 최대 ~18초
            time.sleep(0.3)
            if not HAS_WIN32 or not self._running or gen != self._web_gen:
                return
            cur = list_top_windows(os.getpid())
            match = [h for h, p in cur.items() if p == proc.pid and h not in before]
            if not match:
                match = [h for h in cur if h not in before]
            if match:
                hwnd = match[0]
                self.after(0, lambda h=hwnd, g=gen: self._embed_web(h, g))
                return
            if proc.poll() is not None and not (set(cur) - before):
                break                                # 프로세스가 창 없이 즉시 종료됨
        if self._running and gen == self._web_gen:
            self.after(0, lambda g=gen: self._app_failed(g))

    def _app_failed(self, gen):
        if gen != self._web_gen:
            return
        self._web_hwnd, self._web_mode = 0, None
        self._show_web_msg("⚠ 앱 창을 불러오지 못했습니다.\n"
                           "실행 명령과 폴더를 확인하세요. (창을 띄우는 앱이어야 합니다)")

    def _show_web_msg(self, text):
        self._web_msg.configure(text=text)
        self._web_msg.place(relx=0.5, rely=0.5, anchor="center")

    def _hide_web_msg(self):
        try:
            self._web_msg.place_forget()
        except Exception:
            pass

    def _refresh_web(self):
        # 앱 모드: 고친 코드로 재실행(죽이고 다시 띄움)
        if self._preview_mode == "app":
            if self._app_cmd:
                self._close_chrome()
                self._run_app(self._app_cmd)
            return
        # 웹 모드: CDP 로 진짜 reload (탭·상태 유지). 안 되면 크롬 재시작으로 폴백.
        if HAS_WS and self._cdp and self._web_chrome_alive() and self._cdp.reload():
            return
        url = self.url_var.get().strip() or (self.cfg["url_history"][0] if self.cfg["url_history"] else "")
        if not url:
            return
        self._close_chrome()
        self._launch_chrome(url if url.startswith(("http://", "https://")) else "https://" + url)

    def _close_chrome(self):
        """임베드된 크롬 창 닫기 + CDP/프로세스 정리."""
        self._stop_cdp()
        if self._web_hwnd and HAS_WIN32 and win32gui.IsWindow(self._web_hwnd):
            try:
                win32gui.PostMessage(self._web_hwnd, win32con.WM_CLOSE, 0, 0)
            except Exception:
                pass
        self._kill(self._web_proc)
        self._web_hwnd, self._web_mode = 0, None

    def _close_term(self):
        """실행 중인 에이전트 터미널 닫기."""
        self._term_gen += 1
        self._kill(self._term_proc)
        self._term_proc = None
        self._term_hwnd, self._term_mode = 0, None

    # ── 프로젝트 프리셋 ──────────────────────────────────────────────
    def _refresh_preset_dropdown(self):
        names = sorted(self.cfg.get("presets", {}).keys())
        self.preset_combo.configure(values=names if names else [""])
        cur = self.cfg.get("last_preset", "")
        if cur in names:
            self.preset_var.set(cur)

    def _update_status(self):
        try:
            folder = self.folder_var.get().strip()
            engine = self.engine_var.get().strip()
            name = self.preset_var.get().strip()
            n = len(self._preset_urls)
            parts = []
            if name:
                parts.append(name)
            if folder:
                parts.append(os.path.basename(folder.rstrip("\\/")) or folder)
            if engine:
                parts.append(engine)
            if n:
                parts.append(f"{n} tab" + ("s" if n > 1 else ""))
            self._status_lbl.configure(text="  ·  ".join(parts) if parts else "프리셋 없음")
        except Exception:
            pass

    def _collect_open_urls(self):
        """현재 임베드 크롬에 열려 있는 탭 URL 목록을 CDP 로 수집(없으면 URL 칸 값)."""
        urls = []
        try:
            port = self._cdp.port if self._cdp else self._web_port
            if port:
                raw = urllib.request.urlopen(f"http://127.0.0.1:{port}/json", timeout=2).read()
                for t in json.loads(raw.decode("utf-8", "ignore")):
                    if t.get("type") == "page":
                        u = t.get("url", "")
                        if u and not u.startswith(("devtools://", "chrome://")) and u != "about:blank":
                            urls.append(u)
        except Exception:
            pass
        seen, out = set(), []
        for u in urls:
            if u not in seen:
                seen.add(u); out.append(u)
        if not out:
            u = self.url_var.get().strip()
            if u:
                out = [u if u.startswith(("http://", "https://")) else "https://" + u]
        return out

    def _on_preset_selected(self, name=None):
        """프리셋 선택 → 폼에 채우기만(2.b). 실행은 ▶ 실행 으로."""
        name = (name or self.preset_var.get()).strip()
        p = self.cfg.get("presets", {}).get(name)
        if not p:
            return
        self.folder_var.set(p.get("folder", ""))
        eng = p.get("engine", "claude")
        self.engine_var.set(eng if eng in ENGINES else "claude")
        self._preset_urls = list(p.get("urls", []))
        if self._preset_urls:
            self.url_var.set(self._preset_urls[0])
        self.cfg["last_preset"] = name
        save_settings(self.cfg)
        self._update_status()
        self._toast(f"프리셋 '{name}' 불러옴 — ▶ 실행 을 누르세요", ms=1800)

    def _save_preset(self):
        folder = self.folder_var.get().strip()
        engine = self.engine_var.get().strip()
        if engine not in ENGINES:
            engine = "claude"
        urls = self._collect_open_urls()        # 열린 탭 자동 수집
        default = self.preset_var.get().strip() or (os.path.basename(folder.rstrip("\\/")) if folder else "")
        name = self._prompt_text("프리셋 저장", "이름", default)
        if not name:
            return
        name = name.strip()
        presets = self.cfg.setdefault("presets", {})
        if name in presets and self._ask_dup_simple(name) is False:
            return
        presets[name] = {"folder": folder, "engine": engine, "urls": urls}
        self.cfg["last_preset"] = name
        save_settings(self.cfg)
        self._preset_urls = urls
        self._refresh_preset_dropdown()
        self.preset_var.set(name)
        self._update_status()
        self._toast(f"프리셋 '{name}' 저장됨 (URL {len(urls)}개)")

    def _run_preset(self):
        """현재 폼(폴더/엔진/URL목록)으로 실행 — 기존 크롬·터미널 닫고 새로 시작."""
        urls = self._preset_urls or self._collect_open_urls()
        if not urls:
            u = self.url_var.get().strip()
            urls = [u if u.startswith(("http://", "https://")) else "https://" + u] if u else []
        # 1) 기존 정리
        self._close_chrome()
        self._close_term()
        # 2) 크롬: 첫 URL 임베드 → 나머지 탭으로
        if urls:
            self.url_var.set(urls[0])
            self._launch_chrome(urls[0])
            for i, u in enumerate(urls[1:], start=1):
                self.after(2500 + i * 700, lambda u=u: self._open_tab(u))
        # 3) 터미널 시작(엔진) — mcp/PROGRESS/AGENTS 주입은 _start_claude 가 처리
        self.after(1200, self._start_claude)
        self._update_status()
        self._toast("프리셋 실행 — 크롬·터미널 시작", ms=1800)

    def _delete_preset(self):
        name = self.preset_var.get().strip()
        if not name or name not in self.cfg.get("presets", {}):
            self._toast("삭제할 프리셋이 없습니다")
            return
        if self._ask_dup_simple(name, verb="삭제") is False:
            return
        del self.cfg["presets"][name]
        if self.cfg.get("last_preset") == name:
            self.cfg["last_preset"] = ""
        save_settings(self.cfg)
        self.preset_var.set("")
        self._refresh_preset_dropdown()
        self._update_status()
        self._toast(f"프리셋 '{name}' 삭제됨")

    # ── 스냅샷 / 되돌리기 (개발자 git 히스토리 무손상) ──────────────────
    _SNAP_EXCLUDE = (".git", ".venv", "venv", "node_modules", "__pycache__",
                     ".myah", "캡처", ".mypy_cache", "dist", "build")

    def _cur_folder(self):
        f = self.folder_var.get().strip()
        return f if f and os.path.isdir(f) else None

    def _make_launcher(self):
        """지금 작업 폴더에 run_ai.bat 생성. 더블클릭하면 부모 myah(현재 실행 중인 그것)를
        이 폴더를 작업폴더로 열어 — 같은 환경에서 작업을 이어간다. 부모 경로는 자동으로 박는다."""
        folder = self._cur_folder()
        if not folder:
            self._toast("먼저 작업 폴더를 지정하세요")
            return
        py = sys.executable                       # 현재 실행한 파이썬(부모 .venv pythonw 등)
        # GUI 라 콘솔 안 뜨게 pythonw 선호 (python.exe 면 같은 폴더의 pythonw.exe 로 치환)
        if py.lower().endswith("python.exe"):
            cand = os.path.join(os.path.dirname(py), "pythonw.exe")
            if os.path.exists(cand):
                py = cand
        try:
            myah_py = os.path.abspath(sys.argv[0] if sys.argv and sys.argv[0] else __file__)
        except Exception:
            myah_py = os.path.abspath(__file__)
        bat = (
            "@echo off\r\n"
            "rem myah 런처 — 부모 myah 를 이 폴더 작업폴더로 실행 (자동 생성)\r\n"
            f'start "" "{py}" "{myah_py}" "%~dp0"\r\n'
        )
        path = os.path.join(folder, "run_ai.bat")
        try:
            # cmd 는 한국 Windows 에서 배치파일을 CP949(시스템 ANSI)로 읽는다.
            # UTF-8 로 저장하면 한글 경로가 깨지므로 CP949 로 저장. (불가 문자는 무시)
            with open(path, "w", encoding="cp949", errors="replace") as f:
                f.write(bat)
            self._toast("run_ai.bat 생성 ✓ — 이 폴더에서 더블클릭하면 같은 환경으로 열림")
        except Exception as e:
            self._toast(f"생성 실패: {e}", ms=2500)

    def _git(self, folder, args, env=None):
        try:
            r = subprocess.run(["git", "-C", folder] + args,
                               capture_output=True, text=True, env=env)
            return r.stdout.strip() if r.returncode == 0 else None
        except Exception:
            return None

    def _is_git(self, folder):
        return self._git(folder, ["rev-parse", "--is-inside-work-tree"]) == "true"

    def _make_git_snapshot(self, folder, ts):
        """임시 인덱스로 스냅샷 커밋 생성 + myah/snap/<ts> 태그.
        사용자의 HEAD/브랜치/인덱스/작업트리는 전혀 건드리지 않는다."""
        myah_dir = os.path.join(folder, ".myah")
        os.makedirs(myah_dir, exist_ok=True)
        tmp_index = os.path.join(myah_dir, "tmp_index")
        env = {**os.environ, "GIT_INDEX_FILE": tmp_index}
        has_head = self._git(folder, ["rev-parse", "HEAD"]) is not None
        if has_head:
            self._git(folder, ["read-tree", "HEAD"], env=env)
        # .myah(우리 백업/임시 파일)는 스냅샷에서 제외 — 사용자 .gitignore 는 건드리지 않음
        self._git(folder, ["add", "-A", "--", ".", ":!.myah"], env=env)
        tree = self._git(folder, ["write-tree"], env=env)
        try:
            os.remove(tmp_index)
        except Exception:
            pass
        if not tree:
            return None
        args = ["commit-tree", tree]
        parent = self._git(folder, ["rev-parse", "HEAD"]) if has_head else None
        if parent:
            args += ["-p", parent]
        args += ["-m", f"myah snapshot {ts}"]
        commit = self._git(folder, args, env=env)
        if not commit:
            return None
        tag = f"myah/snap/{ts}"
        self._git(folder, ["tag", tag, commit])
        return tag

    def _backup_git(self):
        folder = self._cur_folder()
        if not folder:
            self._toast("⚠ 먼저 폴더를 여세요"); return
        if not self._is_git(folder):
            self._toast("⚠ git 저장소가 아닙니다 — 폴더백업을 쓰거나 git init 하세요", ms=3000); return
        ts = time.strftime("%Y%m%d_%H%M%S")
        tag = self._make_git_snapshot(folder, ts)
        self._toast(f"git 백업됨 ✓ {tag}" if tag else "⚠ git 백업 실패",
                    ms=2500 if tag else 3000)

    def _backup_folder(self):
        folder = self._cur_folder()
        if not folder:
            self._toast("⚠ 먼저 폴더를 여세요"); return
        ts = time.strftime("%Y%m%d_%H%M%S")
        dst = os.path.join(folder, ".myah", "snapshots", ts)
        self._toast("폴더 백업 중…", ms=1500)
        threading.Thread(target=self._do_folder_backup, args=(folder, dst, ts), daemon=True).start()

    def _do_folder_backup(self, folder, dst, ts):
        try:
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copytree(folder, dst, ignore=shutil.ignore_patterns(*self._SNAP_EXCLUDE))
            self.after(0, lambda: self._toast(f"폴더 백업됨 ✓ {ts}"))
        except Exception as e:
            self.after(0, lambda e=e: self._toast(f"⚠ 폴더 백업 실패: {e}", ms=3000))

    def _list_snapshots(self, folder):
        """[(정렬키, 라벨, kind, ref)] — git 태그 + 폴더 스냅샷."""
        items = []
        if self._is_git(folder):
            tags = self._git(folder, ["tag", "-l", "myah/snap/*"]) or ""
            for t in tags.splitlines():
                t = t.strip()
                if t:
                    ts = t.split("/")[-1]
                    items.append((ts, f"[git]   {self._pretty_ts(ts)}", "git", t))
        snaps = os.path.join(folder, ".myah", "snapshots")
        if os.path.isdir(snaps):
            for d in os.listdir(snaps):
                full = os.path.join(snaps, d)
                if os.path.isdir(full):
                    items.append((d, f"[폴더] {self._pretty_ts(d)}", "folder", full))
        items.sort(key=lambda x: x[0], reverse=True)   # 최신 먼저
        return items

    @staticmethod
    def _pretty_ts(ts):
        try:
            return f"{ts[4:6]}-{ts[6:8]} {ts[9:11]}:{ts[11:13]}"
        except Exception:
            return ts

    def _open_restore(self):
        folder = self._cur_folder()
        if not folder:
            self._toast("⚠ 먼저 폴더를 여세요"); return
        items = self._list_snapshots(folder)
        if not items:
            self._toast("백업이 없습니다 — 먼저 백업하세요", ms=2500); return
        dlg = ctk.CTkToplevel(self)
        dlg.title("되돌리기 — 백업 선택")
        dlg.geometry("380x360")
        dlg.transient(self); dlg.lift(); dlg.grab_set()
        ctk.CTkLabel(dlg, text="복원할 백업을 고르세요\n(복원 전 현재 상태가 자동 백업됩니다)",
                     justify="left").pack(pady=(14, 8), padx=16, anchor="w")
        scroll = ctk.CTkScrollableFrame(dlg, width=340, height=230)
        scroll.pack(padx=14, pady=4, fill="both", expand=True)
        for _key, label, kind, ref in items:
            ctk.CTkButton(scroll, text=label, anchor="w", height=30,
                          command=lambda k=kind, r=ref, l=label, d=dlg: self._do_restore(folder, k, r, l, d)
                          ).pack(fill="x", pady=3)

    def _do_restore(self, folder, kind, ref, label, dlg):
        try:
            dlg.destroy()
        except Exception:
            pass
        if not self._confirm("되돌리기 확인",
                             f"{label}\n\n이 시점으로 복원할까요?\n현재 상태는 먼저 자동 백업됩니다.",
                             ok_text="복원"):
            return
        ts = time.strftime("%Y%m%d_%H%M%S")
        if kind == "git":
            self._make_git_snapshot(folder, ts)                  # 복원 전 현재 자동 백업
            self._git(folder, ["restore", "--source=" + ref, "--", "."])
            self._progress_note_restore(folder, label)
            self._toast(f"복원됨 ✓ {label}", ms=2500)
        else:
            # 폴더 복원: 현재를 먼저 폴더백업 → 스냅샷 내용을 덮어쓰기 (스레드)
            cur_dst = os.path.join(folder, ".myah", "snapshots", ts)
            threading.Thread(target=self._do_folder_restore,
                             args=(folder, ref, cur_dst, label), daemon=True).start()

    def _do_folder_restore(self, folder, snap_dir, cur_dst, label):
        try:
            os.makedirs(os.path.dirname(cur_dst), exist_ok=True)
            shutil.copytree(folder, cur_dst, ignore=shutil.ignore_patterns(*self._SNAP_EXCLUDE))
            shutil.copytree(snap_dir, folder, dirs_exist_ok=True)
            self._progress_note_restore(folder, label)
            self.after(0, lambda: self._toast(f"복원됨 ✓ {label}", ms=2500))
        except Exception as e:
            self.after(0, lambda e=e: self._toast(f"⚠ 복원 실패: {e}", ms=3000))

    def _progress_note_restore(self, folder, label):
        """되돌린 사실을 PROGRESS.md 에 기록 → 다음 세션 AI 가 상태를 오해하지 않게."""
        try:
            myah_dir = os.path.join(folder, ".myah")
            os.makedirs(myah_dir, exist_ok=True)
            path = os.path.join(myah_dir, "PROGRESS.md")
            note = (f"\n> ⟲ [되돌림] {time.strftime('%Y-%m-%d %H:%M')} — {label} 스냅샷으로 복원됨.\n"
                    f">    파일이 이 시점으로 되돌려졌습니다. 위의 '한 일'이 실제와 다를 수 있으니,\n"
                    f">    현재 파일 상태를 기준으로 확인하고 이어서 작업하세요.\n")
            with open(path, "a", encoding="utf-8") as f:
                f.write(note)
        except Exception:
            pass

    def _prompt_text(self, title, label, default=""):
        """간단한 텍스트 입력 모달. 반환: 문자열 또는 None."""
        dlg = ctk.CTkToplevel(self)
        dlg.title(title); dlg.geometry("320x150")
        dlg.transient(self); dlg.lift(); dlg.grab_set()
        self._dlg_result = None
        ctk.CTkLabel(dlg, text=label).pack(pady=(18, 6), padx=16, anchor="w")
        var = tk.StringVar(value=default)
        ent = ctk.CTkEntry(dlg, textvariable=var); ent.pack(fill="x", padx=16); ent.focus_set()

        def ok():
            self._dlg_result = var.get(); dlg.destroy()

        def cancel():
            self._dlg_result = None; dlg.destroy()

        ent.bind("<Return>", lambda e: ok()); ent.bind("<Escape>", lambda e: cancel())
        row = ctk.CTkFrame(dlg, fg_color="transparent"); row.pack(pady=14)
        ctk.CTkButton(row, text="확인", width=90, command=ok).pack(side="left", padx=6)
        ctk.CTkButton(row, text="취소", width=90, fg_color="transparent",
                      border_width=1, command=cancel).pack(side="left", padx=6)
        dlg.protocol("WM_DELETE_WINDOW", cancel)
        self.wait_window(dlg)
        return self._dlg_result

    def _ask_dup_simple(self, name, verb="덮어쓰기"):
        """예/아니오 확인 모달. 반환: True/False."""
        dlg = ctk.CTkToplevel(self)
        dlg.title("확인"); dlg.geometry("320x140")
        dlg.transient(self); dlg.lift(); dlg.grab_set()
        self._dlg_result = False
        msg = f"'{name}' 을(를) {verb} 할까요?"
        ctk.CTkLabel(dlg, text=msg, justify="left").pack(pady=(22, 12), padx=16)

        def yes():
            self._dlg_result = True; dlg.destroy()

        def no():
            self._dlg_result = False; dlg.destroy()

        row = ctk.CTkFrame(dlg, fg_color="transparent"); row.pack()
        ctk.CTkButton(row, text=verb, width=90, command=yes).pack(side="left", padx=6)
        ctk.CTkButton(row, text="취소", width=90, fg_color="transparent",
                      border_width=1, command=no).pack(side="left", padx=6)
        dlg.protocol("WM_DELETE_WINDOW", no)
        self.wait_window(dlg)
        return self._dlg_result

    def _on_auto_yes_toggle(self):
        """'모두 허용' 켤 때 — 되돌리기 안전장치(git/백업) 경고를 확인받는다."""
        if not self.auto_yes_var.get():
            self.cfg["auto_yes"] = False
            save_settings(self.cfg)
            return
        msg = ("'모두 허용'을 켜면 에이전트가 파일 수정·삭제·명령 실행을\n"
               "확인 없이 끝까지 자동으로 진행합니다.\n\n"
               "되돌릴 수 있도록, 현재 작업이 git 저장소나\n"
               "다른 백업(하드드라이브 등)에 저장되어 있어야 합니다.\n"
               "그래야 잘못되더라도 이전 상태로 되돌릴 수 있습니다.\n\n"
               "켤까요?")
        if self._confirm("모두 허용 (주의)", msg, ok_text="켜기"):
            self.cfg["auto_yes"] = True
            save_settings(self.cfg)
            self._toast("모두 허용 ON — 다음 시작부터 적용", ms=2000)
        else:
            self.auto_yes_var.set(False)
            self.cfg["auto_yes"] = False
            save_settings(self.cfg)

    def _confirm(self, title, message, ok_text="확인"):
        """일반 확인 모달. 반환: True/False."""
        dlg = ctk.CTkToplevel(self)
        dlg.title(title); dlg.geometry("400x230")
        dlg.transient(self); dlg.lift(); dlg.grab_set()
        self._dlg_result = False
        ctk.CTkLabel(dlg, text=message, justify="left").pack(pady=(20, 14), padx=18)

        def yes():
            self._dlg_result = True; dlg.destroy()

        def no():
            self._dlg_result = False; dlg.destroy()

        row = ctk.CTkFrame(dlg, fg_color="transparent"); row.pack()
        ctk.CTkButton(row, text=ok_text, width=100, command=yes).pack(side="left", padx=6)
        ctk.CTkButton(row, text="취소", width=100, fg_color="transparent",
                      border_width=1, command=no).pack(side="left", padx=6)
        dlg.protocol("WM_DELETE_WINDOW", no)
        self.wait_window(dlg)
        return self._dlg_result

    # ── 화면 캡처 (CDP) ──────────────────────────────────────────────
    _REGION_OVERLAY_JS = r"""(function(){
      if (window.__bcOv) return 'busy';
      window.__bcRegion = null;
      var ov=document.createElement('div'); window.__bcOv=ov;
      ov.style.cssText='position:fixed;inset:0;z-index:2147483647;cursor:crosshair;background:rgba(0,0,0,0.25)';
      var box=document.createElement('div');
      box.style.cssText='position:fixed;border:2px solid #4da3ff;background:rgba(77,163,255,0.15);display:none;pointer-events:none';
      ov.appendChild(box); document.body.appendChild(ov);
      var sx,sy,drag=false;
      ov.addEventListener('mousedown',function(e){drag=true;sx=e.clientX;sy=e.clientY;box.style.display='block';box.style.left=sx+'px';box.style.top=sy+'px';box.style.width='0px';box.style.height='0px';e.preventDefault();});
      ov.addEventListener('mousemove',function(e){if(!drag)return;var x=Math.min(sx,e.clientX),y=Math.min(sy,e.clientY),w=Math.abs(e.clientX-sx),h=Math.abs(e.clientY-sy);box.style.left=x+'px';box.style.top=y+'px';box.style.width=w+'px';box.style.height=h+'px';});
      ov.addEventListener('mouseup',function(e){if(!drag)return;drag=false;var x=Math.min(sx,e.clientX),y=Math.min(sy,e.clientY),w=Math.abs(e.clientX-sx),h=Math.abs(e.clientY-sy);if(window.__bcOv){document.body.removeChild(window.__bcOv);window.__bcOv=null;}if(w<5||h<5){window.__bcRegion={cancel:true};return;}window.__bcRegion={x:x+window.scrollX,y:y+window.scrollY,width:w,height:h};});
      function key(e){if(e.key==='Escape'){if(window.__bcOv){document.body.removeChild(window.__bcOv);window.__bcOv=null;}window.__bcRegion={cancel:true};document.removeEventListener('keydown',key);}}
      document.addEventListener('keydown',key);
      return 'ok';
    })()"""

    @staticmethod
    def _safe_filename(name):
        name = (name or "").strip()
        for ch in '\\/:*?"<>|':
            name = name.replace(ch, "_")
        return name[:80]

    def _toast(self, msg, ms=2200):
        try:
            self._show_web_msg(msg)
            self.after(ms, self._hide_web_msg)
        except Exception:
            pass

    # 📷 → 범위 선택(전체/구간)
    def _open_capture(self):
        if not (HAS_WS and self._cdp and self._web_chrome_alive()):
            self._toast("⚠ 캡처하려면 먼저 왼쪽에 페이지를 띄우세요.")
            return
        win = ctk.CTkToplevel(self)
        win.title("화면 캡처")
        win.geometry("300x150")
        win.transient(self)
        win.lift()
        win.grab_set()
        self._cap_win = win
        ctk.CTkLabel(win, text="무엇을 캡처할까요?").pack(pady=(18, 10))
        row = ctk.CTkFrame(win, fg_color="transparent")
        row.pack()
        ctk.CTkButton(row, text="전체 페이지", width=120,
                      command=lambda: self._cap_choose("full")).pack(side="left", padx=6)
        ctk.CTkButton(row, text="구간 지정", width=120,
                      command=lambda: self._cap_choose("region")).pack(side="left", padx=6)
        ctk.CTkButton(win, text="취소", width=80, fg_color="transparent", border_width=1,
                      command=win.destroy).pack(pady=(14, 0))

    def _cap_choose(self, mode):
        try:
            self._cap_win.destroy()             # 선택창 닫기(드래그 위해 grab 해제)
        except Exception:
            pass
        if mode == "region":
            self._toast("구간을 드래그하세요 (Esc 취소)", ms=1500)
        threading.Thread(target=self._capture_worker, args=(mode,), daemon=True).start()

    # 백그라운드: 캡처해서 PNG bytes 만 확보 → 메인스레드로 이름/저장 넘김
    def _capture_worker(self, mode):
        cdp = self._cdp
        if not cdp:
            self.after(0, lambda: self._toast("⚠ CDP 연결 없음"))
            return
        clip = None
        if mode == "region":
            cdp.eval_js(self._REGION_OVERLAY_JS)
            rect = None
            for _ in range(300):                # 최대 ~60초 대기
                if not self._running:
                    return
                s = cdp.eval_js("JSON.stringify(window.__bcRegion===undefined?null:window.__bcRegion)")
                if s and s != "null":
                    try:
                        rect = json.loads(s)
                    except Exception:
                        rect = None
                    break
                time.sleep(0.2)
            if not rect or rect.get("cancel"):
                self.after(0, lambda: self._toast("구간 선택 취소됨"))
                return
            clip = {"x": rect["x"], "y": rect["y"], "width": rect["width"],
                    "height": rect["height"], "scale": 1}
        png = cdp.capture(clip=clip, full=(mode == "full"))
        if not png:
            self.after(0, lambda: self._toast("⚠ 캡처 실패"))
            return
        self.after(0, lambda: self._finish_capture(png))

    # 메인스레드: 이름 입력 → (중복 시) 선택지 → 저장
    def _finish_capture(self, png):
        name = self._ask_name()
        if name is None:
            self._toast("취소됨")
            return
        base = self._safe_filename(name) or time.strftime("capture_%Y%m%d_%H%M%S")
        folder = self.folder_var.get().strip()
        if not folder or not os.path.isdir(folder):
            folder = os.path.expanduser("~")
        cap_dir = os.path.join(folder, "캡처")
        try:
            os.makedirs(cap_dir, exist_ok=True)
        except Exception as e:
            self._toast(f"⚠ 폴더 생성 실패: {e}")
            return
        path = os.path.join(cap_dir, base + ".png")
        if os.path.exists(path):
            choice = self._ask_dup(base)        # 'overwrite' / 'number' / None
            if choice is None:
                self._toast("취소됨")
                return
            if choice == "number":
                n = 1
                while os.path.exists(os.path.join(cap_dir, f"{base}_{n}.png")):
                    n += 1
                path = os.path.join(cap_dir, f"{base}_{n}.png")
        try:
            with open(path, "wb") as f:
                f.write(png)
        except Exception as e:
            self._toast(f"⚠ 저장 실패: {e}")
            return
        fname = os.path.basename(path)
        self._copy_text(os.path.join("캡처", fname))   # "캡처/이름.png" 클립보드 → 에이전트에 바로 지목
        self._toast(f"저장됨 ✓ {fname}  (이름 복사됨)")

    # ── 작은 모달 다이얼로그 ─────────────────────────────────────────
    def _ask_name(self):
        """파일 이름 입력. 반환: 문자열 또는 None(취소)."""
        dlg = ctk.CTkToplevel(self)
        dlg.title("이름 지정")
        dlg.geometry("320x160")
        dlg.transient(self)
        dlg.lift()
        dlg.grab_set()
        self._dlg_result = None
        ctk.CTkLabel(dlg, text="파일 이름 (.png 자동)").pack(pady=(18, 6), padx=16, anchor="w")
        var = tk.StringVar(value="")
        ent = ctk.CTkEntry(dlg, textvariable=var, placeholder_text=time.strftime("capture_%H%M%S"))
        ent.pack(fill="x", padx=16)
        ent.focus_set()

        def ok():
            self._dlg_result = var.get()
            dlg.destroy()

        def cancel():
            self._dlg_result = None
            dlg.destroy()

        ent.bind("<Return>", lambda e: ok())
        ent.bind("<Escape>", lambda e: cancel())
        row = ctk.CTkFrame(dlg, fg_color="transparent")
        row.pack(pady=14)
        ctk.CTkButton(row, text="저장", width=90, command=ok).pack(side="left", padx=6)
        ctk.CTkButton(row, text="취소", width=90, fg_color="transparent",
                      border_width=1, command=cancel).pack(side="left", padx=6)
        dlg.protocol("WM_DELETE_WINDOW", cancel)
        self.wait_window(dlg)
        return self._dlg_result

    def _ask_dup(self, base):
        """중복 이름 처리 선택. 반환: 'overwrite' / 'number' / None(취소)."""
        dlg = ctk.CTkToplevel(self)
        dlg.title("같은 이름 있음")
        dlg.geometry("340x160")
        dlg.transient(self)
        dlg.lift()
        dlg.grab_set()
        self._dlg_result = None
        ctk.CTkLabel(dlg, text=f"'{base}.png' 이(가) 이미 있습니다.\n어떻게 할까요?",
                     justify="left").pack(pady=(18, 10), padx=16, anchor="w")

        def choose(v):
            self._dlg_result = v
            dlg.destroy()

        row = ctk.CTkFrame(dlg, fg_color="transparent")
        row.pack()
        ctk.CTkButton(row, text="덮어쓰기", width=100, command=lambda: choose("overwrite")).pack(side="left", padx=5)
        ctk.CTkButton(row, text="숫자 붙이기", width=110, command=lambda: choose("number")).pack(side="left", padx=5)
        ctk.CTkButton(dlg, text="취소", width=80, fg_color="transparent", border_width=1,
                      command=lambda: choose(None)).pack(pady=(12, 0))
        dlg.protocol("WM_DELETE_WINDOW", lambda: choose(None))
        self.wait_window(dlg)
        return self._dlg_result

    # ── CDP 콘솔 수집 ────────────────────────────────────────────────
    def _start_cdp(self, port):
        if not HAS_WS:
            return
        self._stop_cdp()
        # 콜백은 백그라운드 스레드에서 호출되므로 메인스레드로 마샬링
        self._cdp = CDPClient(port, lambda t: self.after(0, self._add_console_entry, t))

    def _stop_cdp(self):
        if self._cdp:
            try:
                self._cdp.close()
            except Exception:
                pass
            self._cdp = None

    def _add_console_entry(self, text):
        if not self._running:
            return
        MAXN = 200
        row = ctk.CTkFrame(self._console_body, fg_color=("#f3f3f3", "#2b2b2b"))
        row.pack(fill="x", padx=2, pady=2)
        row.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(row, text=text, anchor="w", justify="left", wraplength=320,
                     font=ctk.CTkFont(family="Consolas", size=11)
                     ).grid(row=0, column=0, sticky="ew", padx=(6, 2), pady=4)
        ctk.CTkButton(row, text="복사", width=44, height=22,
                      command=lambda t=text: self._copy_text(t)
                      ).grid(row=0, column=1, padx=(2, 6), pady=4)
        self._console_entries.append((text, row))
        if len(self._console_entries) > MAXN:
            _t, old = self._console_entries.pop(0)
            try:
                old.destroy()
            except Exception:
                pass
        if not self._console_open:          # 에러 들어오면 자동으로 펼침
            self._console_open = True
            self._console_body.grid()
        self._update_console_header()
        try:
            self._console_body._parent_canvas.yview_moveto(1.0)   # 맨 아래로
        except Exception:
            pass

    def _copy_text(self, text):
        try:
            self.clipboard_clear()
            self.clipboard_append(text)
            self.update_idletasks()
        except Exception:
            pass

    def _copy_all_console(self):
        if not self._console_entries:
            return
        self._copy_text("\n\n".join(t for t, _ in self._console_entries))

    def _clear_console(self):
        for _t, row in self._console_entries:
            try:
                row.destroy()
            except Exception:
                pass
        self._console_entries = []
        self._update_console_header()

    def _toggle_console(self):
        self._console_open = not self._console_open
        if self._console_open:
            self._console_body.grid()
        else:
            self._console_body.grid_remove()
        self._update_console_header()

    def _update_console_header(self):
        arrow = "▾" if self._console_open else "▸"
        suffix = "" if HAS_WS else "  · websocket-client 필요"
        self._console_btn.configure(text=f"콘솔 {arrow}  ({len(self._console_entries)}){suffix}")

    # ── 북마크 ───────────────────────────────────────────────────────
    def _add_bookmark(self):
        url = self.url_var.get().strip()
        if url and url not in self.cfg["bookmarks"]:
            self.cfg["bookmarks"].append(url)
            save_settings(self.cfg)

    def _show_bookmarks(self):
        if not self.cfg["bookmarks"]:
            return
        bm_win = ctk.CTkToplevel(self)
        bm_win.title("북마크")
        bm_win.geometry("420x300")
        bm_win.transient(self)
        bm_win.lift()
        bm_win.grab_set()

        lb = tk.Listbox(bm_win, bg="#2b2b2b", fg="white",
                        selectbackground="#1f6aa5",
                        font=("Consolas", 11), borderwidth=0)
        lb.pack(fill="both", expand=True, padx=8, pady=8)

        for bm in self.cfg["bookmarks"]:
            lb.insert(tk.END, bm)

        btn_row = ctk.CTkFrame(bm_win, fg_color="transparent")
        btn_row.pack(fill="x", padx=8, pady=(0, 8))

        def load_selected():
            sel = lb.curselection()
            if sel:
                self.url_var.set(self.cfg["bookmarks"][sel[0]])
                bm_win.destroy()
                self._go_url()

        def delete_selected():
            sel = lb.curselection()
            if sel:
                self.cfg["bookmarks"].pop(sel[0])
                lb.delete(sel[0])
                save_settings(self.cfg)

        ctk.CTkButton(btn_row, text="이동", width=80, command=load_selected).pack(side="left", padx=4)
        ctk.CTkButton(btn_row, text="삭제", width=80,
                      fg_color="transparent", border_width=1,
                      command=delete_selected).pack(side="left", padx=4)

    # ── Claude 터미널 ─────────────────────────────────────────────────
    def _start_claude(self, confirm=False):
        # 대화 중(터미널이 떠 있음)에 버튼으로 재시작하면 그동안의 대화가 사라진다.
        # → 확인받고, '아니오'면 취소(대화 유지). 안 떠 있으면 경고 없이 바로 시작.
        if confirm and self._term_hwnd and HAS_WIN32 and win32gui.IsWindow(self._term_hwnd):
            if not self._confirm(
                    "재시작 확인",
                    "AI를 재시작하면 그동안의 대화 내용이 사라집니다.\n"
                    "(위험/안전 모드 변경도 새 세션으로 적용됩니다.)\n\n계속할까요?",
                    ok_text="예, 재시작"):
                return
        folder = self.folder_var.get().strip()
        if not folder or not os.path.isdir(folder):
            folder = os.path.expanduser("~")

        self.cfg["folder_history"] = push_history(self.cfg["folder_history"], folder)
        self.folder_combo.configure(values=self.cfg["folder_history"])

        # 에이전트가 페이지를 조작할 수 있도록 프로젝트에 브라우저 MCP 서버 자동 등록
        self._ensure_mcp_config(folder)

        # 작업 연속성: PROGRESS.md 자동 생성 + 에이전트가 그걸 읽고/갱신하도록 지시문 주입
        self._ensure_progress(folder)
        self._ensure_agent_instructions(folder)

        # 선택 엔진 (allowlist 검증 — PowerShell -Command 에 들어가므로 임의값 차단)
        engine = self.engine_var.get().strip()
        if engine not in ENGINES:
            engine = "claude"
        self.cfg["engine"] = engine
        save_settings(self.cfg)
        self._update_status()

        # '모두 허용'(YOLO): 엔진별 권한 우회 플래그 (고정 문자열 — 사용자 입력 아님)
        parts = [engine]
        if self.auto_yes_var.get():
            if engine == "claude":
                parts.append("--dangerously-skip-permissions")
            elif engine == "codex":
                parts.append("--dangerously-bypass-approvals-and-sandbox")
        # 모델 선택: "(기본)"이 아니고 안전한 문자만일 때 --model 부착
        model = self.model_var.get().strip()
        if model and model not in ("(기본)", "+ 편집…") and re.match(r"^[A-Za-z0-9._:-]+$", model):
            parts += ["--model", model]
        engine_cmd = " ".join(parts)

        self._kill(self._term_proc)
        self._term_hwnd = 0
        self._term_mode = None
        self._term_gen += 1
        gen = self._term_gen

        # 엔진 건강검진 → 깨졌으면 자동복구(최대 2회) → 통과하면 터미널 실행. (백그라운드)
        threading.Thread(target=self._ensure_engine_then_launch,
                         args=(engine, engine_cmd, folder, gen), daemon=True).start()

    # ── 엔진 건강검진 / 자동복구 ──────────────────────────────────────
    _ENGINE_PKG = {"claude": "@anthropic-ai/claude-code", "codex": "@openai/codex"}

    def _engine_ok(self, engine):
        try:
            r = subprocess.run(f"{engine} --version", shell=True,
                               capture_output=True, text=True, timeout=25,
                               creationflags=CREATE_NO_WINDOW)
            return r.returncode == 0
        except Exception:
            return False

    def _npm_ok(self):
        try:
            r = subprocess.run("npm --version", shell=True,
                               capture_output=True, text=True, timeout=25,
                               creationflags=CREATE_NO_WINDOW)
            return r.returncode == 0
        except Exception:
            return False

    def _repair_engine(self, pkg, hard=False):
        try:
            if hard:                                   # 2차: 깨끗이 지우고 재설치
                subprocess.run(f"npm uninstall -g {pkg}", shell=True,
                               capture_output=True, text=True, timeout=180,
                               creationflags=CREATE_NO_WINDOW)
            subprocess.run(f"npm install -g {pkg}", shell=True,
                           capture_output=True, text=True, timeout=300,
                           creationflags=CREATE_NO_WINDOW)
        except Exception:
            pass

    def _ensure_engine_then_launch(self, engine, engine_cmd, folder, gen):
        if gen != self._term_gen:
            return
        # 1) 정상이면 바로 실행
        if self._engine_ok(engine):
            self.after(0, lambda: self._spawn_terminal(engine_cmd, folder, gen))
            return
        # 2) npm 자체가 없으면 자동설치 안 함 — 안내만
        if not self._npm_ok():
            self.after(0, self._notify_node_missing)
            return
        pkg = self._ENGINE_PKG.get(engine)
        if not pkg:
            self.after(0, lambda: self._toast(f"{engine} 자동복구 미지원 — 수동 재설치 필요", ms=4000))
            return
        # 3) 자동복구 최대 2회 (1회 → 1분 대기 → 1회 더)
        for attempt in (1, 2):
            if gen != self._term_gen:
                return
            self.after(0, lambda a=attempt: self._toast(
                f"{engine} 손상 감지 — 자동 복구 중 ({a}/2)…", ms=8000))
            self._repair_engine(pkg, hard=(attempt == 2))
            if gen != self._term_gen:
                return
            if self._engine_ok(engine):
                self.after(0, lambda: self._toast(f"{engine} 복구 완료 ✓"))
                self.after(0, lambda: self._spawn_terminal(engine_cmd, folder, gen))
                return
            if attempt == 1:
                self.after(0, lambda: self._toast("1분 후 다시 시도합니다…", ms=8000))
                for _ in range(60):                    # 1분 대기(중간에 새 시작이면 중단)
                    if gen != self._term_gen:
                        return
                    time.sleep(1)
        # 4) 두 번 다 실패 → 멈추고 안내
        self.after(0, lambda: self._notify_repair_failed(engine, pkg))

    def _notify_node_missing(self):
        if self._confirm(
                "Node.js 필요",
                "AI(claude/codex)를 실행하려면 Node.js(npm)가 필요합니다.\n"
                "Node.js 는 시스템 런타임이라 자동 설치하지 않습니다.\n\n"
                "다운로드 페이지를 열까요?",
                ok_text="다운로드 페이지 열기"):
            try:
                import webbrowser
                webbrowser.open("https://nodejs.org")
            except Exception:
                pass

    def _notify_repair_failed(self, engine, pkg):
        self._confirm(
            "자동 복구 실패",
            f"{engine} 를 두 번 자동 복구했지만 실패했습니다.\n"
            "PowerShell 에서 직접 재설치해 주세요:\n\n"
            f"  npm install -g {pkg}\n\n"
            "(myah 를 완전히 종료한 뒤 설치하면 파일 잠금 문제를 피할 수 있습니다.)",
            ok_text="확인")

    def _spawn_terminal(self, engine_cmd, folder, gen):
        if gen != self._term_gen or not self._running:
            return
        title = f"AGENT_{os.getpid()}_{gen}"           # 런치마다 유니크 (FIX D)
        ps_folder = folder.replace("'", "''")          # PowerShell 작은따옴표 이스케이프
        # 사용자 기본 터미널이 Windows Terminal(wt.exe)이면 자체 탭/타이틀바·다른 창 구조라
        # 테두리 제거·리전 클리핑·크기 맞춤이 안 먹는다. → conhost.exe 로 강제해 옛날 콘솔로 띄운다.
        # (conhost 는 한글 IME 도 정상이고 우리 임베드 로직이 가정한 창 구조다.)
        # owner 모드는 클리핑이 안 되므로, 패널보다 넓게 띄우면 오른쪽으로 삐져나온다.
        # → 일부러 좁게(48열) 띄워 절대 안 넘치게 하고, 임베드 직후 fit 이 컨테이너 폭으로 키운다.
        # conhost 는 기본적으로 QuickEdit(마우스 드래그 복사 / 우클릭 붙여넣기)가 꺼져 있다.
        # 시작 시 콘솔 입력모드에 ENABLE_QUICK_EDIT_MODE|ENABLE_EXTENDED_FLAGS 를 켜서 복붙을 살린다.
        qe = (
            "$s='[DllImport(\"kernel32.dll\")]public static extern IntPtr GetStdHandle(int n);"
            "[DllImport(\"kernel32.dll\")]public static extern bool GetConsoleMode(IntPtr h,out uint m);"
            "[DllImport(\"kernel32.dll\")]public static extern bool SetConsoleMode(IntPtr h,uint m);';"
            "try{ $t=Add-Type -MemberDefinition $s -Name CM -Namespace W -PassThru;"
            "$h=$t::GetStdHandle(-10); $m=0; [void]$t::GetConsoleMode($h,[ref]$m);"
            "[void]$t::SetConsoleMode($h, ($m -bor 0x0040 -bor 0x0080)); }catch{}; "
        )
        cmd = (
            f"$Host.UI.RawUI.WindowTitle='{title}'; "
            + qe +
            f"try {{ "
            f"$Host.UI.RawUI.BufferSize = (New-Object System.Management.Automation.Host.Size(48,9000)); "
            f"$Host.UI.RawUI.WindowSize = (New-Object System.Management.Automation.Host.Size(48,30)); }} catch {{}}; "
            f"Set-Location '{ps_folder}'; {engine_cmd}"
        )
        ps_args = ["powershell", "-NoExit", "-Command", cmd]
        try:
            # 사용자 기본 터미널이 Windows Terminal 이어도 conhost 로 강제 → 임베드/IME/클리핑 정상.
            self._term_proc = subprocess.Popen(
                ["conhost.exe"] + ps_args,
                creationflags=CREATE_NEW_CONSOLE,
            )
        except Exception:
            # conhost 직접 실행 실패 시 폴백 (구형/특수 환경)
            self._term_proc = subprocess.Popen(
                ps_args, creationflags=CREATE_NEW_CONSOLE,
            )
        threading.Thread(target=self._find_term, args=(title, gen), daemon=True).start()

    def _restart_claude(self):
        self._start_claude(confirm=True)

    # ── 실행 중인 세션에 키 입력 전송 ────────────────────────────────
    def _ensure_quickedit_default(self):
        """콘솔 기본값에 QuickEdit/InsertMode 를 켜둔다 → 새로 뜨는 conhost 가 항상 복붙 가능.
        (마우스 드래그로 선택→Enter/우클릭=복사, 우클릭=붙여넣기)"""
        try:
            import winreg
            key = winreg.CreateKey(winreg.HKEY_CURRENT_USER, "Console")
            winreg.SetValueEx(key, "QuickEdit", 0, winreg.REG_DWORD, 1)
            winreg.SetValueEx(key, "InsertMode", 0, winreg.REG_DWORD, 1)
            winreg.CloseKey(key)
        except Exception:
            pass

    def _send_term_text(self, text, enter=True):
        """임베드된 콘솔에 포커스를 주고 text 를 타이핑한다(+엔터). 세션 유지.
        유니코드 문자 주입(KEYEVENTF_UNICODE)이라 한글 IME 가 켜져 있어도 영문 그대로 들어간다."""
        hwnd = self._term_hwnd
        if not (hwnd and HAS_WIN32 and win32gui.IsWindow(hwnd)):
            self._toast("⚠ 실행 중인 터미널이 없습니다")
            return
        try:
            win32gui.SetForegroundWindow(hwnd)
        except Exception:
            pass
        time.sleep(0.08)
        KEYEVENTF_UNICODE = 0x0004
        KEYEVENTF_KEYUP = 0x0002
        for ch in text:
            code = ord(ch)
            # bVk=0, bScan=유니코드 코드포인트, flags=UNICODE → IME/자판 무시하고 그 문자 입력
            win32api.keybd_event(0, code, KEYEVENTF_UNICODE, 0)
            win32api.keybd_event(0, code, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP, 0)
            time.sleep(0.006)
        if enter:
            win32api.keybd_event(0x0D, 0, 0, 0)              # Enter (VK_RETURN)
            win32api.keybd_event(0x0D, 0, KEYEVENTF_KEYUP, 0)

    def _send_model_cmd(self):
        """모델변경: 실행 중인 세션에 /model 전송 → claude 화면에서 직접 선택(세션 유지)."""
        if self.engine_var.get().strip() == "codex":
            self._toast("codex 는 터미널에서 직접 모델을 바꾸세요", ms=2500)
            return
        self._send_term_text("/model", enter=True)

    def _send_raw_unicode(self, s, per_char=0.012):
        """문자열을 유니코드 주입으로 그대로 전송(특수문자·줄바꿈 포함, 자동 엔터 없음).
        한글 깨짐 방지를 위해 글자당 텀을 충분히 둔다(너무 빠르면 conhost 가 일부를 놓침)."""
        KEYEVENTF_UNICODE = 0x0004
        KEYEVENTF_KEYUP = 0x0002
        for ch in s:
            code = ord(ch)
            win32api.keybd_event(0, code, KEYEVENTF_UNICODE, 0)
            time.sleep(0.002)
            win32api.keybd_event(0, code, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP, 0)
            time.sleep(per_char)

    def _write_console_input(self, pid, text):
        """대상 콘솔에 attach 해서 문자를 입력 버퍼에 직접 쓴다. 키보드/IME 를 안 거쳐 한글 안전."""
        try:
            import ctypes
            from ctypes import wintypes
        except Exception:
            return False

        class _KEY(ctypes.Structure):
            _fields_ = [("bKeyDown", wintypes.BOOL),
                        ("wRepeatCount", wintypes.WORD),
                        ("wVirtualKeyCode", wintypes.WORD),
                        ("wVirtualScanCode", wintypes.WORD),
                        ("UnicodeChar", wintypes.WCHAR),
                        ("dwControlKeyState", wintypes.DWORD)]

        class _EVT(ctypes.Union):
            _fields_ = [("KeyEvent", _KEY)]

        class _REC(ctypes.Structure):
            _fields_ = [("EventType", wintypes.WORD), ("Event", _EVT)]

        k = ctypes.windll.kernel32
        try:
            k.FreeConsole()                       # 우리 콘솔(있으면) 떼기
        except Exception:
            pass
        if not k.AttachConsole(int(pid)):
            return False
        try:
            h = k.GetStdHandle(-10)               # STD_INPUT_HANDLE
            if not h or h == ctypes.c_void_p(-1).value:
                return False
            n = len(text)
            arr = (_REC * n)()
            for i, ch in enumerate(text):
                arr[i].EventType = 0x0001          # KEY_EVENT
                ke = arr[i].Event.KeyEvent
                ke.bKeyDown = 1
                ke.wRepeatCount = 1
                ke.UnicodeChar = ch
            written = wintypes.DWORD(0)
            ok = k.WriteConsoleInputW(h, arr, n, ctypes.byref(written))
            return bool(ok) and written.value > 0
        except Exception:
            return False
        finally:
            try:
                k.FreeConsole()
            except Exception:
                pass

    def _paste_to_term(self):
        """붙여넣기: 클립보드를 claude 입력창에 주입. WriteConsoleInput(한글 안전) 우선,
        실패 시 키 주입 폴백. bracketed paste 마커로 감싸 여러 줄도 한 덩어리로."""
        hwnd = self._term_hwnd
        if not (hwnd and HAS_WIN32 and win32gui.IsWindow(hwnd)):
            self._toast("⚠ 실행 중인 터미널이 없습니다")
            return
        try:
            text = self.clipboard_get()
        except Exception:
            text = ""
        if not text:
            self._toast("클립보드가 비어 있습니다")
            return
        text = text.replace("\r\n", "\r").replace("\n", "\r")   # 줄바꿈 보존(CR)
        threading.Thread(target=self._do_paste, args=(hwnd, text), daemon=True).start()

    def _do_paste(self, hwnd, text):
        payload = "\x1b[200~" + text + "\x1b[201~"   # bracketed paste
        # 1) WriteConsoleInput 우선 (IME 안 거침 → 한글 안 깨짐)
        pids = []
        try:
            _tid, wp = win32process.GetWindowThreadProcessId(hwnd)
            if wp:
                pids.append(wp)
        except Exception:
            pass
        if self._term_proc:
            try:
                pids.append(self._term_proc.pid)
            except Exception:
                pass
        for pid in pids:
            if self._write_console_input(pid, payload):
                self.after(0, lambda: self._toast("붙여넣기 ✓ (엔터는 직접)"))
                return
        # 2) 폴백: 키 주입 (한글은 깨질 수 있으나 영문은 됨)
        try:
            win32gui.SetForegroundWindow(hwnd)
        except Exception:
            pass
        time.sleep(0.1)
        self._send_raw_unicode(payload, per_char=0.012)
        self.after(0, lambda: self._toast("붙여넣기 ✓ (폴백 방식)"))

    def _on_engine_change(self, _value=None):
        engine = self.engine_var.get().strip()
        if engine in ENGINES:
            self.cfg["engine"] = engine
            save_settings(self.cfg)
            # 엔진 바뀌면 모델 목록·선택도 그 엔진 것으로 교체
            self.model_combo.configure(values=self._model_values(engine))
            self.model_var.set(self.cfg.get("model_sel", {}).get(engine, "(기본)"))
            self._update_status()

    # ── 모델 선택 (엔진별 목록, 추가·수정) ───────────────────────────
    def _model_values(self, engine):
        models = self.cfg.get("models", {}).get(engine, [])
        return ["(기본)"] + list(models) + ["+ 편집…"]

    def _on_model_change(self, value=None):
        engine = self.engine_var.get().strip()
        value = (value or self.model_var.get()).strip()
        if value == "+ 편집…":
            prev = self.cfg.get("model_sel", {}).get(engine, "(기본)")
            self.model_var.set(prev)            # 편집 항목은 선택값으로 두지 않음
            self._edit_models(engine)
            return
        self.cfg.setdefault("model_sel", {})[engine] = value
        save_settings(self.cfg)
        self._update_status()

    def _edit_models(self, engine):
        """엔진별 모델 목록 추가·수정 (한 줄에 하나)."""
        dlg = ctk.CTkToplevel(self)
        dlg.title(f"{engine} 모델 목록")
        dlg.geometry("340x300")
        dlg.transient(self); dlg.lift(); dlg.grab_set()
        ctk.CTkLabel(dlg, text="한 줄에 모델 하나 (별칭 또는 풀네임)\n예: opus / sonnet / claude-opus-4-8",
                     justify="left").pack(pady=(12, 6), padx=14, anchor="w")
        box = ctk.CTkTextbox(dlg, width=300, height=170)
        box.pack(padx=14, pady=4, fill="both", expand=True)
        box.insert("1.0", "\n".join(self.cfg.get("models", {}).get(engine, [])))

        def save():
            lines = [ln.strip() for ln in box.get("1.0", "end").splitlines() if ln.strip()]
            self.cfg.setdefault("models", {})[engine] = lines
            save_settings(self.cfg)
            self.model_combo.configure(values=self._model_values(engine))
            cur = self.model_var.get()
            if cur not in (["(기본)", "+ 편집…"] + lines):
                self.model_var.set("(기본)")
            dlg.destroy()
            self._toast(f"{engine} 모델 목록 저장됨")

        ctk.CTkButton(dlg, text="저장", command=save).pack(pady=(4, 12))
        dlg.protocol("WM_DELETE_WINDOW", dlg.destroy)

    def _ensure_mcp_config(self, folder):
        """프로젝트 폴더의 .mcp.json 에 우리 브라우저 MCP 서버(bc-browser)를 생성/병합.
        Claude Code 가 프로젝트 스코프 .mcp.json 을 자동 로드한다(첫 실행 시 승인 프롬프트가 뜰 수 있음).
        기존 .mcp.json 의 다른 서버 설정은 보존(병합)한다."""
        if not os.path.isdir(folder) or not os.path.exists(MCP_SERVER):
            return
        path = os.path.join(folder, ".mcp.json")
        data = {}
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception:
                data = {}
        if not isinstance(data, dict):
            data = {}
        servers = data.get("mcpServers")
        if not isinstance(servers, dict):
            servers = {}
        # 절대경로의 venv 파이썬 + 절대경로 스크립트 (윈도우 PATH 함정 회피)
        servers["myah-browser"] = {"type": "stdio", "command": PYTHON, "args": [MCP_SERVER]}
        data["mcpServers"] = servers
        try:
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, path)
        except Exception:
            pass

    # ── 작업 연속성: .myah/PROGRESS.md + 에이전트 지시문 ─────────────────
    _PROGRESS_TEMPLATE = """# 작업 인수인계 (myah)

이 파일은 세션·모델이 바뀌어도 작업을 이어가기 위한 단일 인수인계 문서입니다.
새 세션은 이 파일을 읽고 "마지막 상태"부터 이어서 진행하세요.

## 프로젝트
(한 줄 설명 + 스택. 예: React + Vite 프론트엔드 / FastAPI 백엔드)

## 규칙 (항상 지킬 것)
- (예: TypeScript 사용, Tailwind 사용)
- (예: 금지 라이브러리, 코드 스타일 등 결정사항)

## 한 일 (Done)
- [ ] (완료한 작업을 여기에 누적)

## 할 일 (Next)
- [ ] (다음에 할 작업)

## 마지막 상태 (여기부터 이어서 시작)
(아직 시작 전 — 첫 작업을 진행한 뒤 에이전트가 이 칸을 갱신합니다.)
"""

    _AGENT_DIRECTIVE = (
        "<!-- myah:progress -->\n"
        "## 작업 연속성 (myah) — 필수 규칙\n"
        "다음은 선택이 아니라 **반드시 지켜야 하는 규칙**이다.\n"
        "\n"
        "1. **세션을 시작하면 가장 먼저** `.myah/PROGRESS.md` 를 읽고, \"마지막 상태\"부터 이어서 작업한다.\n"
        "2. **작업 응답을 끝내기 전에 반드시** `.myah/PROGRESS.md` 를 갱신한다. 이는 필수이며 생략할 수 없다:\n"
        "   - \"한 일 (Done)\" : 이번에 완료한 것을 추가\n"
        "   - \"할 일 (Next)\" : 남은/새로 생긴 작업을 갱신\n"
        "   - \"마지막 상태\" : 다음 세션이 곧바로 이어갈 수 있도록 현재 지점을 구체적으로 적는다\n"
        "3. PROGRESS.md 의 \"규칙\" 항목을 항상 지킨다.\n"
        "\n"
        "### UI 작업 방식\n"
        "이 프로젝트는 myah 왼쪽 창에 실행된 채로 미리보기된다. 화면의 버튼·입력·체크박스 등 동작을\n"
        "바꾸거나 추가하려 할 때, **실행 중인 창을 직접 클릭/입력하려 하지 말고 그 동작을 만드는 소스 코드를\n"
        "직접 수정**한다(예: 버튼의 핸들러 함수, 입력 기본값, 레이아웃 코드). 코드를 고친 뒤 다시 실행하면\n"
        "(myah 의 ▶앱/⟳) 그 변경이 그대로 반영된다. 즉 런타임 조작이 아니라 코드 수정이 작업의 기본 방식이다.\n"
        "\n"
        "PROGRESS.md 갱신을 빠뜨린 경우, 작업이 끝난 것으로 간주하지 않는다. 매 작업 종료 시 갱신했는지 스스로 점검하라.\n"
        "<!-- /myah:progress -->\n"
    )

    def _ensure_progress(self, folder):
        """폴더에 .myah/PROGRESS.md 가 없으면 템플릿으로 생성(있으면 보존)."""
        if not os.path.isdir(folder):
            return
        try:
            myah_dir = os.path.join(folder, ".myah")
            os.makedirs(myah_dir, exist_ok=True)
            path = os.path.join(myah_dir, "PROGRESS.md")
            if not os.path.exists(path):
                with open(path, "w", encoding="utf-8") as f:
                    f.write(self._PROGRESS_TEMPLATE)
        except Exception:
            pass

    def _ensure_agent_instructions(self, folder):
        """에이전트가 자동으로 읽는 표준 파일(AGENTS.md=Codex, CLAUDE.md=Claude Code)에
        PROGRESS.md 를 읽고/갱신하라는 지시문 블록을 주입한다.
        마커 블록이 이미 있으면 최신 지시문으로 교체(업그레이드 반영), 없으면 추가. 그 외 내용은 보존."""
        if not os.path.isdir(folder):
            return
        start, end = "<!-- myah:progress -->", "<!-- /myah:progress -->"
        for name in ("AGENTS.md", "CLAUDE.md"):
            try:
                path = os.path.join(folder, name)
                existing = ""
                if os.path.exists(path):
                    with open(path, "r", encoding="utf-8") as f:
                        existing = f.read()
                if start in existing and end in existing:
                    # 기존 블록을 최신 지시문으로 교체
                    pre = existing.split(start, 1)[0]
                    post = existing.split(end, 1)[1]
                    new_content = pre + self._AGENT_DIRECTIVE.rstrip("\n") + post
                else:
                    sep = "" if existing.endswith("\n") or existing == "" else "\n"
                    block = (sep + "\n" + self._AGENT_DIRECTIVE) if existing else self._AGENT_DIRECTIVE
                    new_content = existing + block
                if new_content != existing:
                    tmp = path + ".tmp"
                    with open(tmp, "w", encoding="utf-8") as f:
                        f.write(new_content)
                    os.replace(tmp, path)
            except Exception:
                pass

    def _find_term(self, title, gen):
        for _ in range(40):                # 최대 12초
            time.sleep(0.3)
            if not HAS_WIN32 or not self._running or gen != self._term_gen:
                return
            hwnd = win32gui.FindWindow(None, title)
            if hwnd:
                time.sleep(0.5)            # 창이 완전히 자리잡을 때까지 잠깐 대기(불안정 완화)
                if not self._running or gen != self._term_gen:
                    return
                self.after(0, lambda h=hwnd, g=gen: self._embed_term(h, g))
                return

    def _embed_term(self, hwnd, gen):
        if not self._running or gen != self._term_gen or not win32gui.IsWindow(hwnd):
            return
        # 터미널은 owner 모드(top-level 유지 + 테두리만 제거)로 박는다.
        # conhost 의 한글 IME 는 자식창(WS_CHILD)에서 안 붙고 top-level 에서만 정상 동작하기 때문.
        # 위치는 _sync_tick(30Hz)가 컨테이너에 맞춰 따라붙임. 클릭 시 포커스도 자연히 잡힘.
        make_borderless(hwnd)
        set_owner(hwnd, self.winfo_id())
        self._term_hwnd, self._term_mode = hwnd, "owner"
        self._sync(hwnd, self._term_container)
        w = self._term_container.winfo_width()
        h = self._term_container.winfo_height()
        if w > 10 and h > 10:
            self._run_term_fit(w, h)               # 즉시 컨테이너 폭으로 키움(디바운스 X)
            # claude TUI 가 완전히 뜬 뒤 한 번 더 맞춤 → 입력줄 위치/크기 안정화
            self.after(1500, lambda g=gen: self._refit_term(g))

    def _refit_term(self, gen):
        if gen != self._term_gen:
            return
        if not (self._term_hwnd and HAS_WIN32 and win32gui.IsWindow(self._term_hwnd)):
            return
        w = self._term_container.winfo_width()
        h = self._term_container.winfo_height()
        if w > 10 and h > 10:
            self._sync(self._term_hwnd, self._term_container)
            self._run_term_fit(w, h)

    def _browse_folder(self):
        folder = filedialog.askdirectory(initialdir=self.folder_var.get() or os.path.expanduser("~"))
        if folder:
            self.folder_var.set(folder)
            self._auto_preview(folder)

    def _on_folder_pick(self, _value=None):
        self._auto_preview(self.folder_var.get().strip())

    # ── 테마 ─────────────────────────────────────────────────────────
    def _theme_label(self):
        return "☀ 라이트" if self.cfg["theme"] == "dark" else "🌙 다크"

    def _toggle_theme(self):
        self.cfg["theme"] = "light" if self.cfg["theme"] == "dark" else "dark"
        ctk.set_appearance_mode(self.cfg["theme"])
        self._theme_btn.configure(text=self._theme_label())

    # ── 종료 ─────────────────────────────────────────────────────────
    def _on_close(self):
        self._running = False
        try:
            w = self.winfo_width() - 16
            sash_x = self._paned.sash_coord(0)[0]
            self.cfg["sash_ratio"] = round(sash_x / w, 3)
        except Exception:
            pass
        save_settings(self.cfg)
        self._stop_cdp()
        # 임베드된 창(크롬/터미널/앱)을 확실히 종료한다.
        # 크롬은 멀티프로세스라 우리가 실행한 _web_proc 는 곧 종료되고 실제 창은 다른 chrome.exe 가
        # 소유한다. → 창 HWND 의 '실제 소유 프로세스' 트리를 직접 kill 해야 남지 않는다.
        for hwnd in (self._web_hwnd, self._term_hwnd):
            if hwnd and HAS_WIN32 and win32gui.IsWindow(hwnd):
                try:
                    win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
                except Exception:
                    pass
                try:
                    _tid, pid = win32process.GetWindowThreadProcessId(hwnd)
                    if pid:
                        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                                       creationflags=CREATE_NO_WINDOW,
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                except Exception:
                    pass
        for p in (self._web_proc, self._term_proc):
            self._kill(p)
        self.destroy()

    @staticmethod
    def _kill(proc):
        """프로세스 트리 종료. powershell→claude 같은 자식까지 정리."""
        if proc and proc.poll() is None:
            try:
                if sys.platform == "win32":
                    subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                                   creationflags=CREATE_NO_WINDOW,
                                   stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL)
                else:
                    proc.terminate()
            except Exception:
                try:
                    proc.terminate()
                except Exception:
                    pass


if __name__ == "__main__":
    app = App()
    app.mainloop()
