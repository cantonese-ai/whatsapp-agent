'use strict'

const { describe, it } = require('node:test')
const assert = require('node:assert/strict')
const { createVoiceNoteCatchUp } = require('./catchup')

function silentLogger () {
  const calls = { info: [], warn: [], error: [] }
  return {
    calls,
    info: (...a) => { calls.info.push(a) },
    warn: (...a) => { calls.warn.push(a) },
    error: (...a) => { calls.error.push(a) }
  }
}

const wait = (ms) => new Promise((r) => setTimeout(r, ms))

describe('createVoiceNoteCatchUp', () => {
  it('clears lock after timeout when getChats never resolves', async () => {
    const logger = silentLogger()
    let hangResolve = () => {}
    const hang = new Promise((resolve) => { hangResolve = resolve })
    const ctl = createVoiceNoteCatchUp({
      sessionId: 'demo',
      logger,
      timeoutMs: 80,
      recentFromSeconds: () => 1,
      runScan: async () => {
        await hang
        return { sent: 0, skippedAlready: 0, scanned: 0 }
      }
    })
    try {
      const p = ctl.catchUp(1, 0, 'full')
      await wait(25)
      assert.equal(ctl.getState().catchupRunning, true)
      await p
      assert.equal(ctl.getState().catchupRunning, false)
      assert.equal(ctl.getState().catchupRunningMode, null)
      assert.ok(logger.calls.warn.some((a) => /Voice note catch-up timed out/.test(a[1])))
    } finally {
      hangResolve()
      ctl.dispose()
    }
  })

  it('runs deferred recent after timeout clears lock', async () => {
    const logger = silentLogger()
    const modes = []
    let releaseHang = () => {}
    const hang = new Promise((resolve) => { releaseHang = resolve })
    let first = true
    const deferred = []
    const ctl = createVoiceNoteCatchUp({
      sessionId: 'demo',
      logger,
      timeoutMs: 60,
      recentFromSeconds: () => 1000,
      scheduleDeferred: (fn) => { deferred.push(fn) },
      runScan: async (_from, mode) => {
        modes.push(mode)
        if (first) {
          first = false
          await hang
        }
        return { sent: 0, skippedAlready: 0, scanned: mode === 'recent' ? 1 : 0, remembered: 0 }
      }
    })
    try {
      const fullP = ctl.catchUp(1, 1000, 'full')
      await wait(15)
      await ctl.catchUp(1, 20, 'recent')
      assert.ok(ctl.getState().catchupRecentPending)
      await fullP
      assert.equal(ctl.getState().catchupRunning, false)
      assert.equal(deferred.length, 1)
      releaseHang()
      await deferred[0]()
      await wait(20)
      assert.ok(modes.includes('recent'))
      assert.equal(ctl.getState().catchupRunning, false)
      assert.ok(logger.calls.info.some((a) => /deferred/.test(a[1])))
    } finally {
      releaseHang()
      ctl.dispose()
    }
  })

  it('concurrent catchUp while running defers/skips without leaking lock', async () => {
    const logger = silentLogger()
    let release = () => {}
    const gate = new Promise((resolve) => { release = resolve })
    const ctl = createVoiceNoteCatchUp({
      sessionId: 'demo',
      logger,
      timeoutMs: 2000,
      recentFromSeconds: () => 1,
      runScan: async (_from, mode) => {
        if (mode === 'full') await gate
        return { sent: 0, skippedAlready: 0, scanned: 0, remembered: 0 }
      }
    })
    try {
      const fullP = ctl.catchUp(1, 0, 'full')
      await wait(15)
      assert.equal(ctl.getState().catchupRunning, true)
      await ctl.catchUp(1, 0, 'recent')
      await ctl.catchUp(1, 0, 'full')
      assert.ok(ctl.getState().catchupRecentPending)
      assert.ok(logger.calls.info.some((a) => /deferred/.test(a[1])))
      assert.ok(logger.calls.info.some((a) => /skipped/.test(a[1])))
      release()
      await fullP
      await wait(40)
      assert.equal(ctl.getState().catchupRunning, false)
      assert.equal(ctl.getState().catchupRecentPending, null)
    } finally {
      release()
      ctl.dispose()
    }
  })

  it('successful catchUp still clears lock in finally', async () => {
    const logger = silentLogger()
    const ctl = createVoiceNoteCatchUp({
      sessionId: 'demo',
      logger,
      timeoutMs: 2000,
      recentFromSeconds: () => 1,
      runScan: async () => ({ sent: 2, skippedAlready: 1, scanned: 3, remembered: 4 })
    })
    try {
      await ctl.catchUp(1, 0, 'full')
      assert.equal(ctl.getState().catchupRunning, false)
      assert.equal(ctl.getState().catchupRunningMode, null)
      assert.ok(logger.calls.info.some((a) => /Voice note catch-up done/.test(a[1])))
      assert.equal(logger.calls.warn.length, 0)
    } finally {
      ctl.dispose()
    }
  })

  it('watchdog force-clears lock stuck beyond 2x timeout', async () => {
    const logger = silentLogger()
    const intervals = []
    const ctl = createVoiceNoteCatchUp({
      sessionId: 'demo',
      logger,
      timeoutMs: 40,
      recentFromSeconds: () => 1,
      runScan: async () => ({ sent: 0, skippedAlready: 0, scanned: 0 }),
      setIntervalFn: (fn, ms) => {
        const id = { fn, ms }
        intervals.push(id)
        return id
      },
      clearIntervalFn: () => {}
    })
    try {
      ctl._setRunningForTest(true, 'full', Date.now() - 200)
      assert.equal(ctl.getState().catchupRunning, true)
      for (const id of intervals) id.fn()
      assert.equal(ctl.getState().catchupRunning, false)
      assert.ok(logger.calls.warn.some((a) => /watchdog force-cleared/.test(a[1])))
    } finally {
      ctl.dispose()
    }
  })
})
