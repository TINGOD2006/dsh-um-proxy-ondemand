/**
 * dsh-um-proxy-ondemand —— 設定分頁的瀏覽器端 bundle。
 *
 * 這裡是手寫的：沒有打包步驟、沒有編譯器，DSH 的 client 掃描直接把這個檔案
 * 當成模組載入器（`window.__ModuleLoader__`）格式讀。改完存檔之後只要重新
 * 整理頁面就會生效；新增分頁本身才需要重啟一次 DSH。
 *
 * 只 require('react')，其餘一切照 DSH 的 settings.section 插槽慣例：
 *   ctx.slots.inject('settings.section', () => ctx.slots.register({...}, Component))
 *
 * 資料一律走同源 HTTP 打 `/um-proxy-ondemand/api/*`（宿主端在 lib/settings-api.js），
 * 不碰 RPC —— 出錯時就是一個 HTTP 狀態碼，沒有「連線服務沒掛好」的分支要猜。
 *
 * 設計約束
 * --------
 * - 非受控欄位一律從宿主回來的 `settings.fields` 生成，不在前端重寫一份欄位表：
 *   加欄位只改 lib/settings-api.js 的 FIELD_SPECS。
 * - 密碼與金鑰都只單向送出，不留在 state 超過一次請求，也不回填。
 * - 任何一個區塊失敗都只讓那個區塊顯示錯誤，不讓整個分頁變成白畫面
 *   （分頁本身還有各區塊自己的 try/catch，最外層再包一層 ErrorBoundary）。
 */
