# 小秋 frontdesk 回复策略审计（SK-9273CE1A）

- 审计对象：`app/frontdesk.py`（440 行），联动面 `app/api/internal.py`、`app/main.py`
- 审计范围：@小秋 提及判定、bug 反馈识别闸门、每小时限频、`frontdesk_seen` 幂等去重、
  分类失败回退、与雁阵（Skein）`/internal/intake` 建单 / `/internal/skein/notify` 回写的联动
- 审计方式：静态逐行阅读 + 用桩模块（stub `httpx` / `app.db`）实跑纯函数验证边界
- 结论：**不改业务逻辑**；本文件只记录缺陷、边界条件与修复建议

> 说明：沙箱内未安装 `pytest` / `httpx`，`pytest tests/test_frontdesk.py` 无法直接执行；
> 文中标注「已复现」的结论均来自对 `_candidate` / `_fallback` / `_parse_json` 的桩化实测。

---

## 0. 结论摘要（按风险排序）

| # | 级别 | 问题 | 位置 |
|---|---|---|---|
| F1 | 高 | `failed` 状态无退避、不计入限频 → 每轮重复建单 / 重复回复 | `frontdesk.py:291-300,336-345` |
| F2 | 高 | 非 @ 的 `request` 在建单前被闸门拦掉，与注释「bug/request 照常建工单」及文档矛盾 | `frontdesk.py:312-317` |
| F3 | 高 | 限频闸门置于分类 / 建单之前且不落库 → 超额 bug 静默丢失；notify 回复不计数 | `frontdesk.py:300-301` |
| F4 | 高 | 「先查后写」非原子、标记晚于副作用 → 多实例/重启/写库失败时重复回复+重复建单 | `frontdesk.py:291-293,336-345` |
| F5 | 高 | 时间窗 + `LIMIT 30` 且不排除已见项 → 已处理项占满配额造成饥饿/永久漏读 | `frontdesk.py:353-415` |
| F6 | 高 | intake 失败仍回「已登记」，且状态记为 `replied` 不再补偿 | `frontdesk.py:95-113,336-345` |
| F7 | 高 | 裸 `@小秋`（3 字）被 `len(text)>=4` 过滤，显式提及漏判 | `frontdesk.py:282` |
| F8 | 中 | 提及用精确子串：全角 `＠`、`@ 小秋` 漏判；`@小秋酱`、引用历史 @ 误判；2000 字截断后漏判 | `frontdesk.py:60-62,282,302` |
| F9 | 中 | `COMPLAINT` 过宽、`ACTIONABLE` 无词边界 → LLM 成本虚高 + fallback 误建 bug 单 | `frontdesk.py:31-40,148-169` |
| F10 | 中 | 模型输出未归一化/未校验：大小写空白即漏；可解析但字段不符被永久 `skipped` | `frontdesk.py:304-317,328-332` |
| F11 | 中 | LLM 故障无熔断/无告警；45s×N 叠加 trace 3s；无 key 时静默降级 | `frontdesk.py:95-145,172-224` |
| F12 | 中 | `XQ_BOT_UID` 缺省 0 与匿名帖 `userID=NULL→0` 冲突 | `frontdesk.py:297,369,389,411` |
| F13 | 中 | notify 重复通知不幂等、不受限频；`origin_id` 解析失败静默 skip；只认 HTTP 200 不看 body | `internal.py:73-108`；`frontdesk.py:246` |
| F14 | 中 | intake / notify 契约耦合点缺契约测试（`origin_id`、ccomment 父级顺序、`code` 结构） | `frontdesk.py:323-340`；`internal.py:82-106` |
| F15 | 低 | HTML 实体未解码、prompt injection 可影响公开回复、`_trace` 记录 `forum:<uid>` | `frontdesk.py:60-62,178-181,201-209` |
| F16 | 低 | 启动/DDL 异常未兜底；逐条限频查询；缺索引；死参数与空行 | `frontdesk.py:269-288,423-431` |
| F17 | 低 | 文档与行为漂移；测试仅覆盖纯函数 | `README.md:11`、`docs/DEPLOY.md:68-76`、`tests/test_frontdesk.py` |

