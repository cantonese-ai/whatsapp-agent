// Patched copy of src/sessions.js from wwebjs-api v1.34.7-hotfix2
// (https://github.com/avoylenko/wwebjs-api), mounted over the stock image's file.
//
// MIT License
// Copyright (c) 2024 Anton Voylenko
// Permission is hereby granted, free of charge, to any person obtaining a copy of this software
// and associated documentation files (the "Software"), to deal in the Software without
// restriction, including without limitation the rights to use, copy, modify, merge, publish,
// distribute, sublicense, and/or sell copies of the Software, and to permit persons to whom the
// Software is furnished to do so, subject to the following conditions:
// The above copyright notice and this permission notice shall be included in all copies or
// substantial portions of the Software.
// THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING
// BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND
// NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM,
// DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
// OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.
//
// Modifications (c) cantonese.ai contributors, also MIT. They are marked "cantonese.ai patch":
//   - an unhandled promise rejection is logged instead of killing the process
//   - session_paired / session_removed webhook events when a number is linked or unlinked
//   - your own voice notes are forwarded as 'media' events too (the stock 'message' event skips them)
//   - catch-up: voice notes that arrived while the session was offline are re-sent on 'ready'
// The per-session webhook override (<SESSIONID>_WEBHOOK_URL) is upstream behaviour and is also
// used for the new events. Keep this file in step with the image tag in docker-compose.yml.

const { Client, LocalAuth } = require('whatsapp-web.js')
const fs = require('fs')
const path = require('path')
const sessions = new Map()
const { baseWebhookURL, sessionFolderPath, maxAttachmentSize, setMessagesAsSeen, webVersion, webVersionCacheType, recoverSessions, chromeBin, headless, releaseBrowserLock, proxyUrl, proxyUsername, proxyPassword } = require('./config')
const { triggerWebhook, waitForNestedObject, isEventEnabled, sendMessageSeenStatus, sleep, patchWWebLibrary } = require('./utils')
const { logger } = require('./logger')
const { initWebSocketServer, terminateWebSocketServer, triggerWebSocket } = require('./websocket')

// Function to validate if the session is ready
const validateSession = async (sessionId) => {
  try {
    const returnData = { success: false, state: null, message: '' }

    // Session not Connected 😢
    if (!sessions.has(sessionId) || !sessions.get(sessionId)) {
      returnData.message = 'session_not_found'
      return returnData
    }

    const client = sessions.get(sessionId)
    // wait until the client is created
    await waitForNestedObject(client, 'pupPage')
      .catch((err) => { return { success: false, state: null, message: err.message } })

    // Wait for client.pupPage to be evaluable
    let maxRetry = 0
    while (true) {
      try {
        if (client.pupPage.isClosed()) {
          return { success: false, state: null, message: 'browser tab closed' }
        }
        await Promise.race([
          client.pupPage.evaluate('1'),
          new Promise(resolve => setTimeout(resolve, 1000))
        ])
        break
      } catch (error) {
        if (maxRetry === 2) {
          return { success: false, state: null, message: 'session closed' }
        }
        maxRetry++
      }
    }

    const state = await client.getState()
    returnData.state = state
    if (state !== 'CONNECTED') {
      returnData.message = 'session_not_connected'
      return returnData
    }

    // Session Connected 🎉
    returnData.success = true
    returnData.message = 'session_connected'
    return returnData
  } catch (error) {
    logger.error({ sessionId, err: error }, 'Failed to validate session')
    return { success: false, state: null, message: error.message }
  }
}

// Function to handle client session restoration
const restoreSessions = () => {
  try {
    if (!fs.existsSync(sessionFolderPath)) {
      fs.mkdirSync(sessionFolderPath) // Create the session directory if it doesn't exist
    }
    // Read the contents of the folder
    fs.readdir(sessionFolderPath, async (_, files) => {
      // Iterate through the files in the parent folder
      for (const file of files) {
        // Use regular expression to extract the string from the folder name
        const match = file.match(/^session-(.+)$/)
        if (match) {
          const sessionId = match[1]
          logger.warn({ sessionId }, 'Existing session detected')
          await setupSession(sessionId)
        }
      }
    })
  } catch (error) {
    logger.error(error, 'Failed to restore sessions')
  }
}

