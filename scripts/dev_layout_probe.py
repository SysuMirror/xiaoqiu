"""CDP 布局探针：无头 Edge 打开小秋页，取计算样式/几何 + 截图。

用法: .venv/Scripts/python scripts/dev_layout_probe.py <rt> <width> <height> <out.json>
前置: 服务器 vite dev(5174) 经隧道到本地 5274；dev-auth.html 桥页存在。
"""
import asyncio
import base64
import json
import subprocess
import sys
import urllib.request

import websockets

sys.stdout.reconfigure(encoding="utf-8")

EDGE_PORT = 9333


async def main():
    rt, width, height, out = sys.argv[1], int(sys.argv[2]), int(sys.argv[3]), sys.argv[4]
    proc = subprocess.Popen([
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        "--headless=new", "--disable-gpu", "--hide-scrollbars",
        f"--remote-debugging-port={EDGE_PORT}",
        f"--user-data-dir=C:/Users/ASUS/AppData/Local/Temp/xq-probe-{width}",
        f"--window-size={width},{height}",
        "about:blank",
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        ws_url = None
        for _ in range(50):
            await asyncio.sleep(0.3)
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{EDGE_PORT}/json/list") as r:
                    targets = json.loads(r.read())
                pages = [t for t in targets if t.get("type") == "page"]
                if pages:
                    ws_url = pages[0]["webSocketDebuggerUrl"]
                    break
            except Exception:
                continue
        assert ws_url, "edge devtools not up"

        events: list[dict] = []
        responses: dict[int, dict] = {}
        next_id = 0

        async with websockets.connect(ws_url, max_size=50 * 1024 * 1024) as ws:
            async def reader():
                while True:
                    try:
                        m = json.loads(await ws.recv())
                    except Exception:
                        return
                    (responses if "id" in m else events).__or__(  # noqa - placeholder
                        {}) if False else None
                    if "id" in m:
                        responses[m["id"]] = m
                    else:
                        events.append(m)

            reader_task = asyncio.create_task(reader())

            async def cmd(method, params=None):
                nonlocal next_id
                next_id += 1
                mid = next_id
                await ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
                while mid not in responses:
                    await asyncio.sleep(0.03)
                return responses.pop(mid)

            await cmd("Page.enable")
            await cmd("Runtime.enable")
            await cmd("Emulation.setDeviceMetricsOverride",
                      {"width": width, "height": height, "deviceScaleFactor": 2,
                       "mobile": width < 500})
            await cmd("Page.navigate",
                      {"url": f"http://localhost:5274/new/dev-auth.html?rt={rt}"})

            last = ""
            for i in range(40):
                await asyncio.sleep(2)
                res = await cmd("Runtime.evaluate", {
                    "expression": ("JSON.stringify({p: location.pathname,"
                                   " tb: !!document.querySelector('.top-bar')})"),
                    "returnByValue": True})
                last = res.get("result", {}).get("result", {}).get("value", "")
                if i % 5 == 4 or '"tb":true' in last:
                    print(f"[{i}] {last}")
                if '"tb":true' in last:
                    await asyncio.sleep(2)
                    break
                if '"p":"/"' in last or '"p":"/new/"' in last:
                    await cmd("Runtime.evaluate",
                              {"expression": "location.href='/new/smart-xiaoqiu'"})

            if events:
                errs = [json.dumps(e.get("params", {}), ensure_ascii=False)[:300]
                        for e in events
                        if e.get("method") in ("Runtime.exceptionThrown", "Log.entryAdded")
                        or (e.get("method") == "Runtime.consoleAPICalled"
                            and e["params"].get("type") == "error")]
                if errs:
                    print("CONSOLE-ERRORS:", json.dumps(errs[-10:], ensure_ascii=False))

            probe = r"""
(() => {
  const out = {viewport: {w: innerWidth, h: innerHeight, dpr: devicePixelRatio}};
  out.docScrollW = document.documentElement.scrollWidth;
  const q = (sel) => document.querySelector(sel);
  const info = (el) => {
    if (!el) return null;
    const r = el.getBoundingClientRect();
    const c = getComputedStyle(el);
    return {rect: {x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height)},
            display: c.display, color: c.color, maxW: c.maxWidth};
  };
  out.smartPage = info(q('.smart-page'));
  out.chatColumn = info(q('.chat-column'));
  out.topBar = info(q('.top-bar'));
  out.iconMenu = info(q('.icon-menu'));
  out.iconMenuSvg = (() => { const s = q('.icon-menu svg'); if (!s) return null;
    const c = getComputedStyle(s); const r = s.getBoundingClientRect();
    return {rect: {w: Math.round(r.width), h: Math.round(r.height)}, w: c.width, h: c.height}; })();
  out.topActions = info(q('.top-actions'));
  out.memBtn = info(q('.top-actions .ghost-btn'));
  out.chatScroll = info(q('.chat-scroll'));
  out.chatInner = info(q('.chat-inner'));
  out.userBubble = info(q('.user-bubble'));
  out.assistantBubble = info(q('.assistant-bubble'));
  out.sugRow = info(q('.sug-row'));
  out.composer = info(q('.composer'));
  out.sideDesktop = info(q('.side-desktop'));
  out.bottomNav = info(q('.bottom-nav, [class*="bottom-navbar"], [class*="BottomNav"]'));
  out.overflowing = [...document.querySelectorAll('.smart-page *')]
    .filter(e => e.getBoundingClientRect().right > innerWidth + 1)
    .slice(0, 8)
    .map(e => (String(e.className) || e.tagName).slice(0, 50));
  return out;
})()
"""
            res = await cmd("Runtime.evaluate", {"expression": probe, "returnByValue": True})
            data = res.get("result", {}).get("value", {})
            shot = await cmd("Page.captureScreenshot", {"format": "png"})
            b64 = shot.get("result", {}).get("data", "")
            with open(out.replace(".json", ".png"), "wb") as f:
                f.write(base64.b64decode(b64))
            with open(out, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=1)
            print(json.dumps(data, ensure_ascii=False, indent=1)[:2600])
            reader_task.cancel()
    finally:
        proc.kill()


if __name__ == "__main__":
    asyncio.run(main())
