<script setup lang="ts">
import type { MemoryItem } from '@/api/opsagent/memory'
import { onBeforeUnmount, onMounted, ref, useTemplateRef } from 'vue'
import { deleteMemory, fetchMemories, updateMemory } from '@/api/opsagent/memory'
import { showMsg } from '@/components/MessageBox'

const emit = defineEmits<{
  (e: 'close'): void
}>()

const items = ref<MemoryItem[]>([])
const loading = ref(true)
const editingKey = ref('')
const editText = ref('')
const confirmingKey = ref('')
let confirmTimer: ReturnType<typeof setTimeout> | null = null

const drawerPanel = useTemplateRef('drawerPanel')
const closeButton = useTemplateRef('closeButton')

function trapFocus(e: KeyboardEvent) {
  if (e.key !== 'Tab' || !drawerPanel.value)
    return
  const focusables = drawerPanel.value.querySelectorAll<HTMLElement>(
    'button:not(:disabled), input, textarea, [tabindex]:not([tabindex="-1"])')
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
  closeButton.value?.focus()
  try {
    items.value = await fetchMemories()
  }
  catch (e) {
    console.error(e)
    showMsg('记忆加载失败，请稍后重试。')
  }
  finally {
    loading.value = false
  }
})

onBeforeUnmount(() => {
  document.removeEventListener('keydown', onKeydown)
  document.body.style.overflow = ''
})

async function remove(item: MemoryItem) {
  // 两击确认，避免误删
  if (confirmingKey.value !== item.key) {
    confirmingKey.value = item.key
    if (confirmTimer)
      clearTimeout(confirmTimer)
    confirmTimer = setTimeout(() => {
      confirmingKey.value = ''
    }, 2600)
    return
  }
  confirmingKey.value = ''
  try {
    await deleteMemory(item.key)
    items.value = items.value.filter(i => i.key !== item.key)
    showMsg('已删除该条记忆。')
  }
  catch (e) {
    console.error(e)
    showMsg('删除失败，请稍后重试。')
  }
}

function startEdit(item: MemoryItem) {
  editingKey.value = item.key
  editText.value = item.text
}

async function commitEdit() {
  const key = editingKey.value
  const text = editText.value.trim()
  editingKey.value = ''
  if (!text)
    return
  try {
    await updateMemory(key, text)
    await reload()
  }
  catch (e) {
    console.error(e)
    showMsg('修改失败，请稍后重试。')
  }
}

async function reload() {
  try {
    items.value = await fetchMemories()
  }
  catch {
    /* 刷新失败保持现状 */
  }
}

function timeLabel(iso: string) {
  const d = new Date(iso)
  return Number.isNaN(d.getTime()) ? '' : `${d.getMonth() + 1}/${d.getDate()}`
}
</script>

<template>
  <Teleport to="body">
    <div class="drawer-mask xq-teleport" @click="emit('close')">
    <div
      ref="drawerPanel"
      class="memory-drawer"
      role="dialog"
      aria-modal="true"
      aria-label="小秋的记忆"
      @click.stop
    >
      <header class="drawer-head">
        <div>
          <h3>小秋记得我什么</h3>
          <p>对话中让小秋"记住"的内容都在这里，可随时修改或删除</p>
        </div>
        <button
          ref="closeButton"
          type="button"
          class="close-btn"
          aria-label="关闭"
          @click="emit('close')"
        >
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" aria-hidden="true">
            <path d="M18 6L6 18M6 6l12 12" stroke="currentColor" stroke-width="2" stroke-linecap="round" />
          </svg>
        </button>
      </header>

      <div class="drawer-body lite-scrollbar">
        <p v-if="loading" class="hint" aria-live="polite">
          加载中…
        </p>
        <p v-else-if="items.length === 0" class="hint">
          小秋还没有记住任何内容。对话时说"记住我喜欢…"，它就会开始了解你。
        </p>
        <div v-for="item in items" :key="item.key" class="mem-item">
          <template v-if="editingKey === item.key">
            <textarea
              v-model="editText"
              class="mem-edit"
              name="memory-text"
              rows="3"
              maxlength="500"
              autocomplete="off"
              aria-label="编辑记忆内容"
              @keydown.ctrl.enter.prevent="commitEdit"
            />
            <div class="mem-edit-actions">
              <button type="button" class="mem-btn primary" @click="commitEdit">
                保存
              </button>
              <button type="button" class="mem-btn" @click="editingKey = ''">
                取消
              </button>
              <span class="mem-edit-hint">Ctrl+Enter 保存</span>
            </div>
          </template>
          <template v-else>
            <p class="mem-text">
              {{ item.text }}
            </p>
            <div class="mem-foot">
              <time>{{ timeLabel(item.updated_at) }}</time>
              <span class="mem-actions">
                <button type="button" class="mem-btn" @click="startEdit(item)">
                  编辑
                </button>
                <button
                  type="button"
                  class="mem-btn danger"
                  :class="{ confirming: confirmingKey === item.key }"
                  @click="remove(item)"
                >
                  {{ confirmingKey === item.key ? '确认删除' : '删除' }}
                </button>
              </span>
            </div>
          </template>
        </div>
      </div>
    </div>
  </div>
  </Teleport>