// Setup Session
const setupSession = async (sessionId, pairWithPhoneNumber) => {
  try {
    if (sessions.has(sessionId)) {
      return { success: false, message: `Session already exists for: ${sessionId}`, client: sessions.get(sessionId) }
    }
    logger.info({ sessionId }, 'Session is being initiated')
    // Disable the delete folder from the logout function (will be handled separately)
    const localAuth = new LocalAuth({ clientId: sessionId, dataPath: sessionFolderPath })
    delete localAuth.logout
    localAuth.logout = () => { }

    const clientOptions = {
      puppeteer: {
        executablePath: chromeBin,
        headless,
        args: [
          '--autoplay-policy=user-gesture-required',
          '--disable-background-networking',
          '--disable-background-timer-throttling',
          '--disable-backgrounding-occluded-windows',
          '--disable-breakpad',
          '--disable-client-side-phishing-detection',
          '--disable-component-update',
          '--disable-default-apps',
          '--disable-dev-shm-usage',
          '--disable-domain-reliability',
          '--disable-extensions',
          '--disable-features=AudioServiceOutOfProcess',
          '--disable-hang-monitor',
          '--disable-ipc-flooding-protection',
          '--disable-notifications',
          '--disable-offer-store-unmasked-wallet-cards',
          '--disable-popup-blocking',
          '--disable-print-preview',
          '--disable-prompt-on-repost',
          '--disable-renderer-backgrounding',
          '--disable-speech-api',
          '--disable-sync',
          '--disable-gpu',
          '--disable-accelerated-2d-canvas',
          '--hide-scrollbars',
          '--ignore-gpu-blacklist',
          '--metrics-recording-only',
          '--mute-audio',
          '--no-default-browser-check',
          '--no-first-run',
          '--no-pings',
          '--no-zygote',
          '--password-store=basic',
          '--use-gl=swiftshader',
          '--use-mock-keychain',
          '--disable-setuid-sandbox',
          '--no-sandbox',
          '--disable-blink-features=AutomationControlled',
          // Route Chromium outbound traffic through PROXY_URL when configured.
          ...(proxyUrl ? [`--proxy-server=${proxyUrl}`] : [])
        ]
      },
      authStrategy: localAuth
    }

    if (pairWithPhoneNumber) {
      clientOptions.pairWithPhoneNumber = pairWithPhoneNumber
    }

    if (proxyUrl && proxyUsername != null && proxyPassword != null) {
      clientOptions.proxyAuthentication = { username: proxyUsername, password: proxyPassword }
    }

    if (webVersion) {
      clientOptions.webVersion = webVersion
      switch (webVersionCacheType.toLowerCase()) {
        case 'local':
          clientOptions.webVersionCache = {
            type: 'local'
          }
          break
        case 'remote':
          clientOptions.webVersionCache = {
            type: 'remote',
            remotePath: 'https://raw.githubusercontent.com/wppconnect-team/wa-version/main/html/' + webVersion + '.html'
          }
          break
        default:
          clientOptions.webVersionCache = {
            type: 'none'
          }
      }
    }

    const client = new Client(clientOptions)
    const pairingCode = pairWithPhoneNumber
      ? new Promise((resolve) => client.once('code', resolve))
      : null
    if (releaseBrowserLock) {
      // See https://github.com/puppeteer/puppeteer/issues/4860
      const singletonLockPath = path.resolve(path.join(sessionFolderPath, `session-${sessionId}`, 'SingletonLock'))
      const singletonLockExists = await fs.promises.lstat(singletonLockPath).then(() => true).catch(() => false)
      if (singletonLockExists) {
        logger.warn({ sessionId }, 'Browser lock file exists, removing')
        await fs.promises.unlink(singletonLockPath)
      }
    }

    try {
      // `ready` fires again every time the page reloads and the library re-injects itself, not
      // just once per session. The in-page half of the patch lives on the page's own prototypes,
      // so a reload wipes it - and with `once` it was never put back. In production the patch went
      // stale roughly a day after each session start, which is exactly when `sendMessage` began
      // handing back nothing. Re-run it on every `ready`; `patchSerializedIds` is idempotent and
      // reports `already applied` when the page still carries it.
      client.on('ready', () => {
        patchWWebLibrary(client, sessionId).catch((err) => {
          logger.error({ sessionId, err }, 'Failed to patch WWebJS library')
        })
      })
      initWebSocketServer(sessionId)
      initializeEvents(client, sessionId)
      await client.initialize()
    } catch (error) {
      logger.error({ sessionId, err: error }, 'Initialize error')
      throw error
    }

    // Save the session to the Map
    sessions.set(sessionId, client)
    return {
      success: true,
      message: 'Session initiated successfully',
      client,
      pairingCode: pairingCode ? await pairingCode : null
    }
  } catch (error) {
    return { success: false, message: error.message, client: null }
  }
}

