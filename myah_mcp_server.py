"""
myah_mcp_server.py — Browser+Claude 의 브라우저 조작 MCP 서버 (stdio)

Claude Code 가 자식 프로세스로 띄워서 호출한다. 우리 앱(myah.py)이
왼쪽 패널에 띄운 진짜 크롬에 CDP(Chrome DevTools Protocol)로 붙어
 · browser_snapshot : 페이지 보기 (제목/URL + 상호작용 요소들 + ref)
 · browser_type     : ref 로 지목한 입력 요소에 텍스트 입력 (React 호환)
 · browser_click    : ref 로 지목한 요소 클릭
 · browser_screenshot: 현재 페이지를 이미지로 캡처해 에이전트에 직접 반환
                       (기본 전체 페이지, 필요시 ref/좌표로 구간 지정)
세 가지를 수행한다.

필요: pip install mcp websocket-client
연결: 앱이 크롬을 띄울 때 기록한 포트 파일(임시폴더/myah-cdp.json)에서
      디버깅 포트를 매 호출마다 읽어 접속 → 크롬을 ⟳로 재시작해도 자동 추종.
배치: 이 파일을 myah.py 와 같은 폴더에 둔다. 앱이 프로젝트의
      .mcp.json 에 이 파일 경로로 자동 등록한다.
"""

import json, os, tempfile, urllib.request, base64
import websocket                          # pip install websocket-client
from mcp.server.fastmcp import FastMCP, Image   # pip install mcp

PORT_FILE = os.path.join(tempfile.gettempdir(), "myah-cdp.json")
mcp = FastMCP("myah-browser")


# ── CDP 헬퍼 ──────────────────────────────────────────────────────────
def _port() -> int:
    try:
        with open(PORT_FILE, "r", encoding="utf-8") as f:
            return int(json.load(f).get("port") or 0)
    except Exception:
        return 0


def _page_ws(port: int):
    """활성 page 타깃의 WebSocket 디버거 URL. (devtools 페이지는 제외)"""
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/json", timeout=2) as r:
        targets = json.loads(r.read().decode("utf-8"))
    pages = [t for t in targets
             if t.get("type") == "page"
             and not str(t.get("url", "")).startswith("devtools://")
             and t.get("webSocketDebuggerUrl")]
    return pages[0]["webSocketDebuggerUrl"] if pages else None


def _evaluate(expression: str):
    """활성 페이지에서 JS 를 실행하고 반환값을 돌려준다."""
    port = _port()
    if not port:
        raise RuntimeError("크롬 디버깅 포트를 찾지 못했습니다. 앱(왼쪽)에서 페이지를 먼저 띄우세요.")
    ws_url = _page_ws(port)
    if not ws_url:
        raise RuntimeError("활성 페이지(page 타깃)를 찾지 못했습니다.")
    ws = websocket.create_connection(ws_url, suppress_origin=True, timeout=15)
    try:
        ws.send(json.dumps({
            "id": 1, "method": "Runtime.evaluate",
            "params": {"expression": expression, "returnByValue": True, "awaitPromise": True},
        }))
        while True:
            m = json.loads(ws.recv())
            if m.get("id") == 1:
                if "error" in m:
                    raise RuntimeError(m["error"].get("message", "CDP error"))
                res = m.get("result", {})
                if res.get("exceptionDetails"):
                    raise RuntimeError("페이지 JS 예외: " + json.dumps(res["exceptionDetails"])[:300])
                return res.get("result", {}).get("value")
    finally:
        try:
            ws.close()
        except Exception:
            pass


def _cdp_call(method: str, params=None, timeout: int = 20):
    """활성 페이지에 임의의 CDP 명령을 보내고 result(dict)를 받는다."""
    port = _port()
    if not port:
        raise RuntimeError("크롬 디버깅 포트를 찾지 못했습니다. 앱(왼쪽)에서 페이지를 먼저 띄우세요.")
    ws_url = _page_ws(port)
    if not ws_url:
        raise RuntimeError("활성 페이지(page 타깃)를 찾지 못했습니다.")
    ws = websocket.create_connection(ws_url, suppress_origin=True, timeout=timeout)
    try:
        ws.send(json.dumps({"id": 1, "method": method, "params": params or {}}))
        while True:
            m = json.loads(ws.recv())
            if m.get("id") == 1:
                if "error" in m:
                    raise RuntimeError(m["error"].get("message", "CDP error"))
                return m.get("result", {})
    finally:
        try:
            ws.close()
        except Exception:
            pass


def _ref_selector(ref: str) -> str:
    """ref → 안전한 CSS 선택자 (영숫자만 허용해 주입 방지)."""
    safe = "".join(c for c in str(ref) if c.isalnum())
    return json.dumps(f'[data-bcref="{safe}"]')


