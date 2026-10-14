// cantonese.ai patch: voice-note catch-up lock + hard timeout + watchdog.
// Extracted so unit tests can mock getChats/scan without loading whatsapp-web.js.
// Used by the bind-mounted sessions.js patch.

'use strict'

/**
 * @param {object} opts
 * @param {string} opts.sessionId
 * @param {{ info: Function, warn: Function, error: Function }} opts.logger
 * @param {number} [opts.timeoutMs] CATCHUP_TIMEOUT_MS (default 45000)
 * @param {() => number} opts.recentFromSeconds unix seconds lookback start for deferred recent
 * @param {(from: number, mode: string, delayMs: number, ctx: { deadline: number, isTimedOut: () => boolean }) => Promise<{ sent: number, skippedAlready: number, scanned: number, remembered?: number }>} opts.runScan
 * @param {(fn: Function) => void} [opts.scheduleDeferred] default setImmediate
 * @param {(ms: number, fn: Function) => any} [opts.setIntervalFn]
 * @param {(id: any) => void} [opts.clearIntervalFn]
 */
function createVoiceNoteCatchUp (opts) {
  const {
    sessionId,
    logger,
    timeoutMs = parseInt(process.env.CATCHUP_TIMEOUT_MS || '45000', 10),
    recentFromSeconds,
    runScan,
    scheduleDeferred = (fn) => setImmediate(fn),
    setIntervalFn = setInterval,
    clearIntervalFn = clearInterval
  } = opts

  const CATCHUP_TIMEOUT_MS = Math.max(1, timeoutMs | 0)
  const WATCHDOG_MS = Math.max(1000, Math.floor(CATCHUP_TIMEOUT_MS / 2))

  let catchupRunning = false
  let catchupRunningMode = null
  let catchupRecentPending = null // { delayMs }
  let catchupStartedAt = 0
  let disposed = false

  const catchUpTimeoutError = () => {
    const err = new Error('catch-up timed out')
    err.code = 'CATCHUP_TIMEOUT'
    return err
  }

  const flushPendingRecent = () => {
    if (!catchupRecentPending || disposed) return
    const pending = catchupRecentPending
    catchupRecentPending = null
    const fromRecent = recentFromSeconds()
    scheduleDeferred(() => { catchUp(fromRecent, pending.delayMs, 'recent') })
  }

  const clearLock = (reason) => {
    catchupRunning = false
    catchupRunningMode = null
    catchupStartedAt = 0
    if (reason) {
      logger.warn({ sessionId, reason, timeoutMs: CATCHUP_TIMEOUT_MS }, 'Voice note catch-up watchdog force-cleared stuck lock')
    }
    flushPendingRecent()
  }

  const watchdog = setIntervalFn(() => {
    if (disposed || !catchupRunning || !catchupStartedAt) return
    if (Date.now() - catchupStartedAt > CATCHUP_TIMEOUT_MS * 2) {
      clearLock('stuck_over_2x_timeout')
    }
  }, WATCHDOG_MS)
  if (typeof watchdog?.unref === 'function') watchdog.unref()

  const catchUp = async (from, delayMs, mode = 'full') => {
    if (disposed) return
    if (catchupRunning) {
      // Prefer recent/targeted freshness: defer a recent scan to run right after the
      // current one instead of dropping it behind a long full scan.
      if (mode === 'recent') {
        catchupRecentPending = { delayMs }
        logger.info({ sessionId, mode, running: catchupRunningMode }, 'Voice note catch-up deferred (will run after current)')
      } else {
        logger.info({ sessionId, mode, running: catchupRunningMode }, 'Voice note catch-up skipped (already running)')
      }
      return
    }
    catchupRunning = true
    catchupRunningMode = mode
    catchupStartedAt = Date.now()
    let sent = 0
    let skippedAlready = 0
    let scanned = 0
    const deadline = Date.now() + CATCHUP_TIMEOUT_MS
    const isTimedOut = () => Date.now() >= deadline

    let timeoutHandle = null
    const timeoutPromise = new Promise((_, reject) => {
      const ms = Math.max(1, deadline - Date.now())
      // Do not unref: an unref'd deadline lets the event loop drain while getChats hangs
      // (unit tests, and idle workers), so the lock would never clear.
      timeoutHandle = setTimeout(() => reject(catchUpTimeoutError()), ms)
    })

    // Detach so a hung getChats/scan does not leave an unhandled rejection after race timeout,
    // and so we do not await orphaned puppeteer work forever.
    const work = Promise.resolve().then(() => runScan(from, mode, delayMs, { deadline, isTimedOut }))
    work.catch(() => {})

    try {
      const result = await Promise.race([work, timeoutPromise])
      if (result && typeof result === 'object') {
        sent = result.sent || 0
        skippedAlready = result.skippedAlready || 0
        scanned = result.scanned || 0
        logger.info({
          sessionId,
          since: from,
          sent,
          skippedAlready,
          scanned,
          mode,
          afterSeconds: delayMs / 1000,
          remembered: result.remembered
        }, 'Voice note catch-up done')
      }
    } catch (err) {
      if (err && (err.code === 'CATCHUP_TIMEOUT' || /catch-up timed out/.test(String(err.message || err)))) {
        logger.warn({ sessionId, mode, sent, skippedAlready, scanned, timeoutMs: CATCHUP_TIMEOUT_MS }, 'Voice note catch-up timed out')
      } else {
        logger.error({ sessionId, err, mode }, 'Voice note catch-up failed')
      }
    } finally {
      if (timeoutHandle) clearTimeout(timeoutHandle)
      // Always clear the lock so status/keep-alive and later scans can proceed even if
      // orphaned puppeteer work is still pending in the background.
      catchupRunning = false
      catchupRunningMode = null
      catchupStartedAt = 0
      flushPendingRecent()
    }
  }

  const dispose = () => {
    disposed = true
    clearIntervalFn(watchdog)
    catchupRecentPending = null
    catchupRunning = false
    catchupRunningMode = null
    catchupStartedAt = 0
  }

  const getState = () => ({
    catchupRunning,
    catchupRunningMode,
    catchupRecentPending: catchupRecentPending ? { ...catchupRecentPending } : null,
    catchupStartedAt,
    timeoutMs: CATCHUP_TIMEOUT_MS
  })

  // Test/helper: force the running flag as if a scan started (for concurrent-defer tests).
  const _setRunningForTest = (running, mode = 'full', startedAt = Date.now()) => {
    catchupRunning = !!running
    catchupRunningMode = running ? mode : null
    catchupStartedAt = running ? startedAt : 0
  }

  return { catchUp, dispose, getState, _setRunningForTest, CATCHUP_TIMEOUT_MS }
}

module.exports = {
  createVoiceNoteCatchUp
}
