import { createApp } from 'vue'

import App from './App.vue'
import router from './router'
import { clearStaleNewSSEServiceWorker } from './utils/clearStaleServiceWorker'
import './assets/main.css'
import './assets/xq-overlay.css'
import './assets/mode.css'
import './assets/comment-ui.scss'
import './assets/post-feed.scss'
import './assets/rating-hupu.scss'
import './assets/shop-zone.scss'
import 'virtual:uno.css'

void clearStaleNewSSEServiceWorker()

// 主题引导：同步应用本地主题（ModeButton 移入异步头部组件后，此处保证首帧前生效，避免闪白）
document.body.classList.toggle('dark-mode', (localStorage.getItem('mode') || '') === 'dark-mode')

const app = createApp(App)
app.use(router)

app.mount('#app')