---

## 1. @小秋 提及判定

### F7（高）裸 `@小秋` 被长度闸门过滤

`_candidate()` 的第一个门槛是 `len(text) >= 4`：

```python
# app/frontdesk.py:282
return bool(text) and len(text) >= 4 and (
    "@小秋" in text or bool(COMPLAINT.search(text))
)
```

`item["text"]` 在 `one_round()` 里已经过 `_plain()` 压空白（`frontdesk.py:367,388,410`），
所以只发一句 `@小秋` 的帖子/评论长度恒为 3，**不会进入候选，直接 `_mark("skipped")`**。
用户最明确的求助方式反而永远得不到回应。

桩化实测（`_plain` 后调用 `_candidate`）：

```
'@小秋'              -> candidate=False
'@小秋 ' / '@小秋\n' -> _plain 后仍为 '@小秋'，candidate=False
'@小秋（在吗）'      -> candidate=True
```

**修复建议**：把提及与长度门槛解耦，例如

```python
mention = bool(MENTION.search(text))
return bool(text) and (mention or (len(text) >= 4 and COMPLAINT.search(text)))
```

同时把 `handle()` 里的 `mention = "@小秋" in item["text"]`（`:302`）复用同一判定函数，
避免 `_candidate` 与 `handle` 两处规则再次漂移。

### F8（中）子串匹配的误判/漏判

当前判定是裸子串（`frontdesk.py:282,302`）：

- **漏判**：全角 `＠小秋`、`@ 小秋`（@ 与名字之间有空格）、平台若把提及渲染成
  `<a ...>@小秋</a>` 之外的格式（`_plain` 只保留标签内文本）。
- **误判**：`@小秋酱`、`有人知道@小秋是谁吗`、引用父楼时带出的历史 `@小秋`
  都会被当成「本次 @ 了小秋」，从而绕过「非 @ 只回 bug」的降噪闸门。
- **截断漏判**：`_plain` 默认 `limit=2000`（`:60-62`）先截断再判定；长帖中第 2000 字之后的
  `@小秋` / 故障关键词永远看不到。帖子正文和评论正文都可能触发。

**修复建议**：

1. 定义单一 `_mentioned(text)`（正则或论坛真实提及格式），支持全角 `＠`、可选空白，
   并用「非中文字符/非字母数字」边界排除 `@小秋酱` 这类近似账号；若论坛有结构化提及字段，
   优先用字段而不是文本匹配。
2. 判定/检索使用的截断长度应明显大于用户可见正文上限，或在截断前先抽取提及与关键词。
3. 对「引用块」做剥离（若论坛引用有固定标记），避免历史 @ 触发本轮回复。

---

## 2. bug 反馈识别闸门

### F9（中）关键词面过宽，fallback 误建 bug 单

`COMPLAINT` 包含 `图片|视频|头像|评论|建议|希望|能不能|体验|问题` 等泛词（`:31-36`），
`ACTIONABLE` 更松且无词边界（`:37-40`）。两个后果：

1. **成本**：分享图片、提建议、日常提问都会进入候选并调用一次 LLM（每类 `LIMIT 30`，见 F5）。
2. **误建单**：LLM 不可用（无 key 或报错）时走 `_fallback()`，只要命中 `ACTIONABLE` 就判 `bug`
   并建单。以下在桩化实测中都会变成 `kind=bug` + 「已登记」回复：

```
'今天登录了一下，界面不错'      -> bug（命中“登录”）
'@小秋 这个卡片不错'            -> bug（命中“卡”）
'好坏参半…'                    -> bug（命中“坏”）
'debugger 挺好用'              -> bug（命中“bug”）
```

`ACTIONABLE` 里的 `卡`、`坏`、`bug`、`登录`、`上传`、`下载` 都没有词边界或上下文约束。

**修复建议**：

