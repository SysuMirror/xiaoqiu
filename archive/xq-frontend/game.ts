import { opsagentBaseURL } from '@/api/opsagent/baseUrl'
import { opsagentAuthHeaders } from '@/api/opsagent/headers'
import { ensureAccessToken } from '@/api/req'
import { OpsAgentRequestError, parseOpsAgentJsonError } from '@/api/opsagent/errors'

export type RpsMove = 'rock' | 'paper' | 'scissors'
export type RpsOutcome = 'win' | 'lose' | 'draw'

export interface RpsBan {
  banned: boolean
  seconds: number
  until: string
}

export type RpsSentenceMode = 'commute' | 'extend' | 'none'

export interface RpsSentence {
  mode: RpsSentenceMode
  changed_seconds: number
  remaining_after: number
}

export interface RpsPlayResult {
  user_move: RpsMove
  ai_move: RpsMove
  result: RpsOutcome
  ban: RpsBan | null
  note: string
  /** 减刑局结算（封禁期内出局才有）；none = 平局刑期不变 */
  sentence: RpsSentence | null
  /** 本局结束后的剩余刑期（秒），未封禁为 0 */
  remaining_after: number
}

export const RPS_MOVE_CN: Record<RpsMove, string> = { rock: '石头', scissors: '剪刀', paper: '布' }

/** POST /api/v1/agent/game/rps —— 猜拳一局。赢了没奖励，输了封号 5 分钟。 */
export async function playRps(move: RpsMove): Promise<RpsPlayResult> {
  if (!(await ensureAccessToken()))
    throw new Error('请先登录后再玩')
  const res = await fetch(`${opsagentBaseURL()}/api/v1/agent/game/rps`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Accept: 'application/json', ...opsagentAuthHeaders() },
    body: JSON.stringify({ move }),
  })
  const text = await res.text()
  if (!res.ok) {
    const parsed = parseOpsAgentJsonError(text)
    throw new OpsAgentRequestError(res.status, parsed.msg, parsed.code)
  }
  try {
    const body = JSON.parse(text) as { code?: number, data?: RpsPlayResult }
    if (body.data)
      return body.data
    throw new Error('响应缺少 data')
  }
  catch (e) {
    if (e instanceof OpsAgentRequestError)
      throw e
    throw new Error('猜拳响应解析失败')
  }
}

export interface RpsStats {
  me: { wins: number, losses: number, draws: number, bans: number }
  global: { games: number, bans: number }
  recent: { user_move: RpsMove, ai_move: RpsMove, result: RpsOutcome, banned: boolean, created_at: string }[]
}

/** GET /api/v1/agent/game/rps/stats —— 个人战绩与全局统计。 */
export async function fetchRpsStats(): Promise<RpsStats | null> {
  if (!(await ensureAccessToken()))
    return null
  const res = await fetch(`${opsagentBaseURL()}/api/v1/agent/game/rps/stats`, {
    method: 'GET',
    headers: { Accept: 'application/json', ...opsagentAuthHeaders() },
  })
  if (!res.ok)
    return null
  try {
    const body = await res.json() as { data?: RpsStats }
    return body.data ?? null
  }
  catch {
    return null
  }
}
