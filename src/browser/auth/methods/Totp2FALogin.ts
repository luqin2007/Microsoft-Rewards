import type { Page } from 'patchright'
import * as OTPAuth from 'otpauth'
import type { MicrosoftRewardsBot } from '../../../index'
import { canPromptForInput, getErrorMessage, promptInput, waitForManualEntryOnPage } from './LoginUtils'

export class TotpLogin {
    private readonly textInputSelector =
        'form[name="OneTimeCodeViewForm"] input[type="text"], input#floatingLabelInput5'
    private readonly secondairyInputSelector = 'input[id="otc-confirmation-input"], input[name="otc"]'
    private readonly submitButtonSelector = 'button[type="submit"]'
    private readonly maxManualSeconds = 60
    private readonly maxManualAttempts = 5
    /** 没配 TOTP 密钥时，在可见浏览器窗口里等用户手动输码的最长时间 */
    private readonly browserManualSeconds = 180

    constructor(private bot: MicrosoftRewardsBot) {}

    private generateTotpCode(secret: string): string {
        return new OTPAuth.TOTP({ secret, digits: 6 }).generate()
    }

    /**
     * 没有 TOTP 密钥时，等用户在看得见的浏览器窗口里自己输入 6 位验证码。
     * 判断依据：验证码输入框从页面上消失（说明已经提交并跳转，或用户换了别的验证方式）。
     * 仅在 headless=false 时可用；无头模式下窗口看不见，没人能输。
     */
    private async waitForBrowserManualInput(page: Page): Promise<void> {
        // 不是异常状态，用 INFO 提示即可（WARN 会让用户以为出错了）。
        this.bot.logger.info(
            this.bot.isMobile,
            'LOGIN-TOTP',
            `未配置 TOTP 密钥 —— 请在弹出的浏览器窗口里手动输入 6 位验证码，最长等 ${this.browserManualSeconds} 秒`
        )

        const outcome = await waitForManualEntryOnPage(page, this.bot.logger, this.bot.isMobile, {
            waitSelectors: [this.textInputSelector, this.secondairyInputSelector],
            timeoutSeconds: this.browserManualSeconds,
            tag: 'LOGIN-TOTP',
            message: '仍在等待浏览器窗口里的手动输入…'
        })

        if (outcome === 'page-closed') throw new Error('浏览器已关闭，无法完成人工验证')

        if (outcome === 'left-page') {
            const errorMessage = await getErrorMessage(page)
            if (errorMessage) {
                this.bot.logger.warn(this.bot.isMobile, 'LOGIN-TOTP', `页面提示错误（可能是验证码不对）：${errorMessage}`)
            }
            return
        }

        throw new Error(
            `等待手动输入验证码超时（${this.browserManualSeconds} 秒）。` +
                '可以在「账号管理」里填 TOTP 密钥，下次就能全自动通过。'
        )
    }

    private async fillCode(page: Page, code: string): Promise<boolean> {
        try {
            const visibleInput = await page
                .waitForSelector(this.textInputSelector, { state: 'visible', timeout: 500 })
                .catch(() => null)

            if (visibleInput) {
                await visibleInput.fill(code)
                this.bot.logger.info(this.bot.isMobile, 'LOGIN-TOTP', '已填写TOTP输入')
                return true
            }

            const secondairyInput = await page.$(this.secondairyInputSelector)
            if (secondairyInput) {
                await secondairyInput.fill(code)
                this.bot.logger.info(this.bot.isMobile, 'LOGIN-TOTP', '已填写TOTP输入')
                return true
            }

            this.bot.logger.warn(this.bot.isMobile, 'LOGIN-TOTP', '未找到TOTP输入字段')
            return false
        } catch (error) {
            this.bot.logger.warn(
                this.bot.isMobile,
                'LOGIN-TOTP',
                `填写TOTP输入失败: ${error instanceof Error ? error.message : String(error)}`
            )
            return false
        }
    }

    async handle(page: Page, totpSecret?: string): Promise<void> {
        try {
            this.bot.logger.info(this.bot.isMobile, 'LOGIN-TOTP', '请求TOTP双因素身份验证')

            if (totpSecret) {
                const code = this.generateTotpCode(totpSecret)
                this.bot.logger.info(this.bot.isMobile, 'LOGIN-TOTP', '从密钥生成TOTP代码')

                const filled = await this.fillCode(page, code)
                if (!filled) {
                    this.bot.logger.error(this.bot.isMobile, 'LOGIN-TOTP', '无法填写TOTP输入字段')
                    throw new Error('TOTP input field not found')
                }

                await this.bot.utils.wait(500)
                if (!(await this.bot.browser.utils.ghostClick(page, this.submitButtonSelector))) {
                    throw new Error('Unable to submit TOTP code')
                }
                await page.waitForLoadState('networkidle', { timeout: 5000 }).catch(() => {})

                const errorMessage = await getErrorMessage(page)
                if (errorMessage) {
                    this.bot.logger.error(this.bot.isMobile, 'LOGIN-TOTP', `TOTP失败: ${errorMessage}`)
                    throw new Error(`TOTP authentication failed: ${errorMessage}`)
                }

                this.bot.logger.info(this.bot.isMobile, 'LOGIN-TOTP', 'TOTP身份验证成功完成')
                return
            }

            if (!canPromptForInput()) {
                // GUI 拉起的进程没有终端，只能靠浏览器窗口：窗口可见就让用户自己输
                if (this.bot.config.headless) {
                    throw new Error(
                        '未配置 TOTP 密钥，且当前是无头模式（窗口不可见），无法人工输入验证码。' +
                            '请关掉无头模式重试，或在「账号管理」里填 TOTP 密钥。'
                    )
                }
                await this.waitForBrowserManualInput(page)
                return
            }

            this.bot.logger.info(this.bot.isMobile, 'LOGIN-TOTP', '未提供TOTP密钥，等待手动输入')

            for (let attempt = 1; attempt <= this.maxManualAttempts; attempt++) {
                const code = await promptInput({
                    question: `Enter the 6-digit TOTP code (waiting ${this.maxManualSeconds}s): `,
                    timeoutSeconds: this.maxManualSeconds,
                    validate: code => /^\d{6}$/.test(code)
                })

                if (!code || !/^\d{6}$/.test(code)) {
                    this.bot.logger.warn(
                        this.bot.isMobile,
                        'LOGIN-TOTP',
                        `无效或缺少代码 (尝试 ${attempt}/${this.maxManualAttempts}) | 输入长度=${code?.length}`
                    )

                    if (attempt === this.maxManualAttempts) {
                        throw new Error('Manual TOTP input failed or timed out')
                    }
                    continue
                }

                const filled = await this.fillCode(page, code)
                if (!filled) {
                    this.bot.logger.error(
                        this.bot.isMobile,
                        'LOGIN-TOTP',
                        `无法填写TOTP输入 (尝试 ${attempt}/${this.maxManualAttempts})`
                    )

                    if (attempt === this.maxManualAttempts) {
                        throw new Error('TOTP input field not found')
                    }
                    continue
                }

                await this.bot.utils.wait(500)
                if (!(await this.bot.browser.utils.ghostClick(page, this.submitButtonSelector))) {
                    throw new Error('Unable to submit TOTP code')
                }
                await page.waitForLoadState('networkidle', { timeout: 5000 }).catch(() => {})

                // Check if wrong code was entered
                const errorMessage = await getErrorMessage(page)
                if (errorMessage) {
                    this.bot.logger.warn(
                        this.bot.isMobile,
                        'LOGIN-TOTP',
                        `代码不正确: ${errorMessage} (尝试 ${attempt}/${this.maxManualAttempts})`
                    )

                    if (attempt === this.maxManualAttempts) {
                        throw new Error(`Maximum attempts reached: ${errorMessage}`)
                    }
                    continue
                }

                this.bot.logger.info(this.bot.isMobile, 'LOGIN-TOTP', 'TOTP身份验证成功完成')
                return
            }

            throw new Error(`TOTP input failed after ${this.maxManualAttempts} attempts`)
        } catch (error) {
            this.bot.logger.error(
                this.bot.isMobile,
                'LOGIN-TOTP',
                `发生错误: ${error instanceof Error ? error.message : String(error)}`
            )
            throw error
        }
    }
}