// cantonese.ai patch: a rejection nobody catches must not kill the process and every linked number
// with it. whatsapp-web.js's pairing-code flow (POST /session/requestPairingCode) can reject inside
// the page with nothing to catch it, which used to restart the whole container.
process.on('unhandledRejection', (reason) => {
  logger.error({ err: reason }, 'Unhandled promise rejection (process kept running)')
})

// cantonese.ai patch: pairing / removal notifications for voice-transcriber (which records them
// and, if configured, e-mails them).
// session-<id>/cantonese_ai_pairing.json records which number a session is linked to.
const cantoneseAiPairingPath = (sessionId) => path.join(sessionFolderPath, `session-${sessionId}`, 'cantonese_ai_pairing.json')
const cantoneseAiLastSeenPath = (sessionId) => path.join(sessionFolderPath, `session-${sessionId}`, 'cantonese_ai_last_seen')
const cantoneseAiWebhookUrl = (sessionId) => process.env[sessionId.toUpperCase() + '_WEBHOOK_URL'] || baseWebhookURL

// On 'ready': a session with no pairing record has just been linked. Sessions linked before this
// patch (they already have a last_seen) are recorded silently instead of reported.
const recordPairing = async (client, sessionId, wasConnectedBefore) => {
  const pairingPath = cantoneseAiPairingPath(sessionId)
  if (fs.existsSync(pairingPath)) return
  const pairing = { phone: client.info?.wid?.user || null, pushname: client.info?.pushname || null, pairedAt: Date.now() }
  await fs.promises.writeFile(pairingPath, JSON.stringify(pairing))
  // wasConnectedBefore: the same session linked again after WhatsApp or the phone unlinked it.
  logger.info({ sessionId, phone: pairing.phone, relinked: wasConnectedBefore }, 'New number paired')
  triggerWebhook(cantoneseAiWebhookUrl(sessionId), sessionId, 'session_paired',
    { eventId: `paired:${sessionId}:${pairing.pairedAt}`, ...pairing })
}

// The number was unlinked (from the phone, or the session terminated via the API). Reports once:
// the pairing record is removed, so a later re-link e-mails again. last_seen is kept so that a
// re-link catches up on voice notes that arrived while the number was unlinked.
const reportSessionRemoved = async (sessionId, reason) => {
  let pairing
  try {
    pairing = JSON.parse(await fs.promises.readFile(cantoneseAiPairingPath(sessionId), 'utf8'))
  } catch (_) {
    return // never paired (or already reported)
  }
  await fs.promises.rm(cantoneseAiPairingPath(sessionId), { force: true })
  logger.info({ sessionId, phone: pairing.phone, reason }, 'Number removed')
  triggerWebhook(cantoneseAiWebhookUrl(sessionId), sessionId, 'session_removed',
    { eventId: `removed:${sessionId}:${pairing.pairedAt}`, ...pairing, reason })
}

