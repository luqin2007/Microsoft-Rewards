// 用真实 sendGeneric 模块向 mypanel 推送一条消息，验证「代码 → mypanel → 手机」全链路
const { sendGeneric, flushGenericQueue, renderGenericBody } = require('../../dist/logging/Generic.js')

const url = process.argv[2] || 'http://my.luqion.cn/notify/push'
const message =
    process.argv[3] ||
    '# MRS 助手 · 通用 Webhook 测试\n\n' +
        '通道：webhook.generic\n' +
        '目标：`/notify/push`\n' +
        '状态：**打通**\n\n' +
        '包含 "引号"、反斜杠 C:\\Tools\\、换行\n都应在手机端正确显示。'

;(async () => {
    const cfg = { enabled: true, url, method: 'POST', template: '{"message":"{message}"}' }
    console.log('body =', renderGenericBody(cfg, message, 'info'))
    await sendGeneric(cfg, message, 'info')
    await flushGenericQueue()
    console.log('done (sent)')
})()