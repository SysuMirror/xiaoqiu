<script setup lang="ts">
import type { RpsMove, RpsPlayResult, RpsStats } from '@/api/opsagent/game'
import { onBeforeUnmount, onMounted, ref, useTemplateRef } from 'vue'
import { fetchRpsStats, playRps, RPS_MOVE_CN } from '@/api/opsagent/game'
import { fetchBanStatus } from '@/api/opsagent/ban'
import { showMsg } from '@/components/MessageBox'

const emit = defineEmits<{
  (e: 'close'): void
}>()

const MOVES: RpsMove[] = ['rock', 'scissors', 'paper']

const phase = ref<'pick' | 'playing' | 'result'>('pick')
const result = ref<RpsPlayResult | null>(null)
const stats = ref<RpsStats | null>(null)
const countdown = ref(0)
const revealed = ref<RpsMove | null>(null)
let countdownTimer: ReturnType<typeof setInterval> | null = null

const panel = useTemplateRef('panel')

function trapFocus(e: KeyboardEvent) {
  if (e.key !== 'Tab' || !panel.value)
    return
  const focusables = panel.value.querySelectorAll<HTMLElement>(
    'button:not(:disabled), [tabindex]:not([tabindex="-1"])')
  if (focusables.length === 0)
    return
  const first = focusables[0]
  const last = focusables[focusables.length - 1]
  if (e.shiftKey && document.activeElement === first) {
    e.preventDefault()
    last.focus()
  }
  else if (!e.shiftKey && document.activeElement === last) {
    e.preventDefault()
    first.focus()
  }
}

function onKeydown(e: KeyboardEvent) {
  if (e.key === 'Escape') {
    e.preventDefault()
    emit('close')
  }
  else {
    trapFocus(e)
  }
}

onMounted(async () => {
  document.addEventListener('keydown', onKeydown)
  document.body.style.overflow = 'hidden'
  panel.value?.querySelector<HTMLButtonElement>('.move-btn')?.focus()
  try {
    const bs = await fetchBanStatus()
    if (bs?.banned)
      syncCountdown(bs.remaining)
  }
  catch {
    /* 状态拉取失败不影响开局 */
  }
  stats.value = await fetchRpsStats()
})

onBeforeUnmount(() => {
  document.removeEventListener('keydown', onKeydown)
  document.body.style.overflow = ''
  if (countdownTimer)
    clearInterval(countdownTimer)
})

/** 同步剩余刑期倒计时；>0 时保持走秒，归零自停。开局邀请与结果页共用。 */
function syncCountdown(seconds: number) {
  countdown.value = Math.max(0, seconds)
  if (countdown.value > 0 && !countdownTimer) {
    countdownTimer = setInterval(() => {
      countdown.value -= 1
      if (countdown.value <= 0 && countdownTimer) {
        clearInterval(countdownTimer)
        countdownTimer = null
      }
    }, 1000)
  }
}

async function play(move: RpsMove) {
  if (phase.value === 'playing')
    return
  phase.value = 'playing'
  result.value = null
  revealed.value = null
  // 揭示前给一小段悬念；服务端结果早已定，纯属演出
  const revealDelay = new Promise(r => setTimeout(r, 900 + Math.random() * 500))
  try {
    const [res] = await Promise.all([playRps(move), revealDelay])
    result.value = res
    revealed.value = res.ai_move
    phase.value = 'result'
    syncCountdown(res.remaining_after)
    if (res.sentence || res.ban?.banned) {
      // 刑期有变化（封禁/加刑/减刑），让悬浮窗立即同步
      window.dispatchEvent(new CustomEvent('xq:ban-refresh'))
    }
    stats.value = await fetchRpsStats()
  }
  catch (e) {
    phase.value = 'pick'
    console.error(e)
    showMsg(e instanceof Error && e.message ? e.message : '猜拳失败，请稍后重试。')
  }
}

function backToPick() {
  phase.value = 'pick'
  result.value = null
  revealed.value = null
}

function fmtCountdown() {
  const m = Math.floor(countdown.value / 60)
  const s = countdown.value % 60
  return `${m}:${String(s).padStart(2, '0')}`
}
</script>