- 给英文词加边界（`\bbug\b`），把 `卡|坏` 换成 `卡顿|卡死|坏了|打不开|报错` 等组合词；
- fallback 场景不要把关键词直接升级为 `bug` 建单：可降级为「不计单的回复」，
  或建 `severity=low` 的疑似单并在 summary 标注 `fallback`；
- 给 fallback 建单加独立的小时配额（见 F3），避免 LLM 故障期批量灌单。

### F2（高）非 @ 的 `request` 在 intake 之前被拦掉

```python
# app/frontdesk.py:312-317
kind_of = str(verdict.get("kind") or "venting")
# 主动回复只允许 bug；非 @ 的建议/吐槽一律不回（bug/request 照常建工单）。
if kind_of != "bug" and not mention:
    await _mark(pool, kind, item_id, "skipped")
    return
if kind_of in ("bug", "request"):
    ... # intake 在闸门之后
```

注释写「bug/request 照常建工单」，但 `request` 且 `mention=False` 时会在 `:314` 直接
`return`，**永远不会走到 `:317` 的建单分支**。也就是说：

- 普通用户发一条「希望增加夜间模式」→ 模型判 `request` → 被标记 `skipped`，不建单；
- 只有 `@小秋` 的需求才会建单。

这与注释、`SYSTEM` 提示词（`:26`「可执行 bug/需求」）、`docs/DEPLOY.md:68-69`
（「bug/需求调 Skein intake 建单」）以及 `README.md:11-12` 均不一致。若产品意图确实是
「需求也建单但不公开回复」，则建单必须上移到闸门之前；若意图是「只收 bug」，则应改注释与文档。

**修复建议**：把「是否建单」与「是否公开回复」拆成两个独立决策：

```python
if kind_of in ("bug", "request"):
    ... intake ...
if kind_of != "bug" and not mention:
    reply 不发送（但已建单的仍然落库）
```

并补一条单测覆盖 `mention=False, kind=request`。

### F10（中）模型输出未归一化、未校验

- `action` 精确比较 `"reply"`（`:304-305`）：模型返回 `"Reply"`、`" reply"`、`"reply "` 一律
  视为放弃，且随后 `_mark("skipped")` **永久不再处理**。
- `kind_of` 同样精确比较 `"bug"`（`:314,317`）：`"Bug"`、`" bug"`、`"request "` 会漏建单
  （mention 时只回不建；非 mention 时直接跳过）。
- `_parse_json` 只要解析出 dict 就返回（`:65-81`）。`{"foo":1}` 这种 schema 不符但可解析的输出
  会让 `action=""` 走 `:305` 的 `skipped`，把一条真实 bug 永久丢弃。**建议把「可解析但字段非法」
  当作分类失败走 fallback/重试，而不是终态 skip。**
- `severity`、`category`、`title`、`reply` 均未做类型/枚举校验（`:308,328-332`）：
  非字符串会被 `_plain` 用 `str()` 拼成 `{'...'}` 写进公开回复或工单；`severity` 任意字符串
  会原样透传给 Skein。建议 `strip().lower()` 归一化，并对 `severity ∈ {low,normal,high}`、
  `action ∈ {reply,skip}`、`kind ∈ {bug,request,venting,skip}` 做白名单，非法即视为失败。
- 额外的漏单路径：模型判 `kind=bug` 但 `reply` 为空时，`:309-311` 先 `skipped` 再返回，
  建单永远不发生。建议「工单」不依赖 `reply` 是否生成成功。

### 提示词与闸门不一致（低）

`SYSTEM` 仍写「action: reply（所有吐槽/反馈都回）」（`:23`），而运行时只允许非 @ 的 bug 发出
（`:314`）。模型为吐槽生成的回复会被丢弃，既浪费 token，也容易把 `request` 判成 `venting`。
建议同步提示词：明确「未 @ 时只有 bug 会公开回复；request 只登记不回复」。

---

## 3. 每小时限频

### F3（高）限频位置与计数口径

