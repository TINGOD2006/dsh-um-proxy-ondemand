/**
 * 熱路徑效率優化的純單元測試：不寫檔、不起真實代理、不碰 8788。
 *
 * 需要真的喚醒代理的驗證在 test/ondemand_check.mjs（那支會重啟 8788，跑之前
 * 要先確認沒有會話正在用它）。這裡只驗「決策邏輯」：
 *   - single-flight 是否真的把並行呼叫合併成一次
 *   - 輪詢排程是否前密後疏、不會比舊版多睡
 *   - 狀態檔節流視窗
 *   - healthDetail 是否能分辨「沒人在聽」與「有人但不是我們」
 *
 * 執行：node test/efficiency_unit.mjs
 */
import assert from 'node:assert/strict'
import http from 'node:http'
import { createServer } from 'node:net'
import os from 'node:os'
import path from 'node:path'

import {
  ensureProxy,
  healthDetail,
  inflightCount,
  isRecoverableWakeError,
  planStateWrite,
  pollDelay,
  singleFlight,
  STARTUP_POLL_DELAYS,
  STATE_WRITE_MIN_INTERVAL_MS,
  wakeRetryCount,
} from '../lib/proxy.js'

const tests = []
const test = (name, fn) => tests.push([name, fn])

/** 要一個「現在確定沒有人在聽」的埠。 */
async function closedPort() {
  const probe = createServer()
  await new Promise((resolve) => probe.listen(0, '127.0.0.1', resolve))
  const { port } = probe.address()
  await new Promise((resolve) => probe.close(resolve))
  return port
}

test('pollDelay：前段密、後段放大，尾端固定 250ms', () => {
  const values = Array.from({ length: STARTUP_POLL_DELAYS.length + 5 }, (_, i) => pollDelay(i))
  assert.equal(values[0], 20, '第一次探測間隔要小於舊版的 250ms')
  for (let i = 1; i < values.length; i += 1) {
    assert.ok(values[i] >= values[i - 1], `第 ${i} 檔不該比前一檔短：${values[i - 1]} → ${values[i]}`)
  }
  assert.equal(pollDelay(STARTUP_POLL_DELAYS.length - 1), 250, '排程最後一檔保持舊版節奏')
  assert.equal(pollDelay(999), 250, '超過排程長度就固定用最後一檔')
  assert.ok(Math.max(...values) <= 250, '不該比舊版 250ms 睡更久')
})

test('singleFlight：同一 key 的並行呼叫只執行一次，回傳同一個結果', async () => {
  let calls = 0
  const first = singleFlight('unit:k1', async () => {
    calls += 1
    await new Promise((resolve) => setTimeout(resolve, 20))
    return 'winner'
  })
  const second = singleFlight('unit:k1', async () => {
    calls += 1
    return 'loser'
  })
  assert.equal(inflightCount(), 1, '進行中應該只有一個')
  const [a, b] = await Promise.all([first, second])
  assert.equal(calls, 1, '併發進來時只能 spawn 一次')
  assert.equal(a, 'winner')
  assert.equal(b, 'winner', '後到的呼叫要拿到領先者的結果')
  assert.equal(inflightCount(), 0, '完成後要清掉 map')
})

test('singleFlight：失敗後清掉 map，下一次可以重試', async () => {
  let calls = 0
  await assert.rejects(
    singleFlight('unit:k2', async () => {
      calls += 1
      throw new Error('boom')
    }),
    /boom/,
  )
  assert.equal(inflightCount(), 0, '失敗也要清掉，否則之後永遠拿到同一個 reject')
  const value = await singleFlight('unit:k2', async () => {
    calls += 1
    return 'retried'
  })
  assert.equal(value, 'retried')
  assert.equal(calls, 2)
})

test('singleFlight：完成後再呼叫會重新執行（不能變成永久快取）', async () => {
  let calls = 0
  await singleFlight('unit:k3', async () => {
    calls += 1
    return calls
  })
  await singleFlight('unit:k3', async () => {
    calls += 1
    return calls
  })
  assert.equal(calls, 2)
})

test('planStateWrite：15 秒視窗內不重複落盤，超過才寫', () => {
  const now = 1_000_000
  assert.equal(planStateWrite(undefined, now), true, '第一次一定要寫')
  assert.equal(planStateWrite(now - STATE_WRITE_MIN_INTERVAL_MS + 1, now), false, '視窗內跳過')
  assert.equal(planStateWrite(now - STATE_WRITE_MIN_INTERVAL_MS, now), true, '剛好到期要寫')
  assert.equal(planStateWrite(now, now + 1, 1), true, '自訂視窗可縮短')
})

test('healthDetail：埠上沒人監聽時回 refused（熱路徑可以省掉 netstat）', async () => {
  const port = await closedPort()
  const detail = await healthDetail(port, 500)
  assert.deepEqual(detail, { ok: false, refused: true })
})

test('healthDetail：有服務但不是 UM 代理時不算 ok、也不算 refused', async () => {
  const server = http.createServer((req, res) => res.end('{"service":"someone-else"}'))
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve))
  const { port } = server.address()
  try {
    const detail = await healthDetail(port, 1000)
    assert.deepEqual(detail, { ok: false, refused: false }, '要讓呼叫端去 netstat 查 squatter')
  } finally {
    await new Promise((resolve) => server.close(resolve))
  }
})

test('healthDetail：service 相符才算我們的代理活著', async () => {
  const server = http.createServer((req, res) => res.end('{"status":"ok","service":"um-opencode-proxy"}'))
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve))
  const { port } = server.address()
  try {
    assert.deepEqual(await healthDetail(port, 1000), { ok: true, refused: false })
  } finally {
    await new Promise((resolve) => server.close(resolve))
  }
})

test('isRecoverableWakeError：只重試自家代理冷啟動逾時', () => {
  assert.equal(
    isRecoverableWakeError(new Error('proxy on port 8788 did not become healthy within 20000 ms')),
    true,
  )
  assert.equal(
    isRecoverableWakeError(
      new Error('port 8788 is held by pid 1234 but /health is not the UM proxy; refusing to start'),
    ),
    false,
  )
  assert.equal(isRecoverableWakeError(new Error('spawn C:\\Tools\\Python312\\pythonw.exe ENOENT')), false)
  assert.equal(isRecoverableWakeError(undefined), false)
})

test('wakeRetryCount：一開始是 0（重試次數可觀測）', () => {
  assert.equal(typeof wakeRetryCount(), 'number')
  assert.equal(wakeRetryCount(), 0)
})

test('ensureProxy：腳本不存在時給出可操作的錯誤，而不是 spawn 失敗', async () => {
  const tmp = os.tmpdir()
  await assert.rejects(
    () =>
      ensureProxy({
        port: 63999,
        script: path.join(tmp, 'um-test-missing', 'um_opencode_setup.py'),
        stateFile: path.join(tmp, 'um-test-missing.state.json'),
        logFile: path.join(tmp, 'um-test-missing.log'),
      }),
    err => {
      assert.match(err.message, /proxy script not found/)
      assert.match(err.message, /um_opencode_setup\.py/)
      assert.match(err.message, /UM_OPENCODE_HOME/)
      return true
    },
  )
})

let failed = 0
for (const [name, fn] of tests) {
  try {
    await fn()
    console.log(`ok   ${name}`)
  } catch (err) {
    failed += 1
    console.error(`FAIL ${name}`)
    console.error(String((err && err.stack) || err))
  }
}

console.log(`\n${tests.length - failed}/${tests.length} passed`)
if (failed > 0) process.exitCode = 1