<template>
  <Teleport to="body">
    <div class="rps-mask xq-teleport" @click="emit('close')">
      <div
        ref="panel"
        class="rps-panel"
        role="dialog"
        aria-modal="true"
        aria-label="剪刀石头布"
        @click.stop
      >
        <header class="rps-head">
          <div>
            <h3>剪刀石头布</h3>
            <p>赢了没奖励 · 输了封号 5 分钟</p>
          </div>
          <button type="button" class="close-btn" aria-label="关闭" @click="emit('close')">
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" aria-hidden="true">
              <path d="M18 6L6 18M6 6l12 12" stroke="currentColor" stroke-width="2" stroke-linecap="round" />
            </svg>
          </button>
        </header>

        <!-- 出拳 -->
        <div v-if="phase === 'pick'" class="rps-body">
          <p class="rps-rule">
            规则简单：赢了什么都没有，输了封号 5 分钟、登录状态立即作废。
            小秋的拳由服务端密码学随机决定——公平，且不讲情面。
          </p>
          <div v-if="countdown > 0" class="rps-sentence-invite" role="status">
            <p class="invite-head">
              服刑中，剩 <strong>{{ fmtCountdown() }}</strong>。要不，赌一把？
            </p>
            <p class="invite-rule">
              减刑局：赢了刑期 -5 分钟（剩余不足直接释放），平局不变，输了 +5 分钟。
            </p>
          </div>
          <div class="move-row">
            <button
              v-for="m in MOVES"
              :key="m"
              type="button"
              class="move-btn"
              :aria-label="`出${RPS_MOVE_CN[m]}`"
              @click="play(m)"
            >
              <span class="move-icon" aria-hidden="true">
                <svg v-if="m === 'rock'" width="34" height="34" viewBox="0 0 24 24" fill="none">
                  <circle cx="12" cy="12" r="7.2" stroke="currentColor" stroke-width="2" />
                  <path d="M9 12.2c1-.8 2-.8 3 0s2 .8 3 0" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" />
                </svg>
                <svg v-else-if="m === 'scissors'" width="34" height="34" viewBox="0 0 24 24" fill="none">
                  <circle cx="6" cy="6" r="2.6" stroke="currentColor" stroke-width="2" />
                  <circle cx="6" cy="18" r="2.6" stroke="currentColor" stroke-width="2" />
                  <path d="M8.2 7.6 19.5 17.6M8.2 16.4 19.5 6.4" stroke="currentColor" stroke-width="2" stroke-linecap="round" />
                </svg>
                <svg v-else width="34" height="34" viewBox="0 0 24 24" fill="none">
                  <rect x="5.5" y="3.5" width="13" height="17" rx="2.5" stroke="currentColor" stroke-width="2" />
                  <path d="M9 8.5h6M9 12h6M9 15.5h3.5" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" />
                </svg>
              </span>
              <span class="move-label">{{ RPS_MOVE_CN[m] }}</span>
            </button>
          </div>
          <p v-if="stats" class="rps-stats">
            我的战绩 胜 {{ stats.me.wins }} / 负 {{ stats.me.losses }} / 平 {{ stats.me.draws }}
            · 被封 {{ stats.me.bans }} 次
            <span class="rps-global">全站 {{ stats.global.games }} 局，已有 {{ stats.global.bans }} 人被封</span>
          </p>
        </div>

        <!-- 揭晓 -->
        <div v-else-if="phase === 'playing'" class="rps-body rps-center" aria-live="polite">
          <p class="rps-thinking">
            小秋在想出什么<span class="dots"><i>.</i><i>.</i><i>.</i></span>
          </p>
        </div>

        <!-- 结果 -->
        <div v-else-if="result" class="rps-body rps-center">
          <p class="rps-reveal">
            你出 <strong>{{ RPS_MOVE_CN[result.user_move] }}</strong>
            · 小秋出 <strong>{{ RPS_MOVE_CN[result.ai_move] }}</strong>
          </p>

          <!-- 减刑局结算 -->
          <template v-if="result.sentence?.mode === 'commute'">
            <p class="rps-verdict win" aria-live="polite">
              减刑成功。{{ countdown > 0 ? `剩 ${fmtCountdown()}` : '当场释放！' }}
            </p>
            <p class="rps-sub">
              刑期 -5 分钟。{{ countdown > 0 ? '还想再减？再来一局。' : '' }}
            </p>
          </template>
          <template v-else-if="result.sentence?.mode === 'extend'">
            <p class="rps-verdict lose" aria-live="polite">
              加刑 5 分钟。剩 {{ fmtCountdown() }}
            </p>
            <p class="rps-sub">
              减刑局输了，刑期累计。不服？再来一局，赢回来。
            </p>
          </template>
          <template v-else-if="result.sentence?.mode === 'none'">
            <p class="rps-verdict draw" aria-live="polite">
              平局。刑期不变，剩 {{ fmtCountdown() }}
            </p>
            <p class="rps-sub">
              什么都没发生，刑期原地踏步。
            </p>
          </template>
          <!-- 普通局 -->
          <template v-else-if="result.result === 'win'">
            <p class="rps-verdict win" aria-live="polite">
              你赢了。
            </p>
            <p class="rps-sub">
              奖励：没有。说好的。
            </p>
          </template>
          <template v-else-if="result.result === 'draw'">
            <p class="rps-verdict draw" aria-live="polite">
              平局。
            </p>
            <p class="rps-sub">
              账号保住了，什么也没发生。
            </p>
          </template>
          <template v-else-if="result.ban?.banned">
            <p class="rps-verdict lose" aria-live="polite">
              你输了。封号 {{ fmtCountdown() }}
            </p>
            <p class="rps-sub">
              当前会话保留，但封禁期间无法重新登录，5 分钟后自动解封。
            </p>
            <p v-if="countdown <= 0" class="rps-sub free">
              刑满释放。要再来一局吗？
            </p>
          </template>
          <template v-else>
            <p class="rps-verdict lose" aria-live="polite">
              你输了。
            </p>
            <p class="rps-sub">
              {{ result.note || '本次免于封号。' }}
            </p>
          </template>

          <div class="rps-after">
            <button type="button" class="rps-btn primary" @click="backToPick">
              再来一局
            </button>
            <button type="button" class="rps-btn" @click="emit('close')">
              溜了
            </button>
          </div>
        </div>
      </div>
    </div>
  </Teleport>