```python
# app/frontdesk.py:300-301
if await _replied_last_hour(pool) >= _env_int("FRONTDESK_MAX_PER_HOUR", 12):
    return
```

问题：

1. **闸门在 classify / intake 之前**。一旦本小时回复额度用满，后续条目既不公开回复、
   **也不建工单**，而且**不写 `frontdesk_seen`**。默认 `FRONTDESK_LOOKBACK=600`（`:353`），
   等额度恢复时这些条目早已滑出 10 分钟查询窗口 → **静默永久丢失**，无日志、无指标。
   若产品意图是「限的是公开回复量」，建单不应被同一闸门阻断。
2. **计数只认 `status='replied'`**（`:269-276`）：`failed` 重试、fallback 建单都不计数，
   因此限频无法约束最需要约束的重复副作用（见 F1）。
3. **notify 回写不受限频**：`app/api/internal.py:107` 直接 `post_reply`，不经过
   `frontdesk_seen`，也不计入 `FRONTDESK_MAX_PER_HOUR`；若限频语义是「小秋每小时公开发言上限」，
   该路径可突破上限。
4. **非原子**：`count` 与随后的 `post_reply` / `_mark` 分离（单进程单任务时无碍，
   多实例或不退出重叠部署时会超发）。

**修复建议**：

- 拆成两个额度：`max_replies_per_hour`（公开回复）与 `max_intakes_per_hour`（建单），
  分别计数；建单失败重试也计入 intake 额度。
- 超额时不要「静默 return」：至少 `log.info` + 计数器，并考虑把条目落成 `deferred`
  状态（带 `next_retry_at`），避免依赖 lookback 窗口。
- 若限频要覆盖 notify，回写处也应走同一计数（或单独 `max_notify_per_hour`）。
- 计数查询加 `(status, created_at)` 索引；`_replied_last_hour` 目前是逐条调用（`:300`），
  建议每轮算一次并传入。

### 边界值

`_env_int` 会把负值夹到 0（`:53-57`）。`FRONTDESK_MAX_PER_HOUR=0` 时 `>= 0` 恒真，
monitor 变成「只扫不处理且不落库」的空转；`FRONTDESK_LOOKBACK=0` 时 SQL 变成
`NOW() - INTERVAL 0 SECOND`，几乎查不到任何历史项。这两种配置建议在启动时显式告警。

---

## 4. `frontdesk_seen` 幂等与去重

### F4（高）先查后写非原子、标记晚于副作用

```python
# app/frontdesk.py:290-293
status = await _seen(pool, kind, item_id)
if status in ("replied", "skipped"):
    return
...
# :336  intake（有副作用）
# :342  公开发回复（有副作用）
# :345  才落库
await _mark(pool, kind, item_id, "replied" if ok else "failed")
```

- 两个 monitor 实例（滚动发布重叠、双副本、手工再起一个）会同时读到空状态，
  然后**各发一次回复、各建一次单**。PK 只能防重复行，防不住重复副作用。
- 进程若在 `:342` 与 `:345` 之间被杀（发布/驱逐/OOM），重启后会再次回复；
  在 `:336` 与 `:345` 之间被杀，会再次建单。
- `_mark` 若因 PG 抖动失败，异常被 `one_round` 吞掉（`:416-420`），下一轮同样重复副作用。

**修复建议**：改成「先原子认领，再执行副作用」：

```sql
-- 认领：只有抢到 claimed 的实例才继续
INSERT INTO frontdesk_seen(kind, item_id, status, attempts)
VALUES ($1,$2,'claimed',1)
ON CONFLICT (kind,item_id) DO UPDATE
  SET status='claimed', attempts=frontdesk_seen.attempts+1, created_at=now()
WHERE frontdesk_seen.status='failed'
RETURNING attempts;
```

并在 intake 侧携带幂等键（`message_id` / `thread_id`，见 F14），使重试可安全去重。
`_mark` 最好在 `post_reply` 成功后立刻写入；若担心「已发但没记」，用回复端幂等号兜底。