const initializeEvents = (client, sessionId) => {
  // check if the session webhook is overridden
  const sessionWebhook = process.env[sessionId.toUpperCase() + '_WEBHOOK_URL'] || baseWebhookURL

  if (recoverSessions) {
    waitForNestedObject(client, 'pupPage').then(() => {
      const restartSession = async (sessionId) => {
        sessions.delete(sessionId)
        await client.destroy().catch(e => { })
        await setupSession(sessionId)
      }
      client.pupPage.once('close', function () {
        // emitted when the page closes
        logger.warn({ sessionId }, 'Browser page closed. Restoring')
        restartSession(sessionId)
      })
      client.pupPage.once('error', function () {
        // emitted when the page crashes
        logger.warn({ sessionId }, 'Error occurred on browser page. Restoring')
        restartSession(sessionId)
      })
      client.pupPage
        .on('console', message => {
          const type = message.type().substr(0, 3).toUpperCase()
          logger.debug({ sessionId, type }, `Page console log: ${message.text()}`)
        })
        .on('requestfailed', request => {
          const failure = request.failure()
          if (!failure) {
            logger.error({ sessionId, url: request.url() }, 'Page request failed but no failure reason provided')
            return
          }
          // WhatsApp Web aborts its own long-poll requests as a matter of course. In a four day
          // production log these were 403 lines out of 492, which buries everything worth
          // reading, so they are reported at debug and real failures keep the error level.
          const level = failure.errorText === 'net::ERR_ABORTED' ? 'debug' : 'error'
          logger[level]({ sessionId, url: request.url() }, `Page request failed: ${failure.errorText}`)
        })
        .on('pageerror', ({ message }) => logger.error({ sessionId, message }, 'Page error occurred'))
    }).catch(e => { })
  }

  if (isEventEnabled('auth_failure')) {
    client.on('auth_failure', (msg) => {
      triggerWebhook(sessionWebhook, sessionId, 'status', { msg })
      triggerWebSocket(sessionId, 'status', { msg })
    })
  }

  client.on('authenticated', () => {
    client.qr = null
    if (isEventEnabled('authenticated')) {
      triggerWebhook(sessionWebhook, sessionId, 'authenticated')
      triggerWebSocket(sessionId, 'authenticated')
    }
  })

  if (isEventEnabled('call')) {
    client.on('call', (call) => {
      triggerWebhook(sessionWebhook, sessionId, 'call', { call })
      triggerWebSocket(sessionId, 'call', { call })
    })
  }

  if (isEventEnabled('change_state')) {
    client.on('change_state', state => {
      triggerWebhook(sessionWebhook, sessionId, 'change_state', { state })
      triggerWebSocket(sessionId, 'change_state', { state })
    })
  }

  if (isEventEnabled('disconnected')) {
    client.on('disconnected', (reason) => {
      triggerWebhook(sessionWebhook, sessionId, 'disconnected', { reason })
      triggerWebSocket(sessionId, 'disconnected', { reason })
    })
  }

  if (isEventEnabled('group_join')) {
    client.on('group_join', (notification) => {
      triggerWebhook(sessionWebhook, sessionId, 'group_join', { notification })
      triggerWebSocket(sessionId, 'group_join', { notification })
    })
  }

  if (isEventEnabled('group_leave')) {
    client.on('group_leave', (notification) => {
      triggerWebhook(sessionWebhook, sessionId, 'group_leave', { notification })
      triggerWebSocket(sessionId, 'group_leave', { notification })
    })
  }

  if (isEventEnabled('group_admin_changed')) {
    client.on('group_admin_changed', (notification) => {
      triggerWebhook(sessionWebhook, sessionId, 'group_admin_changed', { notification })
      triggerWebSocket(sessionId, 'group_admin_changed', { notification })
    })
  }

  if (isEventEnabled('group_membership_request')) {
    client.on('group_membership_request', (notification) => {
      triggerWebhook(sessionWebhook, sessionId, 'group_membership_request', { notification })
      triggerWebSocket(sessionId, 'group_membership_request', { notification })
    })
  }

  if (isEventEnabled('group_update')) {
    client.on('group_update', (notification) => {
      triggerWebhook(sessionWebhook, sessionId, 'group_update', { notification })
      triggerWebSocket(sessionId, 'group_update', { notification })
    })
  }

  if (isEventEnabled('loading_screen')) {
    client.on('loading_screen', (percent, message) => {
      triggerWebhook(sessionWebhook, sessionId, 'loading_screen', { percent, message })
      triggerWebSocket(sessionId, 'loading_screen', { percent, message })
    })
  }

  if (isEventEnabled('media_uploaded')) {
    client.on('media_uploaded', (message) => {
      triggerWebhook(sessionWebhook, sessionId, 'media_uploaded', { message })
      triggerWebSocket(sessionId, 'media_uploaded', { message })
    })
  }

  client.on('message', async (message) => {
    if (isEventEnabled('message')) {
      triggerWebhook(sessionWebhook, sessionId, 'message', { message })
      triggerWebSocket(sessionId, 'message', { message })
      if (message.hasMedia && message._data?.size < maxAttachmentSize) {
      // custom service event
        if (isEventEnabled('media')) {
          message.downloadMedia().then(messageMedia => {
            triggerWebhook(sessionWebhook, sessionId, 'media', { messageMedia, message })
            triggerWebSocket(sessionId, 'media', { messageMedia, message })
          }).catch(error => {
            logger.error({ sessionId, err: error }, 'Failed to download media')
          })
        }
      }
    }
    if (setMessagesAsSeen) {
      // small delay to ensure the message is processed before sending seen status
      await sleep(1000)
      sendMessageSeenStatus(message)
    }
  })

  if (isEventEnabled('message_ack')) {
    client.on('message_ack', (message, ack) => {
      triggerWebhook(sessionWebhook, sessionId, 'message_ack', { message, ack })
      triggerWebSocket(sessionId, 'message_ack', { message, ack })
    })
  }

  if (isEventEnabled('message_create')) {
    client.on('message_create', (message) => {
      triggerWebhook(sessionWebhook, sessionId, 'message_create', { message })
      triggerWebSocket(sessionId, 'message_create', { message })
    })
  }

  // cantonese.ai patch: 'message' skips our own messages, so also download our own voice
  // notes (e.g. "Message yourself") for voice-transcriber. The REST download endpoints can't
  // find them afterwards (fetchMessages({ messageId }) returns nothing on this version).
  if (isEventEnabled('media')) {
    client.on('message_create', (message) => {
      if (!message.fromMe || !['ptt', 'audio'].includes(message.type)) return
      if (!message.hasMedia || !(message._data?.size < maxAttachmentSize)) return
      message.downloadMedia().then(messageMedia => {
        triggerWebhook(sessionWebhook, sessionId, 'media', { messageMedia, message })
        triggerWebSocket(sessionId, 'media', { messageMedia, message })
      }).catch(error => {
        logger.error({ sessionId, err: error }, 'Failed to download own media')
      })
    })
  }

  // cantonese.ai patch: catch up on voice notes that arrived while this session was not
  // connected (e.g. a container restart). The time the
  // session was last connected is kept in its folder; on 'ready' every ptt/audio since then
  // is re-sent as 'media' and voice-transcriber drops the ones it already has. A session's
  // first link has no saved time, so old voice notes are never transcribed.
  if (isEventEnabled('media')) {
    const lastSeenPath = cantoneseAiLastSeenPath(sessionId)
    // Never older than an hour by default: posting transcripts hours late is unwanted.
    const catchupMaxSeconds = parseInt(process.env.CATCHUP_MAX_SECONDS || '3600')
    const marginSeconds = 120 // last_seen is saved every 30s
    let lastSeenTimer = null
    const saveLastSeen = async () => {
      try {
        if (await client.getState() !== 'CONNECTED') return
        await fs.promises.writeFile(lastSeenPath, String(Math.floor(Date.now() / 1000)))
      } catch (err) {
        logger.error({ sessionId, err }, 'Failed to save last_seen')
      }
    }
    client.on('ready', async () => {
      let since = null
      try {
        since = parseInt(await fs.promises.readFile(lastSeenPath, 'utf8')) || null
      } catch (_) {}
      await recordPairing(client, sessionId, Boolean(since)).catch((err) => {
        logger.error({ sessionId, err }, 'Failed to record pairing')
      })
      await saveLastSeen()
      if (!lastSeenTimer) lastSeenTimer = setInterval(saveLastSeen, 30000)
      if (!since) {
        logger.info({ sessionId }, 'Voice note catch-up: first connection, nothing to catch up')
        return
      }
      const now = Math.floor(Date.now() / 1000)
      const from = Math.max(since - marginSeconds, now - catchupMaxSeconds)
      // A freshly (re-)linked device downloads chat history gradually, so look again later;
      // voice-transcriber drops notes it already has.
      for (const delayMs of [0, 60000, 300000]) {
        setTimeout(() => catchUp(from, delayMs), delayMs)
      }
    })
    const catchUp = async (from, delayMs) => {
      let sent = 0
      try {
        const chats = await client.getChats()
        for (const chat of chats) {
          if (!chat.timestamp || chat.timestamp < from || chat.id?._serialized === 'status@broadcast') continue
          // Busy chats: 50 messages may not reach back far enough.
          const messages = await chat.fetchMessages({ limit: 300 })
          for (const message of messages) {
            if (!['ptt', 'audio'].includes(message.type) || message.timestamp < from) continue
            if (!message.hasMedia || !(message._data?.size < maxAttachmentSize)) continue
            try {
              const messageMedia = await message.downloadMedia()
              if (!messageMedia) continue
              // catchUp: the transcript may already be in the chat; voice-transcriber checks first.
              triggerWebhook(sessionWebhook, sessionId, 'media', { messageMedia, message, catchUp: true })
              sent++
            } catch (err) {
              logger.error({ sessionId, err }, 'Voice note catch-up: download failed')
            }
          }
        }
        logger.info({ sessionId, since: from, sent, afterSeconds: delayMs / 1000 }, 'Voice note catch-up done')
      } catch (err) {
        logger.error({ sessionId, err }, 'Voice note catch-up failed')
      }
    }
    // WhatsApp can end a link without a LOGOUT event (a fresh link can drop after a few minutes and
    // the page reloads unpaired). A paired session asking for a QR code, or failing authentication,
    // has been unlinked.
    client.on('qr', () => {
      reportSessionRemoved(sessionId, 'unlinked by WhatsApp (the session asked for a new QR code)').catch((err) => {
        logger.error({ sessionId, err }, 'Failed to report removal')
      })
    })
    client.on('auth_failure', (message) => {
      reportSessionRemoved(sessionId, `authentication failed (${message})`).catch((err) => {
        logger.error({ sessionId, err }, 'Failed to report removal')
      })
    })
    client.on('disconnected', (reason) => {
      clearInterval(lastSeenTimer)
      lastSeenTimer = null
      // LOGOUT: unlinked from the phone's Linked devices. Other reasons (NAVIGATION, CONFLICT,
      // a crashed page) keep the login and reconnect, so they are not a removal.
      if (['LOGOUT', 'UNPAIRED'].includes(reason)) {
        reportSessionRemoved(sessionId, `unlinked (${reason})`).catch((err) => {
          logger.error({ sessionId, err }, 'Failed to report removal')
        })
      }
    })
  }

  if (isEventEnabled('message_reaction')) {
    client.on('message_reaction', (reaction) => {
      triggerWebhook(sessionWebhook, sessionId, 'message_reaction', { reaction })
      triggerWebSocket(sessionId, 'message_reaction', { reaction })
    })
  }

  if (isEventEnabled('message_edit')) {
    client.on('message_edit', (message, newBody, prevBody) => {
      triggerWebhook(sessionWebhook, sessionId, 'message_edit', { message, newBody, prevBody })
      triggerWebSocket(sessionId, 'message_edit', { message, newBody, prevBody })
    })
  }

  if (isEventEnabled('message_ciphertext')) {
    client.on('message_ciphertext', (message) => {
      triggerWebhook(sessionWebhook, sessionId, 'message_ciphertext', { message })
      triggerWebSocket(sessionId, 'message_ciphertext', { message })
    })
  }

  if (isEventEnabled('message_revoke_everyone')) {
    client.on('message_revoke_everyone', (message) => {
      triggerWebhook(sessionWebhook, sessionId, 'message_revoke_everyone', { message })
      triggerWebSocket(sessionId, 'message_revoke_everyone', { message })
    })
  }

  if (isEventEnabled('message_revoke_me')) {
    client.on('message_revoke_me', (message, revokedMsg) => {
      triggerWebhook(sessionWebhook, sessionId, 'message_revoke_me', { message, revokedMsg })
      triggerWebSocket(sessionId, 'message_revoke_me', { message, revokedMsg })
    })
  }

  client.on('qr', (qr) => {
    // inject qr code into session
    client.qr = qr
    if (isEventEnabled('qr')) {
      triggerWebhook(sessionWebhook, sessionId, 'qr', { qr })
      triggerWebSocket(sessionId, 'qr', { qr })
    }
  })

  if (isEventEnabled('ready')) {
    client.on('ready', () => {
      triggerWebhook(sessionWebhook, sessionId, 'ready')
      triggerWebSocket(sessionId, 'ready')
    })
  }

  if (isEventEnabled('contact_changed')) {
    client.on('contact_changed', (message, oldId, newId, isContact) => {
      triggerWebhook(sessionWebhook, sessionId, 'contact_changed', { message, oldId, newId, isContact })
      triggerWebSocket(sessionId, 'contact_changed', { message, oldId, newId, isContact })
    })
  }

  if (isEventEnabled('chat_removed')) {
    client.on('chat_removed', (chat) => {
      triggerWebhook(sessionWebhook, sessionId, 'chat_removed', { chat })
      triggerWebSocket(sessionId, 'chat_removed', { chat })
    })
  }

  if (isEventEnabled('chat_archived')) {
    client.on('chat_archived', (chat, currState, prevState) => {
      triggerWebhook(sessionWebhook, sessionId, 'chat_archived', { chat, currState, prevState })
      triggerWebSocket(sessionId, 'chat_archived', { chat, currState, prevState })
    })
  }

  if (isEventEnabled('unread_count')) {
    client.on('unread_count', (chat) => {
      triggerWebhook(sessionWebhook, sessionId, 'unread_count', { chat })
      triggerWebSocket(sessionId, 'unread_count', { chat })
    })
  }

  if (isEventEnabled('vote_update')) {
    client.on('vote_update', (vote) => {
      triggerWebhook(sessionWebhook, sessionId, 'vote_update', { vote })
      triggerWebSocket(sessionId, 'vote_update', { vote })
    })
  }

  if (isEventEnabled('code')) {
    client.on('code', (code) => {
      triggerWebhook(sessionWebhook, sessionId, 'code', { code })
      triggerWebSocket(sessionId, 'code', { code })
    })
  }
}