</template>

<style scoped>
.rps-mask {
  position: fixed;
  inset: 0;
  z-index: 2600;   /* 高于站点 MobileHeader(2100)/BottomNavbar(2000)，与其他小秋覆盖层一致 */
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 16px;
  background: rgba(15, 23, 42, 0.55);
  backdrop-filter: blur(2px);
}

.rps-panel {
  display: flex;
  flex-direction: column;
  width: min(420px, 94vw);
  max-height: min(560px, 90vh);
  overflow: hidden;
  border-radius: 18px;
  background: var(--xq-surface);
  color: var(--xq-text);
  box-shadow: 0 12px 48px rgba(15, 23, 42, 0.25);
  animation: pop-in 0.2s ease;
}

.rps-panel button {
  touch-action: manipulation;
  -webkit-tap-highlight-color: transparent;
}

.rps-panel button svg {
  flex: none;
}

.rps-panel :focus-visible {
  outline: 2px solid var(--xq-accent);
  outline-offset: 2px;
  border-radius: 8px;
}

.close-btn svg {
  width: 18px !important;
  height: 18px !important;
}

@keyframes pop-in {
  from {
    transform: scale(0.96) translateY(6px);
    opacity: 0.6;
  }
  to {
    transform: scale(1) translateY(0);
    opacity: 1;
  }
}

.rps-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 10px;
  padding: 18px 18px 14px;
  border-bottom: 1px solid var(--xq-border);
}

.rps-head h3 {
  margin: 0;
  font-size: 16px;
  font-weight: 650;
  color: var(--xq-text);
}

.rps-head p {
  margin: 4px 0 0;
  font-size: 12px;
  color: var(--xq-text-muted);
}

.close-btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 32px;
  height: 32px;
  flex-shrink: 0;
  border: none;
  border-radius: 8px;
  background: transparent;
  color: var(--xq-text-muted);
  cursor: pointer;
}

.close-btn:hover {
  background: var(--xq-surface-muted);
  color: var(--xq-text);
}

.rps-body {
  padding: 16px 18px 18px;
  overflow-y: auto;
  overscroll-behavior: contain;
}

.rps-center {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 6px;
  text-align: center;
}

.rps-rule {
  margin: 0 0 14px;
  font-size: 12.5px;
  line-height: 1.7;
  color: var(--xq-text-secondary);
}

.rps-sentence-invite {
  margin: 0 0 14px;
  padding: 10px 14px;
  border: 1px solid rgba(239, 83, 80, 0.3);
  border-radius: 12px;
  background: rgba(239, 83, 80, 0.07);
}

.invite-head {
  margin: 0;
  font-size: 13px;
  font-weight: 600;
  color: var(--xq-text);
}

.invite-head strong {
  color: #e53935;
  font-variant-numeric: tabular-nums;
}

.invite-rule {
  margin: 4px 0 0;
  font-size: 11.5px;
  line-height: 1.6;
  color: var(--xq-text-muted);
}

.move-row {
  display: flex;
  gap: 10px;
}

.move-btn {
  flex: 1;
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 8px;
  padding: 18px 8px 14px;
  border: 1px solid var(--xq-border);
  border-radius: 14px;
  background: var(--xq-surface-muted);
  color: var(--xq-text-secondary);
  font-size: 13.5px;
  font-weight: 600;
  cursor: pointer;
  transition:
    color 0.15s ease,
    border-color 0.15s ease,
    background 0.15s ease,
    transform 0.12s ease;
}

.move-btn:hover:not(:disabled) {
  color: var(--xq-accent-deep);
  border-color: var(--xq-accent-border);
  background: var(--xq-accent-soft);
  transform: translateY(-2px);
}

.move-btn svg {
  width: 34px !important;
  height: 34px !important;
}

.move-icon {
  display: inline-flex;
}

.rps-stats {
  margin: 16px 0 0;
  font-size: 11.5px;
  line-height: 1.7;
  color: var(--xq-text-muted);
  font-variant-numeric: tabular-nums;
}

.rps-global {
  display: block;
}

.rps-thinking {
  margin: 22px 0;
  font-size: 15px;
  color: var(--xq-text-secondary);
}

.dots i {
  display: inline-block;
  animation: dot-blink 1.2s infinite;
  font-style: normal;
}

.dots i:nth-child(2) {
  animation-delay: 0.2s;
}

.dots i:nth-child(3) {
  animation-delay: 0.4s;
}

@keyframes dot-blink {
  0%,
  60%,
  100% {
    opacity: 0.2;
  }

  30% {
    opacity: 1;
  }
}

.rps-reveal {
  margin: 4px 0 10px;
  font-size: 13px;
  color: var(--xq-text-muted);
}

.rps-reveal strong {
  color: var(--xq-text);
}

.rps-verdict {
  margin: 0;
  font-size: 22px;
  font-weight: 700;
  letter-spacing: 0.01em;
}

.rps-verdict.win {
  color: var(--xq-accent-deep);
}

.rps-verdict.draw {
  color: var(--xq-text);
}

.rps-verdict.lose {
  color: #e53935;
  font-variant-numeric: tabular-nums;
}

.rps-sub {
  margin: 4px 0 0;
  font-size: 12.5px;
  line-height: 1.7;
  color: var(--xq-text-secondary);
  max-width: 320px;
  overflow-wrap: anywhere;
}

.rps-sub.free {
  color: var(--xq-accent-deep);
  font-weight: 600;
}

.rps-after {
  display: flex;
  gap: 10px;
  margin-top: 18px;
}

.rps-btn {
  padding: 9px 18px;
  border: 1px solid var(--xq-border);
  border-radius: 999px;
  background: var(--xq-surface);
  color: var(--xq-text-secondary);
  font-size: 13px;
  font-weight: 500;
  cursor: pointer;
  transition:
    color 0.15s ease,
    border-color 0.15s ease,
    background 0.15s ease;
}

.rps-btn:hover:not(:disabled) {
  color: var(--xq-accent-deep);
  border-color: var(--xq-accent-border);
}

.rps-btn:disabled {
  opacity: 0.55;
  cursor: not-allowed;
}

.rps-btn.primary {
  background: var(--xq-accent);
  border-color: var(--xq-accent);
  color: #fff;
}

.rps-btn.primary:hover:not(:disabled) {
  background: var(--xq-accent-deep);
  color: #fff;
}

@media (prefers-reduced-motion: reduce) {
  .rps-panel {
    animation: none;
  }

  .rps-panel *,
  .rps-panel *::before,
  .rps-panel *::after {
    animation-duration: 0.01ms !important;
    transition-duration: 0.01ms !important;
  }
}
</style>