### F1（高）`failed` 重试无上限、无限频

`failed` 不属于终态（`:292` 只跳过 `replied`/`skipped`），于是**每轮（默认 60s）都会重新
classify + intake + post_reply**。触发条件很常见：

- `INTERNAL_TOKEN` 未配置 → `post_reply` 直接 `return False`（`:232-233`）→ 永远 `failed`；
- 回写接口 5xx / 超时（`:246-249`）；
- `GO_INTERNAL_URL` 配错。

此时若 `SKEIN_TOKEN` 正常，**同一帖子每 60 秒被判一次 bug、建一次单**；`_replied_last_hour`
只数 `replied`，拦不住（F3）。若 Skein 的 followup 合并仅按帖去重，单量不至于爆炸，
但 LLM 调用、trace、日志与重复 followup 会持续产生；评论/楼中楼还可能重复回帖。

**修复建议**：

- 表增加 `attempts`、`next_retry_at`、`intake_code`、`reply_status` 等列；
- 指数退避（1m/5m/30m…）并在 N 次后落 `failed_permanent`，同时打告警；
- 区分「intake 已成功、仅回复失败」「回复已成功、仅标记失败」，分别重试，避免重复建单。

### 其他去重边界

- **编辑/补充不重扫**：`skipped` 是终态。帖子发表后被编辑加入 `@小秋` 或故障描述，
  永远不会被重新评估；评论若只改内容亦同。若论坛有 `edit_time`，建议纳入键或重新入池。
- **匿名项**：`userID` 为 NULL 的帖子/评论在 `one_round` 被转成 `0`（`:369,389,411`）；
  当 `XQ_BOT_UID` 未配置时 `_env_int(...,0)` 也是 0（`:297`）→ 匿名内容全部被当成机器人跳过。
  建议「机器人过滤」只在 `XQ_BOT_UID` 显式 > 0 时启用，并单独处理 `user_id` 缺失。
- **私密/删除**：查询用 `IFNULL(p.is_private,0)=0`（`:358,378,400`），对 NULL 友好；
  但未看到 `is_deleted` 类字段过滤，且 `internal.py:47` 的 RAG 取样用的是 `p.is_private=0`
  （NULL 会被排除），两处口径不一致。若论坛有删除标记，建议统一补过滤，避免对已删内容建单。

---

## 5. 分类失败回退

### F11（中）无熔断、无告警、静默降级

```python
# app/frontdesk.py:176-177
if not key:
    return _fallback(item, mention)
...
# :211-224
except (httpx.HTTPError, ValueError, KeyError) as exc:
    ... status="error" ...
    return _fallback(item, mention)
```

- 无 key 时**不写 trace、不打日志**，线上看起来「一切正常」，实际已退化为关键词规则；
  结合 F9，会把「登录/卡片/好坏」误建成 bug 单。建议启动时 `log.warning` 一次并暴露指标。
- HTTP 401/403（key 失效）、429（限流）、5xx（故障）都走同一条静默 fallback；
  key 失效应显著告警，429/5xx 宜退避而不是逐条重试。
- **无熔断**：每条约 45s 超时（`:184`），单轮最多 90 条（3 类 × `LIMIT 30`），
  最坏一轮可达 **约 67 分钟**；若 `_trace` 也超时（3s）还会叠加。轮次时长一旦超过
  `FRONTDESK_LOOKBACK`，期间新到内容会在下一轮查询前滑出窗口——这是 F5 的另一条丢消息路径。
  建议：更短的分类超时（如 8-12s）、连续失败熔断（跳过本轮剩余 LLM 调用）、
  以及「整轮时限」保护。
- 异常捕获面偏窄：`data` 若不是 dict（如 list），`data.get` 抛 `AttributeError`；
  `choices` 元素不是 dict 也会抛。这些不会被 `except` 捕获，异常冒到 `one_round`，
  条目**不落库**，下一轮重试。建议统一捕获 `Exception`（保留原始 error 到 trace）
  并明确失败态，而不是依赖外层兜底。