// Function to delete client session folder
const deleteSessionFolder = async (sessionId) => {
  try {
    const targetDirPath = path.join(sessionFolderPath, `session-${sessionId}`)
    const resolvedTargetDirPath = await fs.promises.realpath(targetDirPath)
    const resolvedSessionPath = await fs.promises.realpath(sessionFolderPath)

    // Ensure the target directory path ends with a path separator
    const safeSessionPath = `${resolvedSessionPath}${path.sep}`

    // Validate the resolved target directory path is a subdirectory of the session folder path
    if (!resolvedTargetDirPath.startsWith(safeSessionPath)) {
      throw new Error('Invalid path: Directory traversal detected')
    }
    await fs.promises.rm(resolvedTargetDirPath, { recursive: true, force: true })
  } catch (error) {
    logger.error({ sessionId, err: error }, 'Folder deletion error')
    throw error
  }
}

// Function to reload client session without removing browser cache
const reloadSession = async (sessionId) => {
  try {
    const client = sessions.get(sessionId)
    if (!client) {
      return
    }
    client.pupPage?.removeAllListeners('close')
    client.pupPage?.removeAllListeners('error')
    try {
      const pages = await client.pupBrowser.pages()
      await Promise.all(pages.map((page) => page.close()))
      await Promise.race([
        client.pupBrowser.close(),
        new Promise(resolve => setTimeout(resolve, 5000))
      ])
    } catch (e) {
      const childProcess = client.pupBrowser.process()
      if (childProcess) {
        childProcess.kill(9)
      }
    }
    sessions.delete(sessionId)
    await setupSession(sessionId)
  } catch (error) {
    logger.error({ sessionId, err: error }, 'Failed to reload session')
    throw error
  }
}

