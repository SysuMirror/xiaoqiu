import { opsagentBaseURL } from '@/api/opsagent/baseUrl'
import { opsagentAuthHeaders } from '@/api/opsagent/headers'
import { ensureAccessToken } from '@/api/req'

export interface BanStatus {
  banned: boolean
  remaining: number
  until: string | null
  reason: string
}

/** GET /api/v1/agent/me/ban —— 账号封禁状态（辱骂禁言 + 猜拳封禁共用，banend 为事实源）。 */
export async function fetchBanStatus(): Promise<BanStatus | null> {
  if (!(await ensureAccessToken()))
    return null
  const res = await fetch(`${opsagentBaseURL()}/api/v1/agent/me/ban`, {
    method: 'GET',
    headers: { Accept: 'application/json', ...opsagentAuthHeaders() },
  })
  if (!res.ok)
    return null
  try {
    const body = await res.json() as { data?: BanStatus }
    return body.data ?? null
  }
  catch {
    return null
  }
}