- `_trace` 在成功路径位于 try 内（`:201-209`）：若 trace 自身抛出非 `httpx.HTTPError`，
  会被当成分类失败并再次 trace 错误，导致已成功的分类被丢弃。建议把 trace 放入
  `try/except Exception` 的独立保护块。

### F6（高）建单失败却对外承诺「已登记」

`_skein_call` 在以下情况都返回 `None`（`:95-113`）：`SKEIN_TOKEN` 为空、非 200、超时、
响应非 JSON。`handle` 随后照常发送回复：

- 模型/fallback 生成的回复本身就含「收到，已登记，我先复现一下」；
- 即使回复不含该句，代码也不会因为 `ticket is None` 改变措辞。

结果：**工单根本没建，用户却被告知已登记**；且 `_mark("replied")` 后不会补建。
同样，`code` 取 `ticket["code"]`（`:337`），若契约返回嵌套结构（如 `{"data":{"code":...}}`）
或字段名不同，码也丢失。

**修复建议**：

- 建单失败时：不承诺「已登记」，改为「我先看一下」；或保留 `intake_pending` 状态，
  下一轮只补建单、不重复回复；
- 建单成功后再把 `code` 持久化，notify 对账/回复重试都以它为准；
- 启动时校验 `SKEIN_TOKEN` / `INTERNAL_TOKEN` / `GO_INTERNAL_URL`，缺失即 fail-fast 或显著告警。

---

## 6. 与雁阵 intake / notify 的联动

### F14（中）契约耦合点，缺契约测试

`intake` 请求体（`:323-333`）：

```json
{"source":"forum_*","thread_id":"<post>","message_id":"<item>",
 "requester":"forum:<uid>","title":...,"summary":...,
 "category":"bug|request","severity":...,"visibility":"market",
 "parent_id":"<pcomment>（仅 ccomment）"}
```

而回写侧 `app/api/internal.py:82-106` 期望的 `origin_id` 格式是：

- `forum_post` → `"<post_id>"`
- `forum_comment` → `"<post_id>:<pcomment_id>"`
- `forum_ccomment` → `"<post_id>:<pcomment_id>:<ccomment_id>"`

也就是说，Skein 必须能从 intake 的三个字段**还原出上述顺序**（ccomment 尤其依赖
`thread_id:parent_id:message_id`）。当前仓库内没有 intake 实现/契约文档（`docs/chat-events.md`
不在本仓），无法静态确认：

1. Skein 是否按 `thread_id[:parent_id]:message_id` 生成 `origin_id`；
2. `category` 是否接受 `bug|request`（还是域名分类）；
3. `visibility:"market"` 是否对所有来源都合适；
4. intake 在 followup 合并时是否返回既有 `code`；
5. 重复 intake（F1/F4 场景）是否幂等。

**建议**：补一个跨仓契约测试（mock intake，断言 payload 字段；mock notify，断言
`origin_id` 与 `post_reply` 参数一一对应），并把 `docs/chat-events.md` 的关键段落到本仓
`docs/`，避免两边各自演进。

### F13（中）notify 回写的边界

`internal.py:73-108`：

- **不幂等**：同一 `public_status` 被 Skein 重发（网络重试、控制台回放）会重复回帖；
  建议按 `(origin_id, public_status, code)` 去重，或让 Skein 带事件 id。
- **静默丢弃**：`origin_id` 解析失败只返回 `{"ok": true, "skipped": true}`（`:88/94/101`，未知 origin 与空文案在 `:103/:106` 同样静默），
  调用方看到成功、进度其实没回写。建议区分 4xx 与「未知状态」，未知 origin 记 warning。
- **只认 HTTP 200**：`post_reply` 用 `response.status_code == 200` 判断成功（`frontdesk.py:246`），
  不看响应体。若主站接口以 200 + `{"ok": false}` 表示业务失败，会被误记为成功 → 用户收不到回执，
  且 `frontdesk_seen` 不会再重试。建议同时校验 body 的 `ok`。