const destroySession = async (sessionId) => {
  try {
    const client = sessions.get(sessionId)
    if (!client) {
      return
    }
    client.pupPage?.removeAllListeners('close')
    client.pupPage?.removeAllListeners('error')
    try {
      await terminateWebSocketServer(sessionId)
    } catch (error) {
      logger.error({ sessionId, err: error }, 'Failed to terminate WebSocket server')
    }
    await client.destroy()
    // Wait 10 secs for client.pupBrowser to be disconnected
    let maxDelay = 0
    while (client.pupBrowser?.isConnected() && (maxDelay < 10)) {
      await new Promise(resolve => setTimeout(resolve, 1000))
      maxDelay++
    }
    sessions.delete(sessionId)
  } catch (error) {
    logger.error({ sessionId, err: error }, 'Failed to stop session')
    throw error
  }
}

const deleteSession = async (sessionId, validation) => {
  try {
    const client = sessions.get(sessionId)
    if (!client) {
      return
    }
    client.pupPage?.removeAllListeners('close')
    client.pupPage?.removeAllListeners('error')
    // cantonese.ai patch: report before the session folder (and its pairing record) is deleted.
    await reportSessionRemoved(sessionId, 'session terminated via the API').catch((err) => {
      logger.error({ sessionId, err }, 'Failed to report removal')
    })
    try {
      await terminateWebSocketServer(sessionId)
    } catch (error) {
      logger.error({ sessionId, err: error }, 'Failed to terminate WebSocket server')
    }
    if (validation.success) {
      // Client Connected, request logout
      logger.info({ sessionId }, 'Logging out session')
      await client.logout()
    } else if (validation.message === 'session_not_connected') {
      // Client not Connected, request destroy
      logger.info({ sessionId }, 'Destroying session')
      await client.destroy()
    }
    // Wait 10 secs for client.pupBrowser to be disconnected before deleting the folder
    let maxDelay = 0
    while (client.pupBrowser.isConnected() && (maxDelay < 10)) {
      await new Promise(resolve => setTimeout(resolve, 1000))
      maxDelay++
    }
    sessions.delete(sessionId)
    await deleteSessionFolder(sessionId)
  } catch (error) {
    logger.error({ sessionId, err: error }, 'Failed to delete session')
    throw error
  }
}

// Function to handle session flush
const flushSessions = async (deleteOnlyInactive) => {
  try {
    // Read the contents of the sessions folder
    const files = await fs.promises.readdir(sessionFolderPath)
    // Iterate through the files in the parent folder
    for (const file of files) {
      // Use regular expression to extract the string from the folder name
      const match = file.match(/^session-(.+)$/)
      if (match) {
        const sessionId = match[1]
        const validation = await validateSession(sessionId)
        if (!deleteOnlyInactive || !validation.success) {
          await deleteSession(sessionId, validation)
        }
      }
    }
  } catch (error) {
    logger.error(error, 'Failed to flush sessions')
    throw error
  }
}

module.exports = {
  sessions,
  setupSession,
  restoreSessions,
  validateSession,
  deleteSession,
  reloadSession,
  flushSessions,
  destroySession
}