# ── 툴들 ──────────────────────────────────────────────────────────────
SNAPSHOT_JS = r"""(function(){
  var sel='input,textarea,select,button,a[href],[role=button],[contenteditable=""],[contenteditable=true]';
  var out=[], n=0, list=document.querySelectorAll(sel);
  for(var i=0;i<list.length;i++){
    var el=list[i], r=el.getBoundingClientRect();
    if(r.width===0&&r.height===0) continue;
    var cs=getComputedStyle(el);
    if(cs.visibility==='hidden'||cs.display==='none') continue;
    n++; var ref='r'+n; el.setAttribute('data-bcref',ref);
    var label=(el.getAttribute('aria-label')||el.getAttribute('placeholder')||
               el.getAttribute('name')||(el.innerText||'').trim().slice(0,80)||'').trim();
    out.push({ref:ref,tag:el.tagName.toLowerCase(),type:(el.getAttribute('type')||''),
              label:label,value:(el.value!==undefined?String(el.value).slice(0,80):'')});
  }
  return JSON.stringify({title:document.title,url:location.href,count:out.length,elements:out});
})()"""


@mcp.tool()
def browser_snapshot() -> str:
    """현재 왼쪽 크롬 페이지의 제목·URL 과 상호작용 가능한 요소들(입력칸/버튼/링크 등)을
    각 요소의 ref 와 함께 JSON 으로 반환한다. browser_type / browser_click 은 이 ref 로 요소를 지목한다.
    페이지를 조작하기 전에 먼저 이 툴로 페이지를 파악할 것."""
    try:
        return _evaluate(SNAPSHOT_JS) or "{}"
    except Exception as e:
        return f"ERROR: {e}"


@mcp.tool()
def browser_type(ref: str, text: str) -> str:
    """browser_snapshot 에서 얻은 ref 로 지목한 입력 요소(input/textarea/contenteditable)에 text 를 입력한다.
    React/Vue 호환: 네이티브 value setter 로 값을 넣고 input·change 이벤트를 디스패치한다."""
    sel = _ref_selector(ref)
    val = json.dumps(text)
    js = f"""(function(){{
  var el=document.querySelector({sel});
  if(!el) return JSON.stringify({{ok:false,err:'ref not found'}});
  el.focus();
  var val={val};
  if(el.isContentEditable){{
    el.textContent=val;
    el.dispatchEvent(new InputEvent('input',{{bubbles:true}}));
  }} else {{
    var proto=el.tagName==='TEXTAREA'?HTMLTextAreaElement.prototype:HTMLInputElement.prototype;
    var d=Object.getOwnPropertyDescriptor(proto,'value');
    if(d&&d.set) d.set.call(el,val); else el.value=val;
    el.dispatchEvent(new Event('input',{{bubbles:true}}));
    el.dispatchEvent(new Event('change',{{bubbles:true}}));
  }}
  return JSON.stringify({{ok:true,tag:el.tagName.toLowerCase(),value:String(el.value||val).slice(0,80)}});
}})()"""
    try:
        return _evaluate(js) or "{}"
    except Exception as e:
        return f"ERROR: {e}"


@mcp.tool()
def browser_click(ref: str) -> str:
    """browser_snapshot 에서 얻은 ref 로 지목한 요소(버튼/링크 등)를 클릭한다."""
    sel = _ref_selector(ref)
    js = f"""(function(){{
  var el=document.querySelector({sel});
  if(!el) return JSON.stringify({{ok:false,err:'ref not found'}});
  el.scrollIntoView({{block:'center'}});
  el.click();
  return JSON.stringify({{ok:true,tag:el.tagName.toLowerCase()}});
}})()"""
    try:
        return _evaluate(js) or "{}"
    except Exception as e:
        return f"ERROR: {e}"


@mcp.tool()
def browser_screenshot(full: bool = True, ref: str = "",
                       x: float = -1, y: float = -1,
                       width: float = -1, height: float = -1) -> Image:
    """현재 왼쪽 크롬 페이지를 캡처해 이미지로 반환한다(에이전트가 화면을 직접 봄).
    기본은 스크롤 포함 전체 페이지(full=True).
    특정 구간만 필요하면 둘 중 하나로 지정(지정 시 full 은 무시):
      · ref    : browser_snapshot 에서 얻은 요소 ref → 그 요소 영역만 캡처
      · x,y,width,height : 페이지 좌표(px)로 직접 구간 지정
    """
    clip = None
    if ref:
        sel = _ref_selector(ref)
        js = f"""(function(){{
  var el=document.querySelector({sel});
  if(!el) return 'null';
  var r=el.getBoundingClientRect();
  return JSON.stringify({{x:r.left+window.scrollX,y:r.top+window.scrollY,width:r.width,height:r.height}});
}})()"""
        s = _evaluate(js)
        if not s or s == "null":
            raise RuntimeError(f"ref '{ref}' 요소를 찾지 못했습니다. browser_snapshot 으로 ref 를 다시 확인하세요.")
        r = json.loads(s)
        clip = {"x": r["x"], "y": r["y"], "width": r["width"], "height": r["height"], "scale": 1}
    elif x >= 0 and y >= 0 and width > 0 and height > 0:
        clip = {"x": x, "y": y, "width": width, "height": height, "scale": 1}

    params = {"format": "png"}
    if clip:
        params["clip"] = clip
    elif full:
        params["captureBeyondViewport"] = True
    res = _cdp_call("Page.captureScreenshot", params)
    data = res.get("data")
    if not data:
        raise RuntimeError("캡처 실패 (페이지가 떠 있는지 확인하세요)")
    return Image(data=base64.b64decode(data), format="png")


if __name__ == "__main__":
    mcp.run()          # 기본 stdio transport