</template>

<style scoped>
.drawer-mask {
  position: fixed;
  inset: 0;
  z-index: 2600;   /* 高于站点 MobileHeader(2100)/BottomNavbar(2000)，抽屉必须盖住全屏 */
  display: flex;
  justify-content: flex-end;
  background: rgba(15, 23, 42, 0.5);
  backdrop-filter: blur(2px);
}

.memory-drawer {
  display: flex;
  flex-direction: column;
  width: min(420px, 92vw);
  height: 100%;
  background: var(--xq-surface);
  box-shadow: -8px 0 32px rgba(15, 23, 42, 0.18);
  animation: slide-in 0.22s ease;
}

.memory-drawer button {
  touch-action: manipulation;
  -webkit-tap-highlight-color: transparent;
}

.memory-drawer button svg {
  flex: none;
}

.close-btn svg {
  width: 18px !important;
  height: 18px !important;
}

.memory-drawer :focus-visible {
  outline: 2px solid var(--xq-accent);
  outline-offset: 2px;
  border-radius: 6px;
}

@keyframes slide-in {
  from {
    transform: translateX(30px);
    opacity: 0.6;
  }
  to {
    transform: translateX(0);
    opacity: 1;
  }
}

.drawer-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 10px;
  padding: 18px 18px 14px;
  border-bottom: 1px solid var(--xq-border);
}

.drawer-head h3 {
  margin: 0;
  font-size: 16px;
  font-weight: 650;
  color: var(--xq-text);
}

.drawer-head p {
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

.drawer-body {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  overscroll-behavior: contain;
  padding: 14px 18px 20px;
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.hint {
  margin: 20px 0;
  font-size: 13.5px;
  line-height: 1.7;
  color: var(--xq-text-muted);
  text-align: center;
}

.mem-item {
  padding: 12px 14px;
  border: 1px solid var(--xq-border);
  border-radius: 12px;
  background: var(--xq-surface-muted);
}

.mem-text {
  margin: 0;
  font-size: 13.5px;
  line-height: 1.6;
  color: var(--xq-text);
  word-break: break-word;
  white-space: pre-wrap;
}

.mem-foot {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-top: 8px;
}

.mem-foot time {
  font-size: 11px;
  color: var(--xq-text-muted);
  font-variant-numeric: tabular-nums;
}

.mem-actions {
  display: inline-flex;
  gap: 6px;
}

.mem-btn {
  padding: 3px 10px;
  border: 1px solid var(--xq-border);
  border-radius: 999px;
  background: var(--xq-surface);
  color: var(--xq-text-secondary);
  font-size: 11.5px;
  cursor: pointer;
  transition:
    color 0.12s ease,
    border-color 0.12s ease;
}

.mem-btn:hover {
  color: var(--xq-accent-deep);
  border-color: var(--xq-accent-border);
}

.mem-btn.danger:hover {
  color: #ef5350;
  border-color: rgba(239, 83, 80, 0.4);
}

.mem-btn.danger.confirming {
  color: #ef5350;
  border-color: rgba(239, 83, 80, 0.5);
  background: rgba(239, 83, 80, 0.1);
  font-weight: 600;
}

.mem-btn.primary {
  background: var(--xq-accent);
  border-color: var(--xq-accent);
  color: #fff;
}

.mem-btn.primary:hover {
  color: #fff;
  background: var(--xq-accent-deep);
}

.mem-edit {
  width: 100%;
  padding: 8px 10px;
  border: 1px solid var(--xq-accent-border);
  border-radius: 8px;
  outline: none;
  resize: vertical;
  background: var(--xq-surface);
  color: var(--xq-text);
  font: inherit;
  font-size: 13px;
  line-height: 1.5;
}

.mem-edit:focus {
  border-color: var(--xq-accent);
  box-shadow: 0 0 0 3px var(--xq-accent-ring);
}

.mem-edit-actions {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-top: 8px;
}

.mem-edit-hint {
  margin-left: auto;
  font-size: 10.5px;
  color: var(--xq-text-muted);
}

.lite-scrollbar::-webkit-scrollbar {
  width: 4px;
}

.lite-scrollbar::-webkit-scrollbar-thumb {
  background: rgba(15, 23, 42, 0.14);
  border-radius: 999px;
}

@media (max-width: 768px) {
  .memory-drawer {
    width: 100vw;
  }
}

@media (prefers-reduced-motion: reduce) {
  .memory-drawer {
    animation: none;
  }

  .memory-drawer *,
  .memory-drawer *::before,
  .memory-drawer *::after {
    transition-duration: 0.01ms !important;
  }
}
</style>