;(function () {
  if (typeof window === 'undefined') return
  var loader = window.__ModuleLoader__
  if (!loader || typeof loader.load !== 'function') return

  loader.load({
    id: 'dsh-um-proxy-ondemand',
    factory: function (require) {
      var module = { exports: {} }
      var React = require('react')
      var h = React.createElement
      var useState = React.useState
      var useEffect = React.useEffect
      var useCallback = React.useCallback
      var useRef = React.useRef

      var API = '/um-proxy-ondemand/api'
      var POLL_MS = 5000
      var MESSAGE_MS = 6000

      /* ---------------------------------------------------------------- 工具 */

      function requestJson(url, options) {
        return fetch(url, options).then(function (res) {
          return res
            .text()
            .then(function (text) {
              var data = null
              if (text) {
                try {
                  data = JSON.parse(text)
                } catch (err) {
                  data = null
                }
              }
              if (!res.ok) {
                var detail = data && data.error ? data.error : 'HTTP ' + res.status
                throw new Error(detail)
              }
              return data || {}
            })
        })
      }

      function getJson(path) {
        return requestJson(API + path, { method: 'GET', headers: { accept: 'application/json' } })
      }

      function postJson(path, body) {
        return requestJson(API + path, {
          method: 'POST',
          headers: { 'content-type': 'application/json', accept: 'application/json' },
          body: JSON.stringify(body || {}),
        })
      }

      function errorText(err) {
        if (!err) return '未知錯誤'
        return String(err.message || err)
      }

      function pad2(value) {
        return value < 10 ? '0' + value : String(value)
      }

      /** 人類可讀的剩餘時間；不知道就回 null。 */
      function humanDuration(ms) {
        if (typeof ms !== 'number' || !isFinite(ms)) return null
        var sign = ms < 0 ? '-' : ''
        var total = Math.round(Math.abs(ms) / 1000)
        var days = Math.floor(total / 86400)
        var hours = Math.floor((total % 86400) / 3600)
        var minutes = Math.floor((total % 3600) / 60)
        var seconds = total % 60
        if (days > 0) return sign + days + ' 天 ' + hours + ' 小時'
        if (hours > 0) return sign + hours + ' 小時 ' + minutes + ' 分'
        if (minutes > 0) return sign + minutes + ' 分 ' + seconds + ' 秒'
        return sign + seconds + ' 秒'
      }

      function humanTime(ms) {
        if (typeof ms !== 'number' || !isFinite(ms)) return null
        var date = new Date(ms)
        return (
          date.getFullYear() +
          '-' +
          pad2(date.getMonth() + 1) +
          '-' +
          pad2(date.getDate()) +
          ' ' +
          pad2(date.getHours()) +
          ':' +
          pad2(date.getMinutes())
        )
      }

      /* ------------------------------------------------------------------ CSS */

      var CSS = [
        '.umpod_section{display:flex;flex-direction:column;gap:16px;max-width:760px;font-size:13px;color:var(--dsw-alias-label-primary,#e6e6e6)}',
        '.umpod_title{margin:0;font-size:18px;font-weight:600;line-height:1.4}',
        '.umpod_lead{margin:0;font-size:13px;line-height:1.5;color:var(--dsw-alias-label-tertiary,#9a9a9a)}',
        '.umpod_card{border:0.5px solid var(--dsw-alias-border-l3,#333);border-radius:10px;padding:14px 16px;background:var(--dsw-alias-bg-layer-2,rgba(255,255,255,0.02))}',
        '.umpod_cardHead{display:flex;align-items:baseline;justify-content:space-between;gap:10px;margin:0 0 4px}',
        '.umpod_cardTitle{margin:0;font-size:14px;font-weight:600}',
        '.umpod_cardNote{margin:0 0 6px;font-size:12px;line-height:1.5;color:var(--dsw-alias-label-tertiary,#9a9a9a)}',
        '.umpod_grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px 18px}',
        '.umpod_kv{display:flex;flex-direction:column;gap:2px;min-width:0}',
        '.umpod_k{font-size:11px;letter-spacing:.02em;color:var(--dsw-alias-label-tertiary,#9a9a9a)}',
        '.umpod_v{font-size:13px;line-height:1.5;word-break:break-all}',
        '.umpod_ok{color:var(--dsw-alias-state-success-primary,#4ade80)}',
        '.umpod_warn{color:var(--dsw-alias-state-warn-primary,#fbbf24)}',
        '.umpod_err{color:var(--dsw-alias-state-error-primary,#f87171)}',
        '.umpod_field{display:flex;flex-direction:column;gap:6px;padding:12px 0;border-top:0.5px solid var(--dsw-alias-border-l2,#2a2a2a)}',
        '.umpod_field:first-of-type{border-top:0;padding-top:2px}',
        '.umpod_label{font-size:13px;font-weight:500;line-height:1.4}',
        '.umpod_hint{font-size:12px;line-height:1.5;color:var(--dsw-alias-label-tertiary,#9a9a9a)}',
        '.umpod_input{height:34px;padding:0 12px;border:0.5px solid var(--dsw-alias-border-l4,#3a3a3a);border-radius:8px;background:var(--dsw-alias-bg-layer-1,rgba(0,0,0,0.15));color:inherit;font-size:13px;font-family:inherit;width:100%;box-sizing:border-box}',
        '.umpod_input:focus{outline:none;border-color:var(--dsw-alias-brand-primary,#4d6bfe)}',
        '.umpod_input[disabled]{opacity:.5}',
        '.umpod_checkRow{display:flex;align-items:center;gap:8px}',
        '.umpod_actions{display:flex;align-items:center;gap:10px;flex-wrap:wrap;padding-top:12px;border-top:0.5px solid var(--dsw-alias-border-l2,#2a2a2a)}',
        '.umpod_btn{border:0;border-radius:8px;padding:5px 14px;font-size:13px;font-family:inherit;cursor:pointer;background:var(--dsw-alias-label-primary,#e6e6e6);color:var(--dsw-alias-bg-layer-1,#111)}',
        '.umpod_btn:disabled{opacity:.4;cursor:default}',
        '.umpod_btnGhost{background:transparent;border:0.5px solid var(--dsw-alias-border-l4,#3a3a3a);color:inherit}',
        '.umpod_msg{font-size:12px;line-height:1.5}',
        '.umpod_mono{font-family:ui-monospace,Menlo,Consolas,monospace;font-size:12px}',
      ].join('')

      function injectCss() {
        if (typeof document === 'undefined' || !document.head) return function () {}
        var marker = 'dsh-um-proxy-ondemand'
        var existing = document.querySelector('style[data-plugin-css="' + marker + '"]')
        if (existing) {
          existing.textContent = CSS
          return function () {}
        }
        var style = document.createElement('style')
        style.setAttribute('data-plugin-css', marker)
        style.textContent = CSS
        document.head.appendChild(style)
        return function () {
          if (style.parentNode) style.parentNode.removeChild(style)
        }
      }

      /* ------------------------------------------------------- 小型呈現元件 */

      function Card(props) {
        return h(
          'section',
          { className: 'umpod_card' },
          h(
            'div',
            { className: 'umpod_cardHead' },
            h('h3', { className: 'umpod_cardTitle' }, props.title),
            props.aside ? h('span', { className: 'umpod_msg' }, props.aside) : null,
          ),
          props.note ? h('p', { className: 'umpod_cardNote' }, props.note) : null,
          props.children,
        )
      }

      function KV(props) {
        return h(
          'div',
          { className: 'umpod_kv' },
          h('span', { className: 'umpod_k' }, props.label),
          h('span', { className: 'umpod_v' + (props.tone ? ' ' + props.tone : '') }, props.value),
        )
      }

      function Message(props) {
        if (!props.text) return null
        return h('span', { className: 'umpod_msg ' + (props.tone || '') }, props.text)
      }

      /** 一次請求的共用小工具：忙、錯誤、訊息。 */
      function useSubmit() {
        var busyState = useState(false)
        var busy = busyState[0]
        var setBusy = busyState[1]
        var errorState = useState(null)
        var error = errorState[0]
        var setError = errorState[1]

        var run = useCallback(function (task, onDone) {
          setBusy(true)
          setError(null)
          var pending = task()
          return Promise.resolve(pending)
            .then(function (result) {
              setBusy(false)
              if (onDone) onDone(result)
              return result
            })
            .catch(function (err) {
              setBusy(false)
              setError(errorText(err))
              return undefined
            })
        }, [])

        return { busy: busy, error: error, setError: setError, run: run }
      }

      /* ------------------------------------------------------------ 狀態面板 */

      function StatusPanel(props) {
        var data = props.data
        var status = (data && data.status) || {}
        var token = (data && data.token) || { present: false }
        var key = (data && data.key) || {}
        var account = (data && data.account) || {}

        var running = Boolean(status.running)
        var mode = status.alwaysOn ? '常駐' : '閒置關閉'
        var tokenValue = '沒有 token'
        var tokenTone = 'umpod_err'
        if (token.present) {
          if (typeof token.remainingMs === 'number') {
            var left = humanDuration(token.remainingMs)
            tokenValue = (token.expired ? '已過期 ' : '剩 ') + left
            tokenTone = token.expired ? 'umpod_err' : token.remainingMs < 30 * 60 * 1000 ? 'umpod_warn' : 'umpod_ok'
          } else {
            tokenValue = '存在（讀不到到期時間）'
            tokenTone = ''
          }
        }

        var keyValue = '未知'
        var keyTone = ''
        if (key.available === false) {
          keyValue = '宿主沒有 credentials 服務'
          keyTone = 'umpod_warn'
        } else if (key.configured) {
          keyValue = key.source ? '已設定（來源 ' + key.source + '）' : '已設定'
          keyTone = key.writable === false ? 'umpod_warn' : 'umpod_ok'
        } else {
          keyValue = '未設定'
          keyTone = 'umpod_warn'
        }

        var stats = status.stats || {}
        var statsValue = [
          '請求 ' + (stats.requests || 0),
          '喚醒 ' + (stats.wakes || 0),
          '冷啟 ' + (stats.coldStarts || 0),
          '閒置停 ' + (stats.idleStops || 0),
        ].join(' · ')

        return h(
          Card,
          {
            title: '狀態',
            note: '唯讀，每 ' + POLL_MS / 1000 + ' 秒自動更新一次。',
            aside: props.error ? h(Message, { text: props.error, tone: 'umpod_err' }) : null,
          },
          h(
            'div',
            { className: 'umpod_grid' },
            h(KV, { label: '代理', value: running ? '執行中' : '未執行', tone: running ? 'umpod_ok' : 'umpod_warn' }),
            h(KV, { label: '模式', value: mode }),
            h(KV, { label: '埠號', value: String(status.port || '—') }),
            h(KV, { label: 'PID', value: status.pid ? String(status.pid) : '—' }),
            h(KV, { label: '進行中請求', value: String(status.inFlightRequests || 0) }),
            h(KV, { label: 'Token', value: tokenValue, tone: tokenTone }),
            h(KV, { label: 'UM_GENAI_API_KEY', value: keyValue, tone: keyTone }),
            h(KV, {
              label: 'UM 帳號',
              value: account.configured ? '已儲存' : '未儲存',
              tone: account.configured ? 'umpod_ok' : 'umpod_warn',
            }),
            h(KV, { label: '累計', value: statsValue }),
            h(KV, {
              label: '最近一次錯誤',
              value: status.lastError ? String(status.lastError).slice(-160) : '無',
              tone: status.lastError ? 'umpod_err' : '',
            }),
          ),
          data && data.host
            ? h(
                'p',
                { className: 'umpod_cardNote umpod_mono', style: { marginTop: 12, marginBottom: 0 } },
                'UM 家目錄：' + data.host.home,
              )
            : null,
        )
      }

      /* -------------------------------------------------------------- 一般設定 */

      function draftFrom(current, fields) {
        var draft = {}
        for (var i = 0; i < fields.length; i += 1) {
          var field = fields[i]
          var value = current ? current[field.key] : undefined
          if (field.kind === 'boolean') draft[field.key] = value === true
          else draft[field.key] = value === undefined || value === null ? '' : String(value)
        }
        return draft
      }

      function comparable(field, value) {
        if (field.kind === 'boolean') return value === true ? 'true' : 'false'
        return value === undefined || value === null ? '' : String(value)
      }

      function SettingsCard(props) {
        var settings = props.settings
        var fields = settings.fields || []
        var current = settings.current || {}

        var draftState = useState(function () {
          return draftFrom(current, fields)
        })
        var draft = draftState[0]
        var setDraft = draftState[1]
        var dirtyState = useState(false)
        var dirty = dirtyState[0]
        var setDirty = dirtyState[1]
        var savedState = useState(null)
        var saved = savedState[0]
        var setSaved = savedState[1]
        var submit = useSubmit()

        var signature = JSON.stringify(current)
        var hydratedRef = useRef('')
        useEffect(
          function () {
            if (!hydratedRef.current) hydratedRef.current = signature
            if (dirty) return
            // 宿主送來新的有效值（例如熱重載完成）才覆蓋草稿
            if (hydratedRef.current === signature) return
            hydratedRef.current = signature
            setDraft(draftFrom(current, fields))
          },
          [signature, dirty],
        )

        useEffect(
          function () {
            if (!saved) return undefined
            var timer = setTimeout(function () {
              setSaved(null)
            }, MESSAGE_MS)
            return function () {
              clearTimeout(timer)
            }
          },
          [saved],
        )

        var update = function (key, value) {
          setDraft(function (previous) {
            var next = {}
            for (var name in previous) {
              if (Object.prototype.hasOwnProperty.call(previous, name)) next[name] = previous[name]
            }
            next[key] = value
            return next
          })
          setDirty(true)
          setSaved(null)
        }

        var save = function () {
          var patch = {}
          var count = 0
          for (var i = 0; i < fields.length; i += 1) {
            var field = fields[i]
            if (comparable(field, draft[field.key]) === comparable(field, current[field.key])) continue
            patch[field.key] =
              field.kind === 'boolean' ? draft[field.key] === true : String(draft[field.key] == null ? '' : draft[field.key]).trim()
            count += 1
          }
          if (count === 0) {
            setSaved('沒有變更')
            return
          }
          submit.run(
            function () {
              return postJson('/settings', { values: patch })
            },
            function () {
              setDirty(false)
              setSaved('已儲存 ' + count + ' 項，插件正在重載…')
              if (props.onSaved) props.onSaved()
            },
          )
        }

        var reset = function () {
          setDraft(draftFrom(current, fields))
          setDirty(false)
          setSaved(null)
          submit.setError(null)
        }

        var rows = fields.map(function (field) {
          var id = 'umpod-f-' + field.key
          var value = draft[field.key]
          var overridden = (settings.overridden || []).indexOf(field.key) >= 0
          var control
          if (field.kind === 'boolean') {
            control = h(
              'label',
              { className: 'umpod_checkRow', htmlFor: id },
              h('input', {
                id: id,
                type: 'checkbox',
                checked: value === true,
                onChange: function (event) {
                  update(field.key, event.target.checked)
                },
              }),
              h('span', { className: 'umpod_hint' }, value === true ? '開啟' : '關閉'),
            )
          } else {
            control = h('input', {
              id: id,
              className: 'umpod_input',
              type: field.kind === 'integer' ? 'number' : 'text',
              inputMode: field.kind === 'integer' ? 'numeric' : undefined,
              min: field.min,
              max: field.max,
              value: value,
              spellCheck: false,
              autoComplete: 'off',
              onChange: function (event) {
                update(field.key, event.target.value)
              },
            })
          }
          return h(
            'div',
            { className: 'umpod_field', key: field.key },
            h(
              'label',
              { className: 'umpod_label', htmlFor: id },
              field.label,
              overridden ? h('span', { className: 'umpod_hint' }, '（已自訂）') : null,
            ),
            control,
            field.hint ? h('span', { className: 'umpod_hint' }, field.hint) : null,
          )
        })

        return h(
          Card,
          {
            title: '一般設定',
            note: '存檔會寫進 profile 的 cordis.patch.yml，再由 DSH 熱重載；不需要重開。留白＝移除該項覆寫、回到預設值。',
            aside: dirty ? h(Message, { text: '有未儲存的變更', tone: 'umpod_warn' }) : null,
          },
          rows,
          h(
            'div',
            { className: 'umpod_actions' },
            h(
              'button',
              { className: 'umpod_btn', type: 'button', disabled: submit.busy, onClick: save },
              submit.busy ? '儲存中…' : '儲存設定',
            ),
            h(
              'button',
              { className: 'umpod_btn umpod_btnGhost', type: 'button', disabled: submit.busy || !dirty, onClick: reset },
              '還原',
            ),
            h(Message, { text: submit.error, tone: 'umpod_err' }),
            h(Message, { text: saved, tone: 'umpod_ok' }),
          ),
        )
      }

      /* ---------------------------------------------------------------- 帳密 */

      function AccountCard(props) {
        var account = (props.data && props.data.account) || {}
        var usernameState = useState('')
        var username = usernameState[0]
        var setUsername = usernameState[1]
        var passwordState = useState('')
        var password = passwordState[0]
        var setPassword = passwordState[1]
        var savedState = useState(null)
        var saved = savedState[0]
        var setSaved = savedState[1]
        var submit = useSubmit()

        useEffect(
          function () {
            if (!saved) return undefined
            var timer = setTimeout(function () {
              setSaved(null)
            }, MESSAGE_MS)
            return function () {
              clearTimeout(timer)
            }
          },
          [saved],
        )

        var save = function () {
          submit.run(
            function () {
              return postJson('/account', { username: username, password: password })
            },
            function () {
              setUsername('')
              setPassword('')
              setSaved('已交給 UM 工具加密存檔')
              if (props.onSaved) props.onSaved()
            },
          )
        }

        var ready = username.trim() !== '' && password !== ''

        return h(
          Card,
          {
            title: 'UM 帳號',
            note:
              'UM 網頁版的登入帳密。只經由 stdin 交給 um_opencode_setup.py，由它以 Windows DPAPI 加密存進 credentials.dat；DSH 不保存、不回讀、不寫進日誌。',
            aside: h(Message, {
              text: account.configured ? '已儲存' : '未儲存',
              tone: account.configured ? 'umpod_ok' : 'umpod_warn',
            }),
          },
          h(
            'div',
            { className: 'umpod_field' },
            h('label', { className: 'umpod_label', htmlFor: 'umpod-account-user' }, '帳號'),
            h('input', {
              id: 'umpod-account-user',
              className: 'umpod_input',
              type: 'text',
              value: username,
              spellCheck: false,
              autoComplete: 'username',
              onChange: function (event) {
                setUsername(event.target.value)
                setSaved(null)
              },
            }),
          ),
          h(
            'div',
            { className: 'umpod_field' },
            h('label', { className: 'umpod_label', htmlFor: 'umpod-account-pass' }, '密碼'),
            h('input', {
              id: 'umpod-account-pass',
              className: 'umpod_input',
              type: 'password',
              value: password,
              autoComplete: 'current-password',
              onChange: function (event) {
                setPassword(event.target.value)
                setSaved(null)
              },
            }),
            h(
              'span',
              { className: 'umpod_hint' },
              '存好之後，續期腳本在 token 過期時才有辦法自動登入；需要 2FA 的帳號仍會跳出登入視窗請你確認。',
            ),
          ),
          h(
            'div',
            { className: 'umpod_actions' },
            h(
              'button',
              { className: 'umpod_btn', type: 'button', disabled: submit.busy || !ready, onClick: save },
              submit.busy ? '送出中…' : '儲存帳密',
            ),
            h(Message, { text: submit.error, tone: 'umpod_err' }),
            h(Message, { text: saved, tone: 'umpod_ok' }),
          ),
        )
      }

      /* -------------------------------------------------------------- 金鑰 */

      function KeyCard(props) {
        var key = (props.data && props.data.key) || {}
        var valueState = useState('')
        var value = valueState[0]
        var setValue = valueState[1]
        var savedState = useState(null)
        var saved = savedState[0]
        var setSaved = savedState[1]
        var submit = useSubmit()

        useEffect(
          function () {
            if (!saved) return undefined
            var timer = setTimeout(function () {
              setSaved(null)
            }, MESSAGE_MS)
            return function () {
              clearTimeout(timer)
            }
          },
          [saved],
        )

        var unavailable = key.available === false
        var readonly = key.writable === false
        var disabled = unavailable || readonly || submit.busy

        var save = function () {
          submit.run(
            function () {
              return postJson('/api-key', { value: value.trim() })
            },
            function () {
              setValue('')
              setSaved('已更新')
              if (props.onSaved) props.onSaved()
            },
          )
        }

        var stateText = '未設定'
        var stateTone = 'umpod_warn'
        if (unavailable) {
          stateText = '無此服務'
          stateTone = 'umpod_err'
        } else if (key.configured) {
          stateText = '已設定'
          stateTone = 'umpod_ok'
        }

        return h(
          Card,
          {
            title: '代理金鑰（' + (key.ref || 'UM_GENAI_API_KEY') + '）',
            note:
              'DSH 的 provider 用這把金鑰連本地代理。值就存在 DSH 的憑證庫（.credentials.yaml），可以被覆寫；留白不會清除，要換就貼新的。',
            aside: h(Message, { text: stateText, tone: stateTone }),
          },
          h(
            'div',
            { className: 'umpod_field' },
            h('label', { className: 'umpod_label', htmlFor: 'umpod-api-key' }, '新金鑰'),
            h('input', {
              id: 'umpod-api-key',
              className: 'umpod_input umpod_mono',
              type: 'password',
              value: value,
              spellCheck: false,
              autoComplete: 'off',
              disabled: disabled,
              placeholder: readonly ? '由啟動環境的環境變數提供，唯讀' : unavailable ? '宿主沒有 credentials 服務' : '貼上新的金鑰',
              onChange: function (event) {
                setValue(event.target.value)
                setSaved(null)
              },
            }),
            h(
              'span',
              { className: 'umpod_hint' },
              readonly
                ? '這把金鑰是啟動 DSH 時由環境變數帶進來的，在這裡寫不進去；要換得先清掉那個環境變數。'
                : '一般情況下它就是 ~/.um-opencode/local-api-key 的內容；安裝腳本會自動同步。',
            ),
          ),
          h(
            'div',
            { className: 'umpod_actions' },
            h(
              'button',
              { className: 'umpod_btn', type: 'button', disabled: disabled || value.trim() === '', onClick: save },
              submit.busy ? '送出中…' : '更新金鑰',
            ),
            h(Message, { text: submit.error, tone: 'umpod_err' }),
            h(Message, { text: saved, tone: 'umpod_ok' }),
          ),
        )
      }

      /* ---------------------------------------------------------------- 分頁 */

      function Section(props) {
        var dataState = useState(null)
        var data = dataState[0]
        var setData = dataState[1]
        var errorState = useState(null)
        var error = errorState[0]
        var setError = errorState[1]
        var loadingState = useState(true)
        var loading = loadingState[0]
        var setLoading = loadingState[1]
        var nonceState = useState(0)
        var nonce = nonceState[0]
        var setNonce = nonceState[1]
        var aliveRef = useRef(true)

        useEffect(
          function () {
            aliveRef.current = true
            var load = function () {
              getJson('/status')
                .then(function (next) {
                  if (!aliveRef.current) return
                  setData(next)
                  setError(null)
                  setLoading(false)
                })
                .catch(function (err) {
                  if (!aliveRef.current) return
                  setError(errorText(err))
                  setLoading(false)
                })
            }
            load()
            var timer = setInterval(load, POLL_MS)
            return function () {
              aliveRef.current = false
              clearInterval(timer)
            }
          },
          [nonce],
        )

        var reload = useCallback(function () {
          setNonce(function (value) {
            return value + 1
          })
        }, [])

        var body
        if (loading && !data) {
          body = h(Card, { title: '狀態' }, h('span', { className: 'umpod_hint' }, '讀取中…'))
        } else if (!data) {
          body = h(
            Card,
            { title: '無法讀取狀態' },
            h(Message, { text: error || '宿主沒有回應', tone: 'umpod_err' }),
            h(
              'div',
              { className: 'umpod_actions' },
              h('button', { className: 'umpod_btn', type: 'button', onClick: reload }, '重試'),
            ),
          )
        } else {
          body = h(
            React.Fragment,
            null,
            h(StatusPanel, { data: data, error: error }),
            h(SettingsCard, { settings: data.settings || {}, onSaved: reload }),
            h(AccountCard, { data: data, onSaved: reload }),
            h(KeyCard, { data: data, onSaved: reload }),
          )
        }

        return h(
          'div',
          { className: 'umpod_section' },
          h('h2', { className: 'umpod_title' }, 'UM 代理'),
          h(
            'p',
            { className: 'umpod_lead' },
            '本機的 UM 反向代理：DSH 的 um-genai provider 經它轉發。這裡可以看狀態、調整啟動參數、以及補上登入需要的帳密。',
          ),
          body,
        )
      }

      /* ----------------------------------------------------------- 錯誤邊界 */

      function ErrorBoundary(props) {
        var state = useState(null)
        var error = state[0]
        var setError = state[1]
        if (error) {
          return h(
            'div',
            { className: 'umpod_section' },
            h('h2', { className: 'umpod_title' }, 'UM 代理'),
            h(Card, { title: '設定頁發生錯誤' }, h(Message, { text: errorText(error), tone: 'umpod_err' })),
          )
        }
        return h(ErrorBoundaryInner, { onError: setError }, props.children)
      }

      function ErrorBoundaryInner(props) {
        try {
          return props.children
        } catch (err) {
          props.onError(err)
          return null
        }
      }

      /* -------------------------------------------------------------- 掛載 */

      function apply(ctx) {
        if (typeof ctx.effect === 'function') ctx.effect(injectCss)
        ctx.slots.inject('settings.section', function () {
          return ctx.slots.register(
            {
              name: 'settings.section',
              id: 'um-proxy-ondemand',
              order: 48,
              label: function () {
                return 'UM 代理'
              },
            },
            function (slotProps) {
              return h(ErrorBoundary, null, h(Section, slotProps || {}))
            },
          )
        })
      }

      module.exports = {
        name: 'dsh-um-proxy-ondemand',
        inject: ['slots'],
        apply: apply,
      }
      return module.exports
    },
  })
})()
