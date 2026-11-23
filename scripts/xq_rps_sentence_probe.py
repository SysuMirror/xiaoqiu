"""减刑局 UI 探针（独立于 xq_rps_probe.py 的并行改动，验证 sentence 全流程）。

场景：干净用户无邀请横幅、普通赢/首封、封禁期开局邀请、减刑成功（含当场释放）、
加刑累计、平局刑期不变。stub 网络层零真实副作用。
用法: .venv/Scripts/python scripts/xq_rps_sentence_probe.py <out.json>
"""
import asyncio
import base64
import json
import subprocess
import sys
import urllib.request

import websockets

sys.stdout.reconfigure(encoding="utf-8")

EDGE_PORT = 9335


async def main():
    out = sys.argv[1] if len(sys.argv) > 1 else "scripts/xq_rps_sentence_probe.json"
    proc = subprocess.Popen([
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        "--headless=new", "--disable-gpu", "--hide-scrollbars",
        "--force-device-scale-factor=1",
        f"--remote-debugging-port={EDGE_PORT}",
        "--user-data-dir=C:/Users/ASUS/AppData/Local/Temp/xq-rps-sentence",
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

            BASE = "http://localhost:5274/new/__rps_harness3.html"

            async def ev(expression):
                res = await cmd("Runtime.evaluate",
                                {"expression": expression, "returnByValue": True})
                return res.get("result", {}).get("result", {}).get("value")

            async def nav(query=""):
                await cmd("Page.navigate", {"url": BASE + query})
                for _ in range(30):
                    await asyncio.sleep(1)
                    res = await ev("JSON.stringify({ready: !!window.__ready,"
                                   " err: window.__err || ''})")
                    if res and '"ready":true' in res:
                        return True
                return False

            async def shot(name):
                s = await cmd("Page.captureScreenshot", {"format": "png"})
                with open(out.replace(".json", f"-{name}.png"), "wb") as f:
                    f.write(base64.b64decode(s.get("result", {}).get("data", "")))

            result = {}
            if not await nav():
                print("HARNESS not ready")
                return
            err = await ev("window.__err || ''")
            if err:
                print("HARNESS-ERROR:", err[:1500])
                return

            async def click_move(idx=0):
                await ev(f"document.querySelectorAll('.move-btn')[{idx}].click()")
                await asyncio.sleep(1.8)

            async def replay():
                await ev("[...document.querySelectorAll('.rps-btn')]"
                         ".find(b => b.textContent.includes('再来一局'))?.click()")
                await asyncio.sleep(0.4)

            # 1. 干净用户：无邀请横幅；普通赢局
            result["clean"] = await ev(
                "JSON.stringify({open: !!document.querySelector('.rps-mask'),"
                " inviteHidden: !document.querySelector('.rps-sentence-invite')})")
            await click_move(0)
            result["win"] = await ev(r"""
(() => {
  const t = document.querySelector('.rps-mask')?.textContent || '';
  const replay = [...document.querySelectorAll('.rps-btn')]
    .find(x => x.textContent.includes('再来一局'));
  return JSON.stringify({
    winVerdict: !!document.querySelector('.rps-verdict.win'),
    noReward: t.includes('奖励：没有'),
    replayEnabled: !!replay && !replay.disabled,
  });
})()""")
            await shot("win")

            # 2. 普通首封：倒计时 + 回开局出现邀请
            await ev("window.__setCase('lose')")
            await replay()
            await click_move(2)
            result["freshLose"] = await ev(r"""
(() => {
  const t = document.querySelector('.rps-mask')?.textContent || '';
  return JSON.stringify({
    verdict: !!document.querySelector('.rps-verdict.lose'),
    countdownOk: /封号 [45]:\d{2}/.test(t),
  });
})()""")
            await shot("lose")
            await replay()
            result["invite"] = await ev(r"""
(() => {
  const inv = document.querySelector('.rps-sentence-invite');
  const t = inv?.textContent || '';
  return JSON.stringify({
    shown: !!inv,
    headOk: /服刑中，剩 [45]:\d{2}/.test(t),
    ruleOk: t.includes('赢了刑期 -5 分钟') && t.includes('输了 +5 分钟'),
  });
})()""")
            await shot("invite")

            # 3. 减刑成功（带初始封禁重新加载）：270s 服刑中开局，赢 → 剩 4:00
            if not await nav("?ban=270"):
                print("nav2 fail"); return
            result["invite270"] = await ev(
                "JSON.stringify({invite: !!document.querySelector('.rps-sentence-invite')})")
            await ev("window.__setCase('commute')")
            await click_move(0)
            result["commute"] = await ev(r"""
(() => {
  const t = document.querySelector('.rps-mask')?.textContent || '';
  return JSON.stringify({
    verdictOk: (document.querySelector('.rps-verdict.win')?.textContent || '').includes('减刑成功'),
    remainOk: /剩 [34]:\d{2}/.test(t),
    againOk: t.includes('还想再减'),
  });
})()""")
            await shot("commute")

            # 4. 当场释放（剩 60s 赢）
            if not await nav("?ban=60"):
                print("nav3 fail"); return
            await ev("window.__setCase('release')")
            await click_move(1)
            result["release"] = await ev(r"""
(() => {
  const t = document.querySelector('.rps-mask')?.textContent || '';
  return JSON.stringify({
    verdictOk: (document.querySelector('.rps-verdict.win')?.textContent || '').includes('当场释放'),
  });
})()""")
            await shot("release")

            # 5. 加刑（带初始封禁）：输 → 剩 8:00
            if not await nav("?ban=300"):
                print("nav4 fail"); return
            await ev("window.__setCase('extend')")
            await click_move(2)
            result["extend"] = await ev(r"""
(() => {
  const t = document.querySelector('.rps-mask')?.textContent || '';
  return JSON.stringify({
    verdictOk: (document.querySelector('.rps-verdict.lose')?.textContent || '').includes('加刑 5 分钟'),
    remainOk: /剩 [78]:\d{2}/.test(t),
    tauntOk: t.includes('赢回来'),
  });
})()""")
            await shot("extend")

            # 6. 平局刑期不变
            if not await nav("?ban=300"):
                print("nav5 fail"); return
            await ev("window.__setCase('none')")
            await click_move(1)
            result["none"] = await ev(r"""
(() => {
  const t = document.querySelector('.rps-mask')?.textContent || '';
  return JSON.stringify({
    verdictOk: (document.querySelector('.rps-verdict.draw')?.textContent || '').includes('刑期不变'),
    remainOk: /[45]:\d{2}/.test(t),
  });
})()""")

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
