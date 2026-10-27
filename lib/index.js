/**
 * dsh-um-proxy-ondemand —— 讓 UM GenAI 代理「需要時才活著」。
 *
 * 問題
 * ----
 * DSH 的 `um-genai` provider 指向 http://127.0.0.1:8788/v1，而那個代理原本靠
 * 一個「登入時啟動 + 每 2 分鐘保活」的排程工作常駐，整天佔著 Python 行程與
 * headless Chrome，即使一整天都沒用到該模型。
 *
 * 做法
 * ----
 * `llm/stream` 是 DSH 的瀑布事件（waterfall）：
 *     'llm/stream'(options: GenerateOptions, next: () => AsyncIterable<StreamChunk>)
 * 且 `options.provider` 就是即將使用的 provider 名稱。於是：
 *
 *   - provider 不是 um-genai       → 立刻 `return next()`，完全不碰代理（零成本）。
 *   - provider 是 um-genai         → 回一個 async generator，第一次被拉取時先
 *                                     `ensureProxy()`（冷啟動並等到 /health 通過），
 *                                     再把 `next()` 的串流原封不動 `yield*` 出去。
 *
 * 為什麼要在 generator 裡才等待（而不是讓監聽器變成 async 函式）
 * -----------------------------------------------------------------
 * 瀑布事件的回傳值必須是 `AsyncIterable<StreamChunk>`。若把監聽器寫成 async
 * 函式就會回傳 Promise，是否被上游 await 屬於 DSH 內部實作細節，不該賭。
 * 回一個真正的 async generator，延遲只發生在第一次 `next()` 之前，語意等價且安全。
 *
 * 為什麼閒置關閉是必要而不是加分
 * ------------------------------
 * 「用量觸發喚醒」若沒有對應的關閉，代理只是從『開機常駐』變成『用過一次後永遠常駐』。
 * 所以每次請求都會重置一個 15 分鐘的計時器；歸零且沒有請求在跑時就 `stopProxy()`。
 * DSH 本身關閉時也會順手把代理關掉，避免留下孤兒行程。
 *
 * 只擁有這一件事：不註冊 provider、不改模型請求內容、不寫會話日誌。
 *
 * @module dsh-um-proxy-ondemand
 */

import {
  cfgOf,
  clearState,
  ensureProxy,
  flushState,
  health,
  inflightCount,
  readState,
  stopProxy,
  wakeRetryCount,
} from './proxy.js'

/** Cordis 插件名（同時是 loader 條目 id 與 profile insert 行的 name）。 */
export const name = 'dsh-um-proxy-ondemand'

/** 需要 webServer 來提供唯讀的狀態端點。 */
export const inject = ['webServer']

const STATUS_PATH = '/um-proxy-ondemand/state.json'

/** 這個 provider 才需要喚醒代理。 */
function isTargetProvider(options, cfg) {
  const provider = options?.provider
  if (typeof provider !== 'string' || provider === '') return false
  if (cfg.provider) return provider === cfg.provider
  return provider.toLowerCase().includes('um-genai')
}

/**
 * 掛載插件。
 * @param {object} ctx - 宿主 cordis 上下文。
 * @param {object} [config] - cordis.patch.yml 的 config（port / idleMinutes / provider…）。
 */
