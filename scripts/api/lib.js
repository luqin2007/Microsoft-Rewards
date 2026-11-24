import fs from 'node:fs'
import path from 'node:path'

export { envBool, envInt, envStr } from '../env.js'

export function log(level, ...args) {
    const lvl = typeof level === 'string' ? level.toUpperCase() : 'INFO'
    const line = `[api] [${lvl}]`
    if (lvl === 'ERROR') return console.error(line, ...args)
    if (lvl === 'WARN') return console.warn(line, ...args)
    return console.log(line, ...args)
}

export function parseArgs(argv = process.argv.slice(2), { boolean = [] } = {}) {
    const args = { _: [] }
    const booleanOptions = new Set(boolean)

    for (let i = 0; i < argv.length; i++) {
        const arg = argv[i]

        if (arg === '--') {
            args._.push(...argv.slice(i + 1))
            break
        }
        if (!arg.startsWith('-') || arg === '-') {
            args._.push(arg)
            continue
        }

        const option = arg.replace(/^-+/, '')
        const equalsIndex = option.indexOf('=')
        const key = equalsIndex === -1 ? option : option.slice(0, equalsIndex)

        if (!key) {
            args._.push(arg)
        } else if (equalsIndex !== -1) {
            args[key] = option.slice(equalsIndex + 1)
        } else if (booleanOptions.has(key)) {
            args[key] = true
        } else if (i + 1 < argv.length && !argv[i + 1].startsWith('-')) {
            args[key] = argv[++i]
        } else {
            args[key] = true
        }
    }
    return args
}

export function findUnknownOptions(args, allowedOptions) {
    const allowed = new Set(allowedOptions)
    return Object.keys(args).filter(key => key !== '_' && !allowed.has(key))
}

export function getProjectRoot(startDir) {
    let dir = startDir
    let nearestPackageDir = null
    while (dir !== path.parse(dir).root) {
        const packagePath = path.join(dir, 'package.json')
        if (fs.existsSync(packagePath)) {
            nearestPackageDir ??= dir
            try {
                const pkg = JSON.parse(fs.readFileSync(packagePath, 'utf8'))
                if (typeof pkg.name === 'string' && pkg.name.trim()) return dir
            } catch {}
        }
        dir = path.dirname(dir)
    }
    if (nearestPackageDir) return nearestPackageDir
    throw new Error('Could not locate project root (package.json not found).')
}

/**
 * 用户数据目录（默认 `%LOCALAPPDATA%\MRS助手`），与 src/util/UserData.ts 规则一致。
 * 上游把 .env / config.json / sessions 迁到这里，API 必须读同一份，否则和 node 进程
 * 看到的状态不一致（GUI 改完设置脚本读不到）。
 */
export function resolveDataDir() {
    const override = (process.env.MRS_DATA_DIR ?? '').trim()
    if (override) return path.resolve(override)
    const base =
        (process.env.LOCALAPPDATA ?? '').trim() ||
        path.join(process.env.USERPROFILE ?? process.cwd(), 'AppData', 'Local')
    return path.join(base, 'MRS助手')
}

export function loadEnvFile(projectRoot) {
    const candidates = [
        path.join(resolveDataDir(), '.env'),
        path.resolve(process.cwd(), '.env'),
        path.join(projectRoot, '.env'),
        path.join(projectRoot, 'dist', '.env')
    ]
    const envFile = candidates.find(p => fs.existsSync(p))
    if (!envFile) return null

    const raw = fs.readFileSync(envFile, 'utf8')
    for (const line of raw.split(/\r?\n/)) {
        const trimmed = line.trim()
        if (!trimmed || trimmed.startsWith('#')) continue
        const eq = trimmed.indexOf('=')
        if (eq === -1) continue
        const key = trimmed.slice(0, eq).trim()
        let value = trimmed.slice(eq + 1).trim()
        if ((value.startsWith('"') && value.endsWith('"')) || (value.startsWith("'") && value.endsWith("'"))) {
            value = value.slice(1, -1)
        }
        if (process.env[key] === undefined) process.env[key] = value
    }
    return envFile
}

function readJsonFile(filePath) {
    // PowerShell 5.1 的 Set-Content -Encoding UTF8、Windows 记事本另存 UTF-8 都会写 BOM，
    // JSON.parse 遇到 BOM 直接抛 —— 这里先剥掉，否则「配置被静默忽略」很难排查。
    let text = fs.readFileSync(filePath, 'utf8')
    if (text.charCodeAt(0) === 0xfeff) text = text.slice(1)
    return JSON.parse(text)
}

export function loadConfigSafe(projectRoot) {
    const candidates = [
        path.join(resolveDataDir(), 'config.json'),
        path.join(projectRoot, 'config.json'),
        path.join(projectRoot, 'dist', 'config.json'),
        path.join(projectRoot, 'src', 'config.json'),
        path.resolve(process.cwd(), 'config.json')
    ]
    for (const p of candidates) {
        if (!fs.existsSync(p)) continue
        try {
            return { data: readJsonFile(p), path: p }
        } catch (error) {
            return { data: null, path: p, error: error instanceof Error ? error.message : String(error) }
        }
    }
    return null
}

export function redactSecrets(config) {
    if (!config || typeof config !== 'object') return config
    const clone = structuredClone(config)
    const mask = '***REDACTED***'
    const wh = clone.webhook
    if (wh && typeof wh === 'object') {
        if (wh.discord?.url) wh.discord.url = mask
        if (wh.ntfy?.url) wh.ntfy.url = mask
        if (wh.ntfy?.token) wh.ntfy.token = mask
        if (wh.telegram?.botToken) wh.telegram.botToken = mask
        if (wh.telegram?.chatId) wh.telegram.chatId = mask
    }
    return clone
}