- **限频缺口**：notify 回写不计入 `FRONTDESK_MAX_PER_HOUR`（见 F3）。
- **私密帖竞态**：notify 回写前不再检查帖子是否已转私密；若 Skein 验收较晚，
  可能在已私密/已删除帖下回复。`_fetch_post` 只用于 RAG。建议回写前做一次可见性检查。

### F12（中）`XQ_BOT_UID` 缺省值

`_env_int("XQ_BOT_UID", 0)`（`:297`）与匿名内容的 `user_id=0`（`:369,389,411`）撞值：
未配置时所有匿名帖/评论被当作机器人跳过；配置错误（写成字符串/空）也会落回默认。
建议：仅在变量存在且 >0 时启用过滤；启动日志打印生效的 bot uid；匿名 `user_id` 用 `-1`
或 `None` 表示，避免与 bot 混淆。

---

## 7. 其他边界与工程问题

### F5（高）轮询窗口与 `LIMIT` 饥饿

`one_round()` 的三条 SQL 都是「最近 `LOOKBACK` 秒 + `ORDER BY time ASC LIMIT 30`」，
**没有排除已在 `frontdesk_seen` 的条目**（`:355-360,374-379,394-401`），并且跨库
（MySQL 论坛库 / PG `frontdesk_seen`）无法直接 JOIN。后果：

- 已处理的 30 条会在窗口内（10 分钟）每轮重复占满 `LIMIT`，新条目要等它们滑出窗口才被看到
  → 常态下最多 ~10 分钟延迟；
- 当流量 > 30 条 / 10 分钟时，总会有 ≥30 条「更老的已处理项」占据配额，
  更新的条目可能**永远选不中** → 永久漏读；
- 单轮耗时 > `LOOKBACK`（LLM 故障，见 F11）时，期间新到内容在下一轮查询前过期 → 永久漏读。

**修复建议**（任选/组合）：

1. 用游标（每类记录 `last_seen_time, last_id`）替代固定回看窗口，窗口只用于首次初始化；
2. 先把候选 id 批量取出（如 200 条）在内存里过滤 `frontdesk_seen`，再截断处理；
3. 或把 `frontdesk_seen` 同步一份到论坛侧/外键化，SQL 直接 `NOT EXISTS`；
4. `LIMIT` 改为「按未处理数」而非「按总条数」，并给每轮时长加硬上限。

### F15（低）内容处理与注入面

- `_plain` 只做 `re.sub(r"<[^>]+>", " ", ...)`（`:60-62`）：不解码 `&nbsp;`/`&lt;`
  （测试明确断言保留 `&nbsp;`），`<script>alert(1)</script>` 会保留 `alert(1)` 文本；
  用户文本进入 LLM、工单和（经模型）公开回复。建议 `html.unescape` + 去除 `script/style`。
- 用户正文直接拼进 `user` 消息（`:178-181`），无分隔符/边界。恶意帖可尝试 prompt injection
  让小秋输出带链接/诱导性内容；`reply` 只经 `_plain`（保留 URL 与 Markdown）。
  建议加分隔标记、在提示词中声明「以下为不可信用户内容」、并对公开回复做链接白名单/长度约束。
- `_trace` 的 `actor=f"forum:{user_id}"`、`session_id` 含帖子/评论 id（`:201-209`），
  会把论坛用户与内容落到 Skein 的 LLM 调用日志。若属内部审计可接受，但建议在文档中明确留存边界。

### F16（低）健壮性/清理

- `run_monitor` 的 `pool.execute(DDL)`（`:428`）与 `create_pool` 未捕获异常；失败时
  asyncio 任务直接结束，`main.py:142-145` 只记「monitor on」，不会重启 → 前厅静默停摆。
  建议外层 superviser/循环内重试，或加 `/health` 暴露 monitor 心跳。
- 关闭时 `monitor_task.cancel()`（`main.py:148-149`）可能打断在途条目（已回复未落库 → F4）。
  建议 `asyncio.shield` 或设置停止标志处理完当前条目。
