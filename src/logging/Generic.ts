import { httpRequest } from '../util/Http'
import type { HttpRequestConfig } from '../util/Http'
import PQueue from 'p-queue'
import type { WebhookGenericConfig } from '../interface/Config'
import type { LogLevel } from './Logger'
import { flushQueue } from './Queue'

const genericQueue = new PQueue({
    interval: 1000,
    intervalCap: 2,
    carryoverConcurrencyCount: true
})

/**
 * 默认请求体模板：向任意端点 POST 一个 {"message": "..."} 对象。
 * 仅替换 {message} 需要转义；{level} / {timestamp} 由代码注入，不含特殊字符。
 */
const DEFAULT_TEMPLATE = '{"message":"{message}"}'

/**
 * 把任意文本安全地放进 JSON 字符串字面量内部。
 * 摘要消息带换行，不转义会直接破坏请求体。
 */
export function escapeJson(value: string): string {
    return value
        .replace(/\\/g, '\\\\')
        .replace(/"/g, '\\"')
        .replace(/\n/g, '\\n')
        .replace(/\r/g, '\\r')
        .replace(/\t/g, '\\t')
}

/**
 * 按配置模板渲染请求体，返回可直接发送的字符串。
 * 支持占位符：{message} / {level} / {timestamp}
 */
export function renderGenericBody(config: WebhookGenericConfig, content: string, level: LogLevel): string {
    return (config.template || DEFAULT_TEMPLATE)
        .replace(/\{message\}/g, escapeJson(content))
        .replace(/\{level\}/g, level)
        .replace(/\{timestamp\}/g, new Date().toISOString())
}

export async function sendGeneric(config: WebhookGenericConfig, content: string, level: LogLevel): Promise<void> {
    if (!config?.enabled || !config.url) return

    const headers: Record<string, string> = {
        'Content-Type': 'application/json',
        ...(config.headers ?? {})
    }
    if (config.token) headers['Authorization'] = `Bearer ${config.token}`

    const request: HttpRequestConfig = {
        method: config.method ?? 'POST',
        url: config.url,
        headers,
        data: renderGenericBody(config, content, level),
        timeout: 10000
    }

    await genericQueue.add(async () => {
        try {
            await httpRequest(request)
        } catch (err) {
            const status = (err as { response?: { status?: number } })?.response?.status
            // 429 限流与网络失败一律静默：通知不能影响主流程
            if (status !== 429 && process.argv.includes('-dev')) {
                console.warn(`[Generic] 推送失败 (${status ?? 'no-status'}): ${(err as Error)?.message ?? err}`)
            }
        }
    })
}

export function flushGenericQueue(timeoutMs = 5000): Promise<void> {
    return flushQueue(genericQueue, timeoutMs)
}