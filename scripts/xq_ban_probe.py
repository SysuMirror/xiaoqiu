"""封禁悬浮窗 UI 探针：无头 Edge 驱动 __ban_harness.html，stub 封禁状态接口。

验证：干净用户不显示、封禁出现淡悬浮窗（倒计时/原因/pointer-events/层级）、
长时格式 H:MM:SS、倒计时走字、归零自动消失 + toast。零真实副作用。
用法: .venv/Scripts/python scripts/xq_ban_probe.py <out.json>
"""
import asyncio
import base64
import json
import subprocess
import sys
import urllib.request

import websockets

sys.stdout.reconfigure(encoding="utf-8")

EDGE_PORT = 9334
BASE = "http://localhost:5274/new/__ban_harness.html"


async def main():
    out = sys.argv[1] if len(sys.argv) > 1 else "scripts/xq_ban_probe.json"
    proc = subprocess.Popen([
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        "--headless=new", "--disable-gpu", "--hide-scrollbars",
        "--force-device-scale-factor=1",
        f"--remote-debugging-port={EDGE_PORT}",
        "--user-data-dir=C:/Users/ASUS/AppData/Local/Temp/xq-ban-probe",
        "--window-size=1200,800",
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
                      {"width": 1200, "height": 800, "deviceScaleFactor": 1, "mobile": False})
            await cmd("Page.navigate", {"url": BASE})

            async def ev(expression):
                res = await cmd("Runtime.evaluate",
                                {"expression": expression, "returnByValue": True})
                return res.get("result", {}).get("result", {}).get("value")

            for i in range(30):
                await asyncio.sleep(1)
                state = await ev("JSON.stringify({ready: !!window.__ready,"
                                 " err: window.__err || ''})")
                print(f"[{i}] {state}")
                if state and '"ready":true' in state:
                    break
            err = await ev("window.__err || ''")
            if err:
                print("HARNESS-ERROR:", err[:1500])
                return

            async def shot(name):
                s = await cmd("Page.captureScreenshot", {"format": "png"})
                with open(out.replace(".json", f"-{name}.png"), "wb") as f:
                    f.write(base64.b64decode(s.get("result", {}).get("data", "")))

            async def refresh_ban():
                await ev("window.dispatchEvent(new CustomEvent('xq:ban-refresh'))")

            result = {}
            await asyncio.sleep(1.0)

            # 1. 干净用户：无悬浮窗
            result["clean"] = await ev(
                "JSON.stringify({hidden: !document.querySelector('.ban-pill')})")

            # 2. 封禁出现：短倒计时 + 原因
            await ev("window.__setBan(true, 90, '猜拳输给了小秋')")
            await refresh_ban()
            await asyncio.sleep(0.8)
            result["bannedShort"] = await ev(r"""
(() => {
  const p = document.querySelector('.ban-pill');
  if (!p) return JSON.stringify({shown: false});
  const c = getComputedStyle(p);
  const t = p.textContent;
  return JSON.stringify({
    shown: true, teleported: p.parentElement === document.body,
    textOk: t.includes('你已被封禁') && t.includes('后解封') && t.includes('期间无法重新登录'),
    reasonOk: t.includes('猜拳输给了小秋'),
    fmt: /1:2\d|1:30/.test(t),
    pointerEvents: c.pointerEvents, zIndex: c.zIndex,
  });
})()""")
            await shot("banned")
            await asyncio.sleep(2.2)
            result["ticking"] = await ev(r"""
(() => {
  const t = document.querySelector('.ban-pill')?.textContent || '';
  return JSON.stringify({decreasing: /1:2[0-8]/.test(t) || /1:1\d/.test(t), text: t.trim().slice(0, 60)});
})()""")

            # 3. 长封禁 H:MM:SS 格式
            await ev("window.__setBan(true, 40700, '多次辱骂')")
            await refresh_ban()
            await asyncio.sleep(0.8)
            result["longFmt"] = await ev(r"""
(() => {
  const t = document.querySelector('.ban-pill')?.textContent || '';
  return JSON.stringify({hms: /\d+:[0-5]\d:[0-5]\d/.test(t), reasonOk: t.includes('多次辱骂')});
})()""")

            # 4. 归零自动消失 + toast（服务端先说还在封禁，倒计时走完前切到已解封）
            await ev("window.__setBan(true, 3, '')")
            await refresh_ban()
            await asyncio.sleep(1.0)
            await ev("window.__setBan(false, 0, '')")
            await asyncio.sleep(4.0)
            result["released"] = await ev(r"""
(() => {
  const toast = [...document.querySelectorAll('*')]
    .find(e => e.children.length === 0 && /封禁已解除/.test(e.textContent || ''));
  return JSON.stringify({pillGone: !document.querySelector('.ban-pill'),
                         toastShown: !!toast});
})()""")
            await shot("released")

            errs = [json.dumps(e.get("params", {}), ensure_ascii=False)[:200]
                    for e in events
                    if e.get("method") == "Runtime.exceptionThrown"
                    or (e.get("method") == "Runtime.consoleAPICalled"
                        and e["params"].get("type") == "error")]
            result["consoleErrors"] = errs[-5:]

            with open(out, "w", encoding="utf-8") as f:
                json.dump(result, f, ensure_ascii=False, indent=1)
            print(json.dumps(result, ensure_ascii=False, indent=1))
            reader_task.cancel()
    finally:
        proc.kill()


if __name__ == "__main__":
    asyncio.run(main())