export function apply(ctx, config) {
  const cfg = cfgOf(config)
  const idleMinutes = Math.max(1, Number(cfg.idleMinutes ?? 15))
  if (!Number.isFinite(idleMinutes)) throw new TypeError('idleMinutes must be finite')
  // alwaysOn：常駐模式。不閒置關閉、宿主啟動時主動喚醒、宿主結束時也不把代理一起關掉，
  // 讓 8788 在使用者需要時永遠可用（看門狗 um-renew.py 每 5 分鐘補位）。
  // idleMinutes: 0 也視為常駐（比 1 分鐘更好懂）。
  const alwaysOn = cfg.alwaysOn === true || Number(cfg.idleMinutes) === 0
  // idleSeconds 是測試用的短週期覆寫：分鐘級的設定沒辦法在測試裡真的等 15 分鐘。
  const idleMs = cfg.idleSeconds != null ? Math.max(1, Number(cfg.idleSeconds)) * 1000 : idleMinutes * 60000
  if (!Number.isFinite(idleMs) || idleMs > 2147483647) throw new TypeError('invalid idle timeout')
  const idleLabel = alwaysOn ? 'always on' : cfg.idleSeconds ? `${Math.round(idleMs / 1000)}s` : `${idleMinutes} min`

  const warn = (what, err) => {
    try {
      ctx.logger?.warn?.(`${name}: ${what}: %s`, String((err && err.message) || err))
    } catch {
      /* 記錄失敗不能影響請求 */
    }
  }
  const info = (message) => {
    try {
      ctx.logger?.info?.(`${name}: ${message}`)
    } catch {
      /* ignore */
    }
  }

  const stats = {
    requests: 0,
    wakes: 0,
    coldStarts: 0,
    idleStops: 0,
    wakeFailures: 0,
    lastWakeAt: null,
    lastStopAt: null,
  }

  /** 進行中的請求數：>0 時不關代理。 */
  let busy = 0
  let timer = null
  let lastError = null
  let disposed = false
  let stopping = null
  let shutdown = null

  const beginStop = () => {
    if (stopping) return stopping
    const task = stopProxy(cfg)
    stopping = task
    const clear = () => { if (stopping === task) stopping = null }
    task.then(clear, clear)
    return task
  }

  const finishShutdown = () => {
    if (busy > 0) return
    if (!shutdown) shutdown = (async () => {
      await startup
      if (stopping) await stopping.catch(() => {})
      flushState(cfg.stateFile)
      const result = await beginStop()
      if (!result.ok) warn(`shutdown stop incomplete (${result.reason})`, new Error('stop incomplete'))
    })().catch(err => warn('shutdown cleanup failed', err))
    return shutdown
  }

  const clearTimer = () => {
    if (timer) {
      clearTimeout(timer)
      timer = null
    }
  }

  /** 重置閒置計時器。unref 讓它不會拖住行程退出。 */
  const armTimer = () => {
    clearTimer()
    if (alwaysOn) return
    if (disposed || busy > 0) return
    timer = setTimeout(() => {
      timer = null
      void onIdle()
    }, idleMs)
    if (typeof timer.unref === 'function') timer.unref()
  }

  /** 閒置到期：沒人在用就關掉代理。 */
  const onIdle = async () => {
    if (disposed || busy > 0) return
    try {
      const result = await beginStop()
      if (result.stopped) {
        stats.idleStops += 1
        stats.lastStopAt = Date.now()
        info(`idle ${idleLabel} → proxy stopped (pid ${result.pid})`)
      }
      if (!result.ok) {
        lastError = `idle stop incomplete: ${result.reason}`
        warn(lastError, new Error('stop incomplete'))
        armTimer()
      }
    } catch (err) {
      lastError = String((err && err.message) || err)
      warn('idle stop failed', err)
      armTimer()
    }
  }

  // ── 核心：攔截 llm/stream，只在用到 um-genai 時才喚醒代理 ────────────────
  ctx.on('llm/stream', (...args) => {
    const next = args[args.length - 1]
    if (typeof next !== 'function') return undefined

    // 瀑布的參數是 (options, next)；保險起見從尾巴取，容忍上游多傳一個 thisArg。
    const options = args.length >= 2 ? args[args.length - 2] : undefined

    if (!isTargetProvider(options, cfg)) return next()

    return (async function* patchedStream() {
      if (disposed) throw new Error(`${name}: plugin disposed`)
      stats.requests += 1
      busy += 1
      clearTimer()
      try {
        await startup
        if (stopping) await stopping.catch(() => {})
        try {
          const result = await ensureProxy(cfg)
          stats.wakes += 1
          if (result.started) stats.coldStarts += 1
          stats.lastWakeAt = Date.now()
          lastError = null
          info(
            result.started
              ? `woke proxy (pid ${result.pid}) for provider ${options.provider}`
              : `proxy already up (pid ${result.pid}) for provider ${options.provider}`,
          )
        } catch (err) {
          // 喚醒失敗不吞掉請求：讓它照常往下走，由上游回報連線錯誤。
          stats.wakeFailures += 1
          lastError = String((err && err.message) || err)
          warn('wake failed', err)
        }
        yield* next()
      } finally {
        busy -= 1
        if (disposed) await finishShutdown()
        else armTimer()
      }
    })()
  })

  // ── 唯讀狀態端點：確認「現在代理有沒有在跑、誰把它叫起來的」 ──────────────
  const disposers = []
  disposers.push(
    ctx.webServer.register({
      kind: 'exact',
      path: STATUS_PATH,
      handler: async (req, res) => {
        let running = false
        try {
          running = await health(cfg.port, 2000, cfg.service)
        } catch {
          /* 檢查失敗就當作沒跑 */
        }
        const state = readState(cfg.stateFile) || null
        res.writeHead(200, {
          'Content-Type': 'application/json; charset=utf-8',
          'Cache-Control': 'no-store',
        })
        res.end(
          JSON.stringify({
            ok: true,
            widget: name,
            port: cfg.port,
            idleMinutes,
            idleMs,
            alwaysOn,
            running,
            pid: running ? state?.pid ?? null : null,
            inFlightRequests: busy,
            inFlightWakes: inflightCount(),
            wakeRetries: wakeRetryCount(),
            idleTimerArmed: timer !== null,
            stats,
            lastError,
            now: Date.now(),
          }),
        )
      },
    }),
  )

  // ── 啟動時清掉上一次留下的孤兒；關閉時把代理一起關掉 ────────────────────
  const startup = (async () => {
    try {
      if (alwaysOn) {
        // 常駐模式：宿主一啟動就把代理叫起來，之後不再閒置關閉，
        // 這樣 DSH 重啟不會留下一段「8788 沒人聽」的空窗。
        const state = await ensureProxy(cfg)
        if (disposed) return
        if (state?.started) {
          stats.wakes += 1
          stats.coldStarts += 1
          stats.lastWakeAt = Date.now()
          info(`always-on: warmed the proxy at startup (pid ${state.pid})`)
        } else {
          info(`always-on: proxy already running (pid ${state?.pid ?? '?'})`)
        }
        return
      }
      const state = readState(cfg.stateFile)
      const age = state?.lastUsedAt ? Date.now() - state.lastUsedAt : Number.POSITIVE_INFINITY
      const running = await health(cfg.port, 2000, cfg.service)
      if (disposed || busy > 0) return
      if (!running) {
        clearState(cfg.stateFile)
        return
      }
      if (age > idleMs) {
        const result = await beginStop()
        if (result.stopped) info(`stopped a leftover proxy from a previous run (pid ${result.pid})`)
        if (!result.ok) armTimer()
        return
      }
      // 剛用過（例如 host 熱重載）：保留它，但一定要布署計時器，
      // 否則它會變成「DSH 關掉之前永遠常駐」——那正是這次要消除的行為。
      armTimer()
      info(`proxy already running (pid ${state?.pid ?? '?'}); will re-check in ${idleLabel}`)
    } catch (err) {
      warn('startup cleanup failed', err)
      armTimer()
    }
  })()

  ctx.effect(() => () => {
    disposed = true
    clearTimer()
    for (const dispose of disposers) {
      try {
        dispose()
      } catch {
        /* ignore */
      }
    }
    flushState(cfg.stateFile)
    if (alwaysOn) {
      // 常駐模式：宿主關閉不要把代理一起帶走，否則 DSH 每次重啟都會留下一段 8788 空窗。
      // 代理本身的存活由看門狗（um-renew.py，每 5 分鐘）接手。
      info('always-on: leaving the proxy running after the host shuts down')
      return
    }
    return finishShutdown()
  })
}
