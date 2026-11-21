const test = require('node:test')
const assert = require('node:assert/strict')
const { escapeJson, renderGenericBody } = require('../../dist/logging/Generic.js')

test('escapeJson 转义 " \\ 换行 回车 制表符', () => {
    assert.equal(escapeJson('a"b'), 'a\\"b')
    assert.equal(escapeJson('a\\b'), 'a\\\\b')
    assert.equal(escapeJson('a\nb'), 'a\\nb')
    assert.equal(escapeJson('a\rb'), 'a\\rb')
    assert.equal(escapeJson('a\tb'), 'a\\tb')
})

test('默认模板产出合法 JSON，message 保真（含反斜杠与换行）', () => {
    const content = '第1行 "引号" C:\\Tools\n第2行\tTAB'
    const body = renderGenericBody({ enabled: true, url: 'http://x/push' }, content, 'info')
    const parsed = JSON.parse(body)
    assert.equal(parsed.message, content)
})

test('摘要风格的 Markdown 内容不破坏 JSON', () => {
    const content = '每日积分摘要\n状态: 完成\n账户: a@b.com | +51 | 15593→15644\n第二行 "q"'
    const parsed = JSON.parse(renderGenericBody({ enabled: true, url: 'http://x' }, content, 'info'))
    assert.equal(parsed.message, content)
})

test('自定义模板支持 {level} 与 {timestamp} 占位符', () => {
    const body = renderGenericBody(
        { enabled: true, url: 'http://x', template: '{"text":"{message}","level":"{level}","at":"{timestamp}"}' },
        'hello',
        'error'
    )
    const parsed = JSON.parse(body)
    assert.equal(parsed.text, 'hello')
    assert.equal(parsed.level, 'error')
    assert.match(parsed.at, /^\d{4}-\d{2}-\d{2}T/)
})

test('未提供 template 时回落到默认模板', () => {
    const parsed = JSON.parse(renderGenericBody({ enabled: true, url: 'http://x' }, 'm', 'warn'))
    assert.deepEqual(Object.keys(parsed), ['message'])
    assert.equal(parsed.message, 'm')
})

test('escapeJsonOrder 正确：先转义反斜杠再转义引号（不重复转义）', () => {
    // 输入 \" 应变成 \\\" —— 反斜杠一次、引号一次
    assert.equal(escapeJson('\\"'), '\\\\\\"')
})