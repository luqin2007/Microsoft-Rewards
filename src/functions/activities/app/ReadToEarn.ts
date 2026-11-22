import { URLs } from '../../../constants/urls'
import { BING_APP_USER_AGENT } from '../../../constants/userAgents'
import type { HttpRequestConfig } from '../../../util/Http'
import { getReadToEarnArticleLimit } from '../../../util/Humanize'
import { randomBytes } from 'crypto'
import { BaseActivity } from '../BaseActivity'

export class ReadToEarn extends BaseActivity {
    public async doReadToEarn() {
        if (!this.bot.accessToken) {
            this.bot.logger.warn(
                this.bot.isMobile,
                'READ-TO-EARN',
                '跳过：应用访问令牌不可用，此活动需要它！'
            )
            return
        }

        const delayMin = this.bot.config.searchSettings.readDelay.min
        const delayMax = this.bot.config.searchSettings.readDelay.max
        const startBalance = Number(this.bot.userData.currentPoints ?? 0)

        this.bot.logger.info(
            this.bot.isMobile,
            'READ-TO-EARN',
            `开始读文赚积分 | geo=${this.bot.userData.geoLocale} | delayRange=${delayMin}-${delayMax} | currentBalance=${startBalance}`
        )

        try {
            const jsonData = {
                amount: 1,
                id: '1',
                type: 101,
                attributes: {
                    offerid: 'ENUS_readarticle3_30points'
                },
                country: this.bot.userData.geoLocale
            }

            const articleCount = getReadToEarnArticleLimit(10)
            let totalGained = 0
            // articlesRead：真正拿到积分的篇数；attempts：提交过的次数（含不发分的）。
            // 微软判定看的是行为次数，所以汇总里两个数都报出来，便于排障。
            let articlesRead = 0
            let attempts = 0
            let oldBalance = startBalance

            for (let i = 0; i < articleCount; ++i) {
                attempts = i + 1
                jsonData.id = randomBytes(64).toString('hex')

                this.bot.logger.debug(
                    this.bot.isMobile,
                    'READ-TO-EARN',
                    `正在提交读文赚积分活动 | article=${i + 1}/${articleCount} | id=${jsonData.id} | country=${jsonData.country}`
                )

                const request: HttpRequestConfig = {
                    url: URLs.platform.activities,
                    method: 'POST',
                    headers: {
                        Authorization: `Bearer ${this.bot.accessToken}`,
                        'User-Agent': BING_APP_USER_AGENT,
                        'Content-Type': 'application/json',
                        'X-Rewards-Country': this.bot.userData.geoLocale,
                        'X-Rewards-Language': this.bot.userData.langCode,
                        'X-Rewards-ismobile': 'true'
                    },
                    data: JSON.stringify(jsonData)
                }

                const response = await this.bot.http.request<{ response?: { balance?: number } }>(request)

                this.bot.logger.debug(
                    this.bot.isMobile,
                    'READ-TO-EARN',
                    `已收到读文赚积分响应 | article=${i + 1}/${articleCount} | status=${response?.status ?? 'unknown'}`
                )

                const newBalance = Number(response?.data?.response?.balance ?? oldBalance)
                const gainedPoints = newBalance - oldBalance

                this.bot.logger.debug(
                    this.bot.isMobile,
                    'READ-TO-EARN',
                    `文章后的积分变化 | article=${i + 1}/${articleCount} | previousBalance=${oldBalance} | currentBalance=${newBalance} | pointsGained=${gainedPoints}`
                )

                if (gainedPoints <= 0) {
                    // 2026-11-18：**不再退出循环**。
                    // 微软的读文任务判定的是「完成 N 次阅读行为」，不是「拿到 N 次积分」。
                    // 遇到某个 offer 当天已领满（或偶发不发分）就 break，会让行为次数停在
                    // 9 次这种尴尬数字，任务在微软那边显示未完成。所以这里只记日志、
                    // 继续把剩余次数跑完。
                    this.bot.logger.info(
                        this.bot.isMobile,
                        'READ-TO-EARN',
                        `本次未获得积分（继续执行剩余次数）| article=${i + 1}/${articleCount} | status=${response.status} | currentBalance=${newBalance} | 已完成次数=${articlesRead}`
                    )
                    this.bot.userData.currentPoints = newBalance
                    oldBalance = newBalance
                } else {
                    this.bot.userData.currentPoints = newBalance
                    this.bot.userData.gainedPoints = (this.bot.userData.gainedPoints ?? 0) + gainedPoints
                    totalGained += gainedPoints
                    articlesRead = i + 1
                    oldBalance = newBalance

                    this.bot.logger.info(
                        this.bot.isMobile,
                        'READ-TO-EARN',
                        `已阅读第 ${i + 1}/${articleCount} 篇文章 | status=${response.status} | pointsGained=${gainedPoints} | currentBalance=${newBalance}`,
                        'green'
                    )
                }

                this.bot.logger.debug(
                    this.bot.isMobile,
                    'READ-TO-EARN',
                    `文章间等待 | article=${i + 1}/${articleCount} | delayRange=${delayMin}-${delayMax}`
                )

                if (i < articleCount - 1) {
                    await this.bot.utils.wait(this.bot.utils.randomDelay(delayMin, delayMax))
                }
            }

            const finalBalance = Number(this.bot.userData.currentPoints ?? startBalance)

            this.bot.logger.info(
                this.bot.isMobile,
                'READ-TO-EARN',
                `读文赚积分完成 | 提交次数=${attempts}/${articleCount} | 得分篇数=${articlesRead} | pointsGained=${totalGained} | previousBalance=${startBalance} | currentBalance=${finalBalance}`
            )
        } catch (error) {
            this.bot.logger.error(
                this.bot.isMobile,
                'READ-TO-EARN',
                `读文赚积分出错 | message=${error instanceof Error ? error.message : String(error)}`
            )
        }
    }
}
