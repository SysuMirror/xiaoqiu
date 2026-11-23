"""猜拳 UI 探针（harness 版）：无头 Edge 驱动 vite dev 的 __rps_harness.html，
stub 网络层，全流程验证 赢/平/输封号/刑满释放 + ESC 关闭，零真实副作用。

用法: .venv/Scripts/python scripts/xq_rps_probe.py <out.json>
前置: 服务器 vite dev(5174) 经隧道到本地 5274；harness 已 scp 到项目根。
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
BASE = "http://localhost:5274/new/__rps_harness4.html"


async def main():
    out = sys.argv[1] if len(sys.argv) > 1 else "scripts/xq_rps_probe.json"
    proc = subprocess.Popen([
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        "--headless=new", "--disable-gpu", "--hide-scrollbars",
        "--force-device-scale-factor=1",
        f"--remote-debugging-port={EDGE_PORT}",
        "--user-data-dir=C:/Users/ASUS/AppData/Local/Temp/xq-rps-harness",
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

            # 等 harness 就绪
            for i in range(30):
                await asyncio.sleep(1)
                state = await ev("JSON.stringify({ready: !!window.__ready,"
                                 " err: window.__err || ''})")
                print(f"[{i}] {state}")
                if state and ('"ready":true' in state or (state.get("err") if isinstance(state, dict) else "")):
                    break
            err = await ev("window.__err || ''")
            if err:
                print("HARNESS-ERROR:", err[:1500])
                return

            async def shot(name):
                s = await cmd("Page.captureScreenshot", {"format": "png"})
                with open(out.replace(".json", f"-{name}.png"), "wb") as f:
                    f.write(base64.b64decode(s.get("result", {}).get("data", "")))

            result = {}

            # 1. 弹窗结构与 Teleport
            await asyncio.sleep(0.6)
            result["modal"] = await ev(r"""
(() => {
  const mask = document.querySelector('.rps-mask');
  if (!mask) return JSON.stringify({open: false});
  const moves = [...mask.querySelectorAll('.move-btn')];
  return JSON.stringify({
    open: true, teleported: mask.parentElement === document.body,
    moveCount: moves.length,
    moveLabels: moves.map(m => m.getAttribute('aria-label')),
    subtitle: mask.querySelector('.rps-head p')?.textContent.trim() || '',
    ruleOk: (mask.querySelector('.rps-rule')?.textContent || '').includes('封号 5 分钟'),
    statsOk: (mask.querySelector('.rps-stats')?.textContent || '').includes('1234'),
    focusOnMove: (document.activeElement?.className || '').includes('move-btn'),
  });
})()""")
            await shot("modal")

            # 2. win
            await ev("document.querySelectorAll('.move-btn')[0].click()")
            await asyncio.sleep(2.0)
            result["win"] = await ev(r"""
(() => {
  const t = document.querySelector('.rps-mask')?.textContent || '';
  return JSON.stringify({
    verdict: !!document.querySelector('.rps-verdict.win'),
    hasRewardLine: t.includes('奖励：没有'),
    revealOk: t.includes('石头') && t.includes('剪刀'),
    replayEnabled: (() => { const b = [...document.querySelectorAll('.rps-btn')]
      .find(x => x.textContent.includes('再来一局')); return !!b && !b.disabled; })(),
  });
})()""")
            await shot("win")

            # 3. draw
            await ev("[...document.querySelectorAll('.rps-btn')].find(b => b.textContent.includes('再来一局'))?.click()")
            await asyncio.sleep(0.4)
            await ev("window.__setCase('draw'); document.querySelectorAll('.move-btn')[1].click()")
            await asyncio.sleep(2.0)
            result["draw"] = await ev(
                "JSON.stringify({verdict: !!document.querySelector('.rps-verdict.draw')})")

            # 4. lose + 封号倒计时（300s 展示）
            await ev("[...document.querySelectorAll('.rps-btn')].find(b => b.textContent.includes('再来一局'))?.click()")
            await asyncio.sleep(0.4)
            await ev("window.__setCase('lose'); document.querySelectorAll('.move-btn')[2].click()")
            await asyncio.sleep(2.2)
            result["lose"] = await ev(r"""
(() => {
  const t = document.querySelector('.rps-mask')?.textContent || '';
  const replay = [...document.querySelectorAll('.rps-btn')]
    .find(x => x.textContent.includes('服刑中') || x.textContent.includes('再来一局'));
  return JSON.stringify({
    verdict: !!document.querySelector('.rps-verdict.lose'),
    countdown5m: /^封号 5:00|封号 4:59/.test(t),
    noteOk: t.includes('当前会话保留') && t.includes('无法重新登录'),
    replayDisabled: !!replay && replay.disabled,
    replayLabel: replay ? replay.textContent.trim() : '',
  });
})()""")
            await shot("lose")
            await asyncio.sleep(2.0)
            result["countdownTick"] = await ev(r"""
(() => {
  const t = document.querySelector('.rps-verdict.lose')?.textContent || '';
  return JSON.stringify({ticking: /封号 4:5\d/.test(t), text: t.trim()});
})()""")

            # 5. 短倒计时到 0 → 刑满释放
            await ev("location.reload()")
            await asyncio.sleep(2.5)
            await ev("window.__setCase && window.__setCase('release')")
            await asyncio.sleep(0.3)
            await ev("document.querySelectorAll('.move-btn')[0].click()")
            await asyncio.sleep(2.2)
            result["releaseShort"] = await ev(
                "JSON.stringify({countdown: (document.querySelector('.rps-verdict.lose')||{}).textContent || ''})")
            await asyncio.sleep(6.5)
            result["released"] = await ev(r"""
(() => {
  const t = document.querySelector('.rps-mask')?.textContent || '';
  const replay = [...document.querySelectorAll('.rps-btn')]
    .find(x => x.textContent.includes('再来一局'));
  return JSON.stringify({
    freeLine: t.includes('刑满释放'),
    replayEnabled: !!replay && !replay.disabled,
  });
})()""")
            await shot("released")

            # 6. ESC 关闭
            await cmd("Input.dispatchKeyEvent",
                      {"type": "keyDown", "key": "Escape", "code": "Escape",
                       "windowsVirtualKeyCode": 27})
            await asyncio.sleep(0.5)
            result["escClose"] = await ev(
                "JSON.stringify({closedFlag: !!window.__closed})")

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
