import type { Page } from 'patchright'
import readline from 'readline'

export interface PromptOptions {
    question: string
    timeoutSeconds?: number
    validate?: (input: string) => boolean
    transform?: (input: string) => string
}

export function canPromptForInput(): boolean {
    return process.stdin.isTTY === true
}

export function promptInput(options: PromptOptions): Promise<string | null> {
    if (!canPromptForInput()) return Promise.resolve(null)

    const { question, timeoutSeconds = 60, validate, transform } = options

    return new Promise(resolve => {
        const rl = readline.createInterface({
            input: process.stdin,
            output: process.stdout
        })

        let resolved = false

        const cleanup = (result: string | null) => {
            if (resolved) return
            resolved = true
            clearTimeout(timer)
            rl.close()
            resolve(result)
        }

        const timer = setTimeout(() => cleanup(null), timeoutSeconds * 1000)

        rl.question(question, answer => {
            let value = answer.trim()
            if (transform) value = transform(value)

            if (validate && !validate(value)) {
                cleanup(null)
                return
            }

            cleanup(value)
        })
    })
}

export async function getSubtitleMessage(page: Page): Promise<string | null> {
    const message = await page
        .waitForSelector('[data-testid="subtitle"], div#oneTimeCodeDescription, [data-testid="description"]', {
            state: 'visible',
            timeout: 1000
        })
        .catch(() => null)

    if (!message) return null

    const text = await message.innerText()
    return text.trim()
}

export async function getErrorMessage(page: Page): Promise<string | null> {
    const errorAlert = await page
        .waitForSelector('div[role="alert"]', { state: 'visible', timeout: 1000 })
        .catch(() => null)

    if (!errorAlert) return null

    const text = await errorAlert.innerText()
    return text.trim()
}

/** 只需要 logger 的三个级别方法，避免为了类型引入整个 bot */
export interface MinimalLogger {
    info: (isMobile: boolean, tag: string, message: string) => void
    debug: (isMobile: boolean, tag: string, message: string) => void
}

export interface WaitForManualEntryOptions {
    /** 判定「还在等验证码」的选择器（任一存在即视为仍停留在此页） */
    waitSelectors: readonly string[]
    /** 判定「已跳转离开」的选择器（任一可见即立刻返回，避免把下一页误判成还在等） */
    doneSelectors?: readonly string[]
    /** 最长等待秒数 */
    timeoutSeconds: number
    /** 日志标签，例如 LOGIN-TOTP / LOGIN-OTP */
    tag: string
    /** 心跳日志里给用户看的说明文案 */
    message: string
    /** 轮询间隔（毫秒） */
    pollMs?: number
    /** 每多少次轮询输出一次「剩余 N 秒」 */
    heartbeatEvery?: number
}

export type ManualEntryOutcome = 'left-page' | 'timeout' | 'page-closed'

/**
 * 在可见的浏览器窗口里等用户自己完成输入（6 位验证码 / 2FA 等）。
 *
 * 背景：GUI 拉起的进程 `process.stdin.isTTY === false`，`promptInput()` 永远返回 null，
 * 过去这类页面在 GUI 下只能「放弃登录并立刻关窗口」——用户刚看到页面、正要输入，
 * 窗口就没了。这里按页面状态判断完成与否，与有没有终端无关。
 */
export async function waitForManualEntryOnPage(
    page: Page,
    logger: MinimalLogger,
    isMobile: boolean,
    options: WaitForManualEntryOptions
): Promise<ManualEntryOutcome> {
    const { waitSelectors, doneSelectors = [], timeoutSeconds, tag, message, pollMs = 2000, heartbeatEvery = 5 } = options
    const deadline = Date.now() + timeoutSeconds * 1000
    let tick = 0

    while (Date.now() < deadline) {
        if (page.isClosed()) {
            logger.debug(isMobile, tag, '等待手动输入期间页面已关闭')
            return 'page-closed'
        }

        let stillAsking = false
        let alreadyLeft = false
        try {
            for (const selector of doneSelectors) {
                if (await page.$(selector)) {
                    alreadyLeft = true
                    break
                }
            }
            if (!alreadyLeft) {
                for (const selector of waitSelectors) {
                    if (await page.$(selector)) {
                        stillAsking = true
                        break
                    }
                }
            }
        } catch {
            // 页面正在跳转，视为已离开
            alreadyLeft = true
        }

        if (alreadyLeft) {
            logger.info(isMobile, tag, '页面已跳转，继续后续流程')
            return 'left-page'
        }
        if (!stillAsking) {
            logger.info(isMobile, tag, '验证页面已通过，继续后续流程')
            return 'left-page'
        }

        const remaining = Math.max(0, Math.ceil((deadline - Date.now()) / 1000))
        if (++tick % heartbeatEvery === 1) {
            logger.info(isMobile, tag, `${message} | 剩余 ${remaining} 秒`)
        }
        await new Promise(resolve => setTimeout(resolve, pollMs))
    }

    return 'timeout'
}
