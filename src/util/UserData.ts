import fs from 'node:fs';
import path from 'node:path';

/**
 * 用户数据目录解析 —— 与 GUI 侧 `gui/user_data.py` 保持完全一致。
 *
 * 用户的账号密码（.env）、配置（config.json）、登录 Cookie（sessions）住在
 * `%LOCALAPPDATA%\MRS助手\`，不跟着解压目录走。用户换新压缩包之后，这些
 * 东西还在，不用重新输账号密码、多数情况连登录都不用走。
 *
 * 读取优先级（与 Python 侧一致，否则两个进程会读到不同文件）：
 *   1. `MRS_DATA_DIR` 环境变量
 *   2. `%LOCALAPPDATA%\MRS助手\`
 *   3. 解压目录（老用户升级前的旧位置，找到就复制过来）
 *
 * 迁移策略：复制，不移动。旧文件留在解压目录，用户可回退旧版本。
 */
const APP_DIR_NAME = 'MRS助手';

export const DATA_FILES = ['.env', 'config.json', 'clawbot-auth.json'] as const
export const DATA_DIRS = ['sessions'] as const
export type DataItem = (typeof DATA_FILES)[number] | (typeof DATA_DIRS)[number]

/** 解压目录 / 源码树根（dist 的上一级，或 exe 所在目录）。 */
export function appRoot(): string {
    const cwd = process.cwd();
    if (fs.existsSync(path.join(cwd, 'package.json'))) return cwd;
    // dist/util/ -> dist -> root
    const fromDist = path.resolve(__dirname, '..', '..');
    if (fs.existsSync(path.join(fromDist, 'package.json'))) return fromDist;
    return cwd;
}

/** 用户数据目录的绝对路径（不保证已存在）。 */
export function dataRoot(): string {
    const override = (process.env.MRS_DATA_DIR ?? '').trim();
    if (override) return path.resolve(override);
    let base = (process.env.LOCALAPPDATA ?? '').trim();
    if (!base) {
        base = path.join(process.env.USERPROFILE ?? process.cwd(), 'AppData', 'Local');
    }
    return path.join(base, APP_DIR_NAME);
}

/** 创建数据目录并返回路径。 */
export function ensureDataRoot(): string {
    const root = dataRoot();
    try {
        fs.mkdirSync(root, { recursive: true });
    } catch {
        /* 权限不足时留给后续读写暴露问题 */
    }
    return root;
}

/** 把 src 里缺的条目复制到 dst（已存在的不覆盖）。返回是否复制过。 */
function copyMissing(src: string, dst: string): boolean {
    let copied = false;
    try {
        const st = fs.statSync(src);
        if (st.isDirectory()) {
            fs.mkdirSync(dst, { recursive: true });
            for (const name of fs.readdirSync(src)) {
                const s = path.join(src, name);
                const d = path.join(dst, name);
                if (fs.existsSync(d)) continue;
                if (copyMissing(s, d)) copied = true;
            }
            copied = true;
        } else if (!fs.existsSync(dst)) {
            fs.mkdirSync(path.dirname(dst), { recursive: true });
            fs.copyFileSync(src, dst);
            copied = true;
        }
    } catch {
        /* 复制失败不影响主流程：调用方会退回旧路径 */
    }
    return copied;
}

/**
 * 某项数据文件实际该用的路径。
 *
 * 规则（2026-11-18 定稿，用户要求「优先以程序目录下的 .env 为主」）：
 *   - 程序目录没有 → 用用户数据目录那份（没有就先建路径）
 *   - 程序目录有、用户目录没有 → 复制过来
 *   - 两边都有 → 比较修改时间，程序目录那份更新就覆盖用户目录
 *     （用户习惯把新版 .env / sessions 拷进解压目录，那份必须生效；
 *      用 mtime 判断可避免目录里的旧文件把 GUI 刚改的账号盖回去）
 *
 * 返回的永远是用户数据目录的路径——往后读写都落在那儿。
 */
export function resolveDataItem(name: DataItem): string {
    const dst = path.join(ensureDataRoot(), name);
    const src = path.join(appRoot(), name);
    if (path.resolve(src) === path.resolve(dst)) return dst;
    if (!fs.existsSync(src)) return dst;
    if (!fs.existsSync(dst)) {
        copyMissing(src, dst);
        return dst;
    }
    try {
        const newer = fs.statSync(src).isDirectory()
            ? dirMtime(src) > dirMtime(dst) + 1
            : fs.statSync(src).mtimeMs > fs.statSync(dst).mtimeMs + 1000;
        if (newer) replaceItem(src, dst);
    } catch {
        /* 比不出时间就保持现状，别把好数据弄坏 */
    }
    return dst;
}

/** 目录里最新文件的时间（只看一层，够判断 sessions.db 是否更新过）。 */
function dirMtime(dir: string): number {
    let newest = 0;
    try {
        for (const name of fs.readdirSync(dir)) {
            try {
                newest = Math.max(newest, fs.statSync(path.join(dir, name)).mtimeMs);
            } catch {
                /* 单个文件读不到就跳过 */
            }
        }
    } catch {
        return 0;
    }
    return newest;
}

/** 用 src 覆盖 dst（目录先清空再复制）。失败不抛，保持旧数据可用。 */
function replaceItem(src: string, dst: string): void {
    try {
        if (fs.statSync(src).isDirectory()) {
            fs.rmSync(dst, { recursive: true, force: true });
            copyMissing(src, dst);
        } else {
            fs.mkdirSync(path.dirname(dst), { recursive: true });
            fs.copyFileSync(src, dst);
        }
    } catch {
        /* 覆盖失败就用原来的 */
    }
}

/** sessions 数据库所在目录的绝对路径。 */
export function sessionsDir(sessionPath: string): string {
    // 配置里写的是相对路径（默认 'sessions'）。绝对路径原样尊重。
    if (path.isAbsolute(sessionPath)) return sessionPath;
    const resolved = resolveDataItem('sessions');
    // 默认名就用解析结果；配置改成别的名字时挂在数据目录下
    if (sessionPath === 'sessions') return resolved;
    return path.join(path.dirname(resolved), sessionPath);
}