<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref } from 'vue'
import { fetchBanStatus } from '@/api/opsagent/ban'
import { showMsg } from '@/components/MessageBox'

const POLL_MS = 60_000

const banned = ref(false)
const remaining = ref(0)
const reason = ref('')
let pollTimer: ReturnType<typeof setInterval> | null = null
let tickTimer: ReturnType<typeof setInterval> | null = null

function fmt(s: number) {
  const h = Math.floor(s / 3600)
  const m = Math.floor((s % 3600) / 60)
  const sec = s % 60
  const two = (n: number) => String(n).padStart(2, '0')
  return h > 0 ? `${h}:${two(m)}:${two(sec)}` : `${m}:${two(sec)}`
}

function stopTicking() {
  if (tickTimer) {
    clearInterval(tickTimer)
    tickTimer = null
  }
}

function startTicking() {
  stopTicking()
  tickTimer = setInterval(() => {
    remaining.value -= 1
    if (remaining.value <= 0)
      void poll()
  }, 1000)
}

async function poll() {
  try {
    const s = await fetchBanStatus()
    if (s && s.banned && s.remaining > 0) {
      // 以服务端剩余时间为准（纠回本地计时漂移）
      if (!banned.value || Math.abs(s.remaining - remaining.value) > 3)
        remaining.value = s.remaining
      reason.value = s.reason
      banned.value = true
      startTicking()
    }
    else {
      if (banned.value)
        showMsg('封禁已解除，欢迎回来。')
      banned.value = false
      stopTicking()
    }
  }
  catch {
    /* 状态查询失败保持静默，绝不干扰正常使用 */
  }
}

function onBanRefresh() {
  void poll()
}

onMounted(() => {
  void poll()
  pollTimer = setInterval(poll, POLL_MS)
  window.addEventListener('xq:ban-refresh', onBanRefresh)
})

onBeforeUnmount(() => {
  if (pollTimer)
    clearInterval(pollTimer)
  stopTicking()
  window.removeEventListener('xq:ban-refresh', onBanRefresh)
})
</script>

<template>
  <Teleport to="body">
    <div v-if="banned" class="ban-pill xq-teleport" role="status">
      <span class="ban-dot" aria-hidden="true" />
      <span class="ban-text">
        你已被封禁，<span class="ban-count" aria-hidden="true">{{ fmt(remaining) }}</span>后解封，期间无法重新登录
      </span>
      <span v-if="reason" class="ban-reason">原因：{{ reason }}</span>
      <span class="sr-only">账号已封禁，期间无法重新登录，请等待解封</span>
    </div>
  </Teleport>
</template>

<style scoped>
.ban-pill {
  position: fixed;
  top: calc(10px + env(safe-area-inset-top, 0px));
  left: 50%;
  transform: translateX(-50%);
  z-index: 2500;   /* 高于站点顶栏(2100)/底栏(2000)，低于小秋弹窗(2600) */
  display: flex;
  align-items: center;
  gap: 8px;
  max-width: min(92vw, 520px);
  padding: 7px 14px;
  border-radius: 999px;
  border: 1px solid rgba(239, 83, 80, 0.26);
  background: color-mix(in srgb, var(--xq-surface) 78%, transparent);
  backdrop-filter: blur(8px);
  box-shadow: 0 2px 12px rgba(15, 23, 42, 0.08);
  color: var(--xq-text-secondary);
  font-size: 12px;
  line-height: 1.4;
  pointer-events: none;   /* 纯状态提示，绝不挡点击 */
  animation: ban-fade-in 0.5s ease;
}

.ban-dot {
  flex: none;
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: #ef5350;
  opacity: 0.75;
  animation: ban-pulse 2.4s ease-in-out infinite;
}

.ban-text {
  overflow: hidden;
  white-space: nowrap;
  text-overflow: ellipsis;
}

.ban-count {
  color: #e53935;
  font-weight: 600;
  font-variant-numeric: tabular-nums;
  margin: 0 2px;
}

.ban-reason {
  flex: none;
  color: var(--xq-text-muted);
}

.sr-only {
  position: absolute;
  width: 1px;
  height: 1px;
  overflow: hidden;
  clip: rect(0 0 0 0);
  white-space: nowrap;
}

@keyframes ban-fade-in {
  from {
    opacity: 0;
    transform: translate(-50%, -6px);
  }
  to {
    opacity: 1;
    transform: translate(-50%, 0);
  }
}

@keyframes ban-pulse {
  0%,
  100% {
    opacity: 0.35;
  }

  50% {
    opacity: 0.85;
  }
}

@media (max-width: 600px) {
  .ban-reason {
    display: none;
  }
}

@media (prefers-reduced-motion: reduce) {
  .ban-pill {
    animation: none;
  }

  .ban-dot {
    animation: none;
  }
}
</style>