- `_replied_last_hour` 每条候选执行一次（`:300`），一轮最多 90 次额外查询；
  建议每轮取一次缓存。
- `frontdesk_seen` 只有主键，没有 `(status, created_at)` 索引；30 天保留期内数据量上来后
  count 会变慢。
- `_candidate(kind, text)` 的 `kind` 参数已不再使用（`:279`），`handle` 里另算 `mention`（`:302`），
  规则分散；建议合并为单一分类入口，减少漂移。`:285-288` 有三行多余空行。
- `_fallback` 的 `title` 取自 `item["title"]`，帖子标题可能为 `None`（`:150`），
  走到 `_plain(None,120)` 得到空串；Skein 若要求非空标题会拒单。建议回退到正文前缀。
- 依赖 `item["user_id"]`/`item["post_id"]` 等键（`:297,325,343`），若上游字段改名会
  `KeyError` 并被 `one_round` 吞掉；建议入口做一次 schema 校验。

### F17（低）文档漂移与测试缺口

- `README.md:11`、`docs/DEPLOY.md:68` 仍写「所有吐槽都回」；2ffc80b 之后实际只回
  非 @ 的 bug 与所有 @ 提及。
- `docs/DEPLOY.md:76` 写「评论一律交给 LLM 判断」，但 2ffc80b 已把 `_candidate` 改成
  关键词/@ 预筛，大量普通评论不会送 LLM。
- `docs/DEPLOY.md:75-76` 的「bug/需求建单」与 F2 实现不符。
- `tests/test_frontdesk.py` 只覆盖 `_candidate`/`_fallback`/`_parse_json`/`_plain`/`public_message`，
  没有覆盖 `handle()` 的闸门、限频、`failed` 重试、intake 失败补偿。
  建议补异步单测（fake pool / monkeypatch `classify`、`_skein_call`、`post_reply`）：
  - 裸 `@小秋`、全角 `＠`、`@小秋酱`；
  - 非 @ request 是否建单（F2 的回归锚点）；
  - `failed` 条目的重试次数上限与 intake 去重（F1）；
  - 限频期间条目是否落 `deferred` 而非静默消失（F3）；
  - `action="Reply"` / `kind="Bug "` 的归一化（F10）。

---

## 8. 建议的修复顺序（不改业务逻辑的前提下）

1. **止血**：F1（失败重试上限 + 建单/回复状态拆分）、F4（原子认领）、F6（失败不承诺已登记）、
   F3（限频不阻断建单 / 不静默丢单）。
2. **一致性**：F2（request 建单位置与文档统一）、F7/F8（统一提及判定函数）、F10（输出归一化）。
3. **吞吐与可靠性**：F5（游标/过滤已见）、F11（超时/熔断/告警）、F12（bot uid 语义）。
4. **联动契约**：F13/F14（幂等键、origin_id 契约测试、notify 去重）。
5. **清理**：F9（关键词收紧）、F15-F17（内容处理、健壮性、文档与测试）。

---

## 附录：本次用到的复现方式

```python
# 桩掉 httpx / app.db 后可单独调用纯函数
import sys, types
sys.path.insert(0, ".")            # 仓库根
sys.modules["httpx"] = types.SimpleNamespace(AsyncClient=object, HTTPError=Exception)
sys.modules["app.db"] = types.ModuleType("app.db")
import app.frontdesk as f

assert not f._candidate("post", f._plain("@小秋"))       # F7：裸提及被过滤
assert f._candidate("post", "今天登录了一下，界面不错")    # F9：命中“登录”
assert f._fallback({"text": "今天登录了一下，界面不错", "title": None})["kind"] == "bug"  # F9
assert f._fallback({"text": "@小秋 这个卡片不错", "title": None}, True)["kind"] == "bug"   # F9
```

> 完整回归仍需在装有 `httpx`/`asyncpg`/`pytest` 的环境执行
> `pytest tests/test_frontdesk.py`（本沙箱缺依赖，未执行）。
