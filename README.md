# dsh-um-proxy-ondemand

讓 UM GenAI 本機代理（`http://127.0.0.1:8788`）**只在你真的用到它的時候才活著**。

DSH 的 `um-genai` provider 指向這個代理；以前它靠一個「登入時啟動 ＋ 每 2 分鐘保活」
的排程工作常駐，即使整天沒用到該模型也一直佔著一個 Python 行程（以及它自己的
headless Chrome）。這個外掛把它改成 **按需喚醒 ＋ 閒置 15 分鐘自動關閉**。

## 它怎麼運作

```
你在 DSH 選了 UM GenAI 的模型送出訊息
        │
        ▼
llm/stream 瀑布事件（options.provider === 'um-genai'）
        │
        ├─ 不是 um-genai → return next()，完全不碰代理
        │
        └─ 是 um-genai   → 先 ensureProxy()（冷啟動並等 /health 通過）
                          再把 next() 的串流原封不動 yield* 出去
        │
        ▼
閒置 15 分鐘（沒有新請求）→ stopProxy() 關掉代理
DSH 關閉 → 順手關掉代理（不留孤兒行程）
```

關鍵設計取捨：

| 決定 | 為什麼 |
| --- | --- |
| 掛 `llm/stream` 瀑布事件 | `options.provider` 是唯一能精準判斷「這次要用哪個 provider」的時點；hooks 橋接只看得到會話/提示詞，無法區分模型。 |
| 回傳 async generator，在裡面才 `await ensureProxy()` | 瀑布事件必須回傳 `AsyncIterable<StreamChunk>`。把監聽器寫成 async 函式會回傳 Promise，是否被上游 await 屬 DSH 內部實作，不該賭。 |
| 依 `options.provider` 而非 model id 判斷 | provider 名稱穩定；模型清單會變（`DeepSeek-V4.1-Flash`、`Qwen3.5-397B-A17B`…）。 |
| 用 `/health` 的 `service == "um-opencode-proxy"` 確認所有權 | 才敢 `taskkill`。埠上若是別的服務，寧可報錯也不誤殺。 |
| 用自己的 state 檔，不用 `proxy.pid` | `proxy.pid` 是 8787 的 `um_windows.py ensure` 在用的單一檔案，共用會互相覆蓋。 |
| 停止時殺**所有**監聽者 | Windows 的 `SO_REUSEADDR` 加上 Python `HTTPServer.allow_reuse_address=1`，允許兩個行程同時 LISTENING 同一個埠（舊的保活排程就製造過這種情況）。 |
| 停止時以 netstat 的實際監聽者為準，不無條件信 `state.pid` | Windows 會回收 pid；舊狀態檔裡的 pid 可能已被無關行程接手，照殺就是誤殺。只有在 netstat 查不到時才退回 `state.pid`。 |
| 冷啟動時 `detached + windowsHide`，對象是 `pythonw.exe` | GUI 子系統 ＋ `DETACHED_PROCESS` ⇒ 不產生任何主控台視窗（已實測，見下）。 |

## 效率：熱路徑與喚醒延遲

原本的版本每個請求都會做一次 `/health`（正確但也最貴的地方在**冷喚醒**與**同步 netstat**）。
以下是實測後的優化，數字都是本機量到的（Python 3.14 已快取、代理在 143ms 內可服務）：

| 項目 | 舊版 | 現在 | 說明 |
| --- | --- | --- | --- |
| 冷喚醒（spawn → 可服務） | 281–344 ms | **177 ms** | 輪詢排程由固定 250ms 改成 20→250ms 前密後疏，且 spawn 後先立刻探一次。 |
| 並行冷啟動 | 每個請求各 spawn 一次（實測 2 個 python，1 個 bind 失敗退出） | **只 spawn 1 次** | 同埠 `singleFlight()`：進行中的喚醒共用同一個 promise。 |
| 熱路徑（代理已活著） | 1 次 health ＋ 同步 `netstat`（~31ms，卡 event loop）＋ 每請求同步寫 state | **1 次 health（0.25ms）** | `healthDetail()` 分辨 `ECONNREFUSED`＝真的沒人聽 ⇒ 直接 spawn，省掉 netstat；只有「有回應但不是我們的代理」才去查誰佔著埠。 |
| 狀態檔寫入 | 每請求同步寫一次 | 記憶體最新，磁碟最多每 15s | 喚醒、停止、DSH 關閉時 `flushState()` 立刻落盤。 |

其餘刻意**不做**的取捨：`/health` 用 keep-alive agent 實測沒有增益（0.25 對 0.25ms）；
health 快取（例如 2 秒內跳過）能再省 0.3ms，但代理若剛好死掉會讓請求直接失敗而不是自動冷啟動，不值得。

另外，`C:\Python314\python.exe` 的直譯器啟動就佔了冷喚醒的大半——那是代理本身的成本，外掛只能把「其實已經好了卻還在等」的空窗拿掉。

## 檔案

| 檔案 | 作用 |
| --- | --- |
| `package.json` | DSH bundle 契約（`type: module`、`exports`、`dsh.bundle.patch`）。 |
| `cordis.patch.yml` | 插入條目與設定（`port: 8788`、`idleMinutes: 15`）。 |
| `lib/proxy.js` | 代理生命週期：`healthDetail` / `health` / `ensureProxy`（single-flight） / `stopProxy` / `listeningPids`、狀態檔節流。不依賴 cordis。 |
| `lib/index.js` | 事件接線（`llm/stream`）、閒置計時器、唯讀狀態端點、啟動清理與關閉清理。 |
| `test/ondemand_check.mjs` | 生命週期：冷啟動、沿用、停止、冪等、重複監聽。 |
| `test/plugin_wiring.mjs` | 用 mock ctx 跑真的 `apply()`：非目標 provider 零成本放行、目標 provider 喚醒並轉送串流。 |
| `test/idle_timeout.mjs` | 閒置自動關閉（用 `idleSeconds: 3` 壓縮週期）。 |
| `test/coldstart.mjs` | 冷啟動計時，供無視窗驗證使用。 |
| `test/efficiency_unit.mjs` | 純單元測試（不寫檔、不起代理）：single-flight、輪詢排程、狀態檔節流、`healthDetail` 判定。 |
| `scripts/um-recover.ps1` | 反代自救腳本：診斷 401（上游 UM token 過期 vs `local-api-key` 不對），可 `-Fix` 自動重新登入。 |
| `scripts/um-renew.py` | 續期看門狗（獨立於代理）：每 5 分鐘檢查 JWT `exp` **和 gateway 是否真的接受 token**，需要時自動跑 `login`、清掉殘留的登入 Chrome，並在續期後實測新 token。 |

執行測試（前三支會實際啟停 8788 代理）：

```powershell
cd C:\Users\TINGOD\Documents\deepseek-harness\default-workspace\dsh-um-proxy-ondemand
node test/efficiency_unit.mjs   # 安全：不碰 8788
node test/ondemand_check.mjs
node test/plugin_wiring.mjs
node test/idle_timeout.mjs
```

> 前三支生命週期測試會 `taskkill` 掉 8788 上的代理。若正在用 UM GenAI 模型對話，先把 provider
> 切成別的（或等對話結束）再跑，否則那一輪請求會斷。

## 設定

`cordis.patch.yml`：

```yaml
- insert:
    - id: dsh-um-proxy-ondemand
      name: dsh-um-proxy-ondemand
      config:
        port: 8788
        idleMinutes: 15
```

可用欄位：`port`、`idleMinutes`、`idleSeconds`（測試用短週期覆寫）、`provider`
（預設比對 `um-genai`）、`python`、`script`、`logFile`、`stateFile`、`startupTimeoutMs`。

## 怎麼確認它在運作

唯讀狀態端點（DSH 開著時）。埠是 DSH 自己那個網頁埠，**每次啟動可能不同**（把下面 `14639`
換成你瀏覽器上 DSH 的埠即可）：

```powershell
Invoke-RestMethod http://127.0.0.1:14639/um-proxy-ondemand/state.json
```

會回報 `running`、`pid`、`inFlightRequests`、`inFlightWakes`、`wakeRetries`、`idleTimerArmed`、
`stats`（`requests`／`wakes`／`coldStarts`／`idleStops`／`wakeFailures`）與 `lastError`。

`wakeRetries` 是「冷啟動失敗後補救重試」的累計次數。外掛等代理變健康等到逾時時，會先確認是不是
「埠被 bind 住但 `/health` 不回應」的半死行程（實測 `pythonw` 出現過一次），有的話殺掉再試**一次**；
「埠被別人的服務佔著」或 python 路徑錯（ENOENT）則直接報錯、不重試。正常情況下這個數字應該一直是 `0`。

看日誌：DSH 的 host 記錄裡搜 `dsh-um-proxy-ondemand:`，會看到
`woke proxy (pid N) for provider um-genai` 與 `idle 15 min → proxy stopped (pid N)`。

## 反代 401：上游 UM 登入 token 過期

**症狀**：代理行程活著、`/health` 也 200，但每次對話請求都是 401；`~\.um-opencode\proxy.log` 反覆出現：

```text
UM token was rejected; trying a silent refresh...
automatic re-login failed (no stored credentials, a rejected password, or a two-factor prompt nobody approved).
could not open the UM login window automatically (no debug port answered). Run 'refresh' to log in by hand.
auto-login timed out.
```

**原因**：UM 的登入 JWT 只有 **9 小時**效期（`~\.um-opencode\upstream-token` 的 `exp`）。代理內建
`token_watchdog`，每 300 秒用私有 Chrome profile（`~\.um-opencode\browser-profile`）靜默續期；一旦那個
profile 被一個**沒有開 debug port** 的 Chrome 實例佔住，新開的 Chrome 只會把網址轉交給舊實例就退出，
`silent_token_capture` 永遠拿不到 debug port，於是續期一路失敗。
**這不是 DSH 的 API key 問題，也不是反代壞掉。**

**一鍵診斷／修復**（`scripts\um-recover.ps1`，Windows PowerShell 5.1 即可）：

```powershell
cd C:\Users\TINGOD\Documents\deepseek-harness\default-workspace\dsh-um-proxy-ondemand

# 只診斷，不動任何東西
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\um-recover.ps1

# 診斷，且判定「上游 token 失效」時自動重新登入（會開瀏覽器，Duo 可能要在手機按核准）
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\um-recover.ps1 -Fix
```

它依序檢查 `/health` → `setup status`（上游是否接受 token）→ 帶 `local-api-key` 的 `/v1/models`，
所以能分清兩種 401：

| 檢查結果 | 意思 | 下一步 |
| --- | --- | --- |
| `status` 上游 healthy、`/v1/models` 401 | `local-api-key` 與代理不符 | 比對 `~\.um-opencode\local-api-key` 和 profile 的 `UM_GENAI_API_KEY`；重新登入沒用 |
| `status` 上游 401／非 healthy、`/v1/models` 401 | 真的是 UM 登入 token 過期 | 跑 `-Fix`，或手動 `login` |
| `/health` 沒回應 | 代理沒在跑（多半是閒置被關掉） | 在 DSH 用一次 um-genai 模型，或手動 `serve --port 8788` |

結束碼：`0` 可用／已修好、`2` 需要人工處理、`3` 代理沒在跑、`4` 環境錯誤。

- 登入成功後代理**不用重啟**，下一個請求就會拿到新 token（log 會出現 `token renewed on demand.`）。
- 手動等價指令：`C:\Python314\python.exe C:\Users\TINGOD\.um-opencode\um_opencode_setup.py login --timeout 170`
  （`login` 不吃 `--port`）。
- 若在 DSH 的受限沙箱裡跑，login 會死在 `os.chmod('C:\Users\TINGOD\.um-opencode')`
  → `PermissionError: [WinError 5]`；請在一般 PowerShell／cmd 視窗執行。
- 想根治：登入後把那個 UM 登入視窗留著（有 debug port 可重複用）或完全關掉再讓它靜默續期；
  最怕的是「活著但沒有 debug port」的殘留 Chrome。

## token 到期自動續期（排程看門狗）

代理自己的 `token_watchdog` 只活在**代理行程裡**，而這個外掛是「按需」的：代理閒置關掉之後，
沒有人會發現 token 已經過期；等下一次請求喚醒代理，它才臨時去續期，常常已經來不及（請求先拿到 401）。

代理醒著的時候，`token_watchdog` 也不再是固定 300 秒：距到期還很久就維持 300 秒（閒著幾乎不花錢），
**剩不到 30 分鐘或已過期就改成每 60 秒探一次**，把「token 死了還在服務 401」的空窗從最多 5 分鐘壓到
1 分鐘內。可用 `UM_WATCHDOG_FAST_SECONDS`（預設 `60`）、`UM_WATCHDOG_FAST_WINDOW`（預設 `1800` 秒）
調整；純邏輯測試在 `um-opencode\test_watchdog_interval.py`（9/9）。

`scripts/um-renew.py` 就是補上這個洞的看門狗，**獨立於代理**，由 Windows 排程每 5 分鐘叫醒一次：

| 情況 | 行為 |
| --- | --- |
| JWT 還有效、距到期 > 20 分鐘，**且 gateway 實測接受** | 讀一次 JWT ＋ 一次 `GET /api/models`，靜默 `exit 0`（實測 **315 ms**，不開任何視窗） |
| 距到期 ≤ 20 分鐘、已過期、或 JWT 讀不到 | 執行 `python um_opencode_setup.py login --timeout 120`，把過程寫進 log |
| **JWT 沒到期但 gateway 回 401／403** | 一樣當作需要續期（見下節：JWT `exp` 不是有效性證明） |
| 續期後 gateway 仍拒絕新 token | 記成失敗（`token rejected by the gateway right after login`）並進入退避，不再假裝成功 |
| 私有 profile 被殘留 Chrome 佔住、開不出 debug port | 自動 `taskkill` 那個殘留視窗再開新的（`closing stale Chrome holding the private profile: pid ...`） |
| 續期失敗 | 15 分鐘內不重試（避免每 5 分鐘都開一次登入窗），之後再試 |
| 同時有另一個在看門狗在跑 | 用 `renew-watchdog.lock` 互斥，直接跳過 |

### JWT `exp` 不是有效性證明（2026-10-25 事故）

2026-10-25 下午的「反代失效」不是 token 過期，而是**檔裡的 JWT 還宣稱有 4.7 小時，UM gateway 卻直接回 401**：
當天 13:07 續期成功、13:07 的 JWT `exp` 是 22:07，但 14:08 起每個請求都是 401（token payload 帶
`"mfa_verified":false`、`"session_stamp":null`）。舊版看門狗只看本地 `exp`，所以整個下午都沒有告警、沒有重試。

現在看門狗與健康檢查都改成**兩個獨立判定**：

- `/diagnostics` 的 `token.valid`、`um-renew.py --status`：只代表 JWT 還沒過期（樂觀）。
- **真的拿 token 打 `https://chat.genai.um.edu.mo/api/models`**：gateway 說 200 才算數。
  `um-renew.py --probe` 單獨跑這項；`um-health.py` 也新增一列 `gateway accepts token`。

另外，殘留的登入 Chrome 會鎖住 `~\.um-opencode\browser-profile`：Chrome 對同一個 `--user-data-dir` 只允許一個實例，
第二個行程只會把 URL 交棒給舊的然後退出，於是自動登入永遠等不到 debug port（log：`a Chrome window already owns
the private login profile and exposes no debug port`／`auto-login skipped: no login window with a debug port is open`）。
看門狗現在會在續期前自動清掉這種殘留視窗。

檔案位置（都在 `C:\Users\TINGOD\.um-opencode\`）：`renew-watchdog.log`（每次判斷與結果）、
`renew-watchdog.lock`、`renew-watchdog.last-fail`。

```powershell
# 只看狀態，不動任何東西
C:\Python314\python.exe -B scripts\um-renew.py --status

# 問 gateway 這個 token 還算不算數（exit 0 接受 / 2 被拒 / 3 連不上）
C:\Python314\python.exe -B scripts\um-renew.py --probe

# 立刻續期一次（不必等排程）
C:\Python314\python.exe -B scripts\um-renew.py --force
```

排程工作 `\UMGenAIProxy-Renew`（已建立，每 5 分鐘、只在目前使用者登入時執行）：

```powershell
schtasks /Query /TN "\UMGenAIProxy-Renew" /FO LIST /V    # Status: Ready、Last Result: 0
schtasks /Run   /TN "\UMGenAIProxy-Renew"                # 手動觸發一次
schtasks /Change /TN "\UMGenAIProxy-Renew" /DISABLE      # 不要自動續期時停用
schtasks /Delete /TN "\UMGenAIProxy-Renew" /F            # 完全移除
```

若要重建（例如換電腦）：

```powershell
schtasks /Create /TN "\UMGenAIProxy-Renew" /SC MINUTE /MO 5 /F `
  /TR '"C:\Python314\pythonw.exe" -B "C:\Users\TINGOD\Documents\deepseek-harness\default-workspace\dsh-um-proxy-ondemand\scripts\um-renew.py"'
```

`login` 成功後代理**不用重啟**，下一個請求就會用新 token；Duo 若跳出核准，看門狗會自動點
「Send me a push」，但仍需要你在手機上按核准（30 天 browertrust 內不會再問）。

> 看門狗用的是 `python.exe -B`（不是 `pythonw.exe`）：`login` 會 `print` 很多訊息，
> 在無主控台的 `pythonw` 下 `sys.stdout` 是 `None`，直接 `print` 會炸。

## 上游模型改名：自動同步

UM 上游偶爾會替模型 id 加上版本後綴（例如 2026-10-22：`DeepSeek-V4.1-Flash` → `DeepSeek-V4.1-Flash-1017`）。
**舊 id 不是「比較舊」而是直接不存在**——反代會回 `400`：

```text
POST /v1/chat/completions {"model":"DeepSeek-V4.1-Flash"}  →  HTTP 400
POST /v1/chat/completions {"model":"DeepSeek-V4.1-Flash-1017"} → HTTP 200
```

兩份設定會因此對不上：DSH profile 的 `um-genai: models` 清單（還有 subagent 的 `allowedModels`）
與 OpenCode 的 `~\.um-opencode\opencode.json`（`model`／`small_model`／`provider.um-genai.models`）。

`scripts\um-models-sync.py` 以**反代實際回報的 `GET /v1/models` 為準**，只改模型相關的行，
其他設定（baseURL、apiKeyEnv、其他 provider）原封不動，改之前先備份成 `<檔名>.bak-models-<時間>`：

```powershell
cd C:\Users\TINGOD\Documents\deepseek-harness\default-workspace\dsh-um-proxy-ondemand

npm run um:models          # 只看有沒有漂移（唯讀）
npm run um:models:apply    # 有漂移就改寫 profile 與 opencode.json
```

| 結束碼 | 意思 |
| --- | --- |
| `0` | 兩份設定都與上游一致 |
| `10` | 有漂移（唯讀模式，還沒改） |
| `20` | 已改寫設定檔 |
| `2` | 反代沒在跑／`/v1/models` 拿不到（先讓代理醒來或跑 `um:recover`） |
| `3` | 認證失敗（`local-api-key` 不符） |
| `4` | 設定檔或註冊表有問題 |
| `5` | **核對失敗**（id 重複／上游沒有、名稱重複或空白、`maxTokens > contextWindow`、寫出的文字無法原樣 round-trip） |

排程用的 `--auto` 模式把「代理睡著、`/v1/models` 連不上」視為「這次不用同步」（log 記一筆 `skipped:`、
結束碼仍是 `0`），否則代理每次閒置關掉後，排程就會一直留下失敗紀錄。手動的唯讀檢查則照上表回 `2`。

中繼資料的優先序是「**上游 gateway 說的算，其次才是本地註冊表**」：

| 欄位 | 來源順序 | 說明 |
| --- | --- | --- |
| `name` | gateway `name` → `um-models.json` 的 `display` → 由 id 推導 | gateway 只給與 id 相同的字串時視為沒有暱稱，改用註冊表的可讀名稱 |
| `contextWindow` | gateway `max_model_len` → 註冊表 `contextWindow` | `max_model_len` 才是真正可用的視窗（2026-10-22 實測：DeepSeek/GLM-5.3 是 1048576，Qwen 262144，gemma 180000） |
| `attachment`（圖片／檔案輸入） | gateway `info.meta.capabilities` 的 `vision`／`file_upload`／`file_context` → 註冊表 | 任何一個為真就寫 `attachment: true`（opencode.json）與 `input: [text, image]`（DSH profile） |
| `maxTokens`、`tool_call`、`temperature` | 只用註冊表 | 上游沒有這些欄位；`maxTokens` 會被夾到不超過 `contextWindow` |

`um-models.json` 的 key 是 `family`＝去掉結尾版本號的名稱（`DeepSeek-V4.1-Flash-1017` → `DeepSeek-V4.1-Flash`），
所以改名後仍能沿用同一筆策展資料（顯示名稱、輸出上限、tool_call）。

**上游推出全新的模型族**（不是改名，而是新 family）時，同步腳本會自動把它加進清單：名稱由 id 推導
（`Brand-New-Model-1234` → `Brand New Model (UM)`）、`contextWindow` 取上游 `max_model_len`、`maxTokens`
用預設值，並在 log 記一筆 `warning: ... no registry entry`；確認沒問題後再照既有格式補一個 family 進註冊表即可。

每次執行（含唯讀檢查）最後都會做一次**核對**，唯讀模式下會印出即將寫入的清單與來源統計：

```text
catalog: gateway: 7 models, 1 versioned id(s) (DeepSeek-V4.1-Flash-1017); names from {'registry': 7}; contextWindow from {'gateway': 7}
catalog:   DeepSeek-V4.1-Flash-1017 | DeepSeek V4.1 Flash (UM - coding agent) | ctx 1048576 | out 32768
...
verified: 7 models match the gateway (id, name, contextWindow, maxTokens)
```

核對項目：id 不重複且都存在於上游、名稱不重複且非空、`contextWindow > 0`、`maxTokens <= contextWindow`，
以及「把即將寫入的文字再丟回 rebuilder 必須原樣產出」（避免腳本自己改壞格式）。任一項失敗就回 `5`，
並且**不寫檔**（若已寫檔則備份仍留著，可直接還原）。

### 圖片輸入：「照片」開關為什麼是灰的

DSH 的 pi-ai 適配器把**手寫的模型條目一律當純文字**（`DEFAULT_INPUT = ['text']`），
即使端點支援視覺也一樣；附件會在送出前就被擋下、並在輸入框顯示「照片」不可用。
要打開它，模型條目必須自己宣告模態：

```yaml
models:
  - id: DeepSeek-V4.1-Flash-1017
    input: [text, image]
```

同步腳本已把這件事自動化：`info.meta.capabilities` 的 `vision`／`file_upload`／`file_context`
任一為真就寫 `input: [text, image]`，否則寫 `input: [text]`；上游哪天收回視覺能力，下一次同步就會自己收回。

上游是真的吃圖（2026-10-22 實測）：對 `DeepSeek-V4.1-Flash-1017` 送 1×1 PNG 的 `image_url`，
回 `HTTP 200`、`usage.prompt_tokens_details.multimodal_tokens.image = 184`（上游確實處理了圖片）、內容 `Salmon`。

改完同樣要**重啟 DSH host**，composer 的「照片」開關才會亮起來。

排程工作 `\UMGenAIProxy-Models` 每 30 分鐘跑一次 `--auto`（沒有漂移時**完全不寫檔、不寫 log**，
實測 ~80 ms；有漂移就自動改寫並記一筆）。要注意的是：**改完設定要重啟 DSH host**，模型選單才會換成新 id
（OpenCode 則是下次啟動讀設定）。

```powershell
schtasks /Query /TN "\UMGenAIProxy-Models" /FO LIST /V    # Last Result: 0
schtasks /Run   /TN "\UMGenAIProxy-Models"                # 手動跑一次
schtasks /Change /TN "\UMGenAIProxy-Models" /DISABLE      # 停止自動同步
```

### 一鍵健康檢查：`npm run um:health`

新增 `scripts/um-health.py`，一次把「代理活著嗎、token 還剩多久、上游通不通、log 有沒有異常、
兩個排程在不在」全部檢查完：

```powershell
npm run um:health                 # 人看的表格；結束碼 0=全部正常、1=有警告、2=有失敗
npm run um:health -- --json       # 給其他腳本吃的 JSON
npm run um:health -- --no-tasks   # 跳過排程查詢（沒有 schtasks 權限時）
```

檢查項目：`/health`、帶 key 的 `/v1/models`、`/diagnostics`（代理自己的計數器與 log 統計）、
上游 token 剩餘時間（`/diagnostics` 不可用時直接解 JWT `exp`）、**`gateway accepts token`（直接拿 token 打
`https://chat.genai.um.edu.mo/api/models`，401 就判 FAIL 並提示跑 `um-renew.py --force`）**、
`renew-watchdog.log` 最後一行、排程 `\UMGenAIProxy-Renew` 與 `\UMGenAIProxy-Models` 的狀態與下次執行時間。

> 為什麼要單獨一列：JWT 的 `exp` 只是本地宣稱。2026-10-25 就出現過「JWT 顯示還有 4.7 小時、gateway 已經 401」
> 的情況，只看剩餘時間的健康檢查會給出綠色。詳細事故說明見上面「JWT `exp` 不是有效性證明」。

### `/diagnostics`：代理端可觀察性

`serve` 現在多了一個需要 `local-api-key` 的端點：

```powershell
$key = (Get-Content "$env:USERPROFILE\.um-opencode\local-api-key" -Raw).Trim()
curl.exe -s -H "Authorization: Bearer $key" "http://127.0.0.1:8788/diagnostics?probe=1&log=1"
```

回傳 `proxy`（pid／啟動時間／uptime）、`token`（剩餘秒數／到期時間）、`stats`（累計請求、上游失敗、
重試次數、最後一次失敗訊息）、可選的 `log`（proxy.log 近 24 小時統計）與 `upstream`（即時探測上游）。

每次上游失敗都會多記一行 `upstream failed: <型別>: <訊息>`，重試時記
`... failed (attempt 1/2): ...`。**502 只在「還沒開始送回應」時才送出**；已經開始串流的請求不會被
第二個回應污染，而是直接斷線。

**上游抖動自動重試**：連線被重置／`IncompleteRead`／`RemoteDisconnected`，以及上游回
`502/503/504`，都會換一條新連線重試一次（連線層 0.4s、5xx 0.6s 後重試）；用完才把 502 回給客戶端。

## 注意事項

- **要重啟 DSH host 才會生效**：外掛是 `%USERPROFILE%\.dsh\profiles\desktop\node_modules\dsh-um-proxy-ondemand`
  的 junction 指向這個目錄，但 host 是在啟動時載入 `lib/`，改完原始碼必須重啟 host。
- **舊的每 2 分鐘保活排程已被停用**（不是刪除）：
  ```powershell
  schtasks /Query /TN "\UMGenAIProxy"                     # 應顯示 Status: Disabled
  schtasks /Change /TN "\UMGenAIProxy" /DISABLE           # 停用（已做過）
  schtasks /Change /TN "\UMGenAIProxy" /ENABLE            # 要改回常駐就開這個
  ```
  停用前它每天約啟動 720 次 python（每次都在 bind 失敗後退出），也讓「閒置自動關閉」失去意義。
- `C:\Users\TINGOD\.um-opencode\proxy.pid` 是 8787／8788 共用的舊 pid 檔；排程停用後它會停在最後
  一個死 pid。本外掛刻意不讀它，只用 `ondemand-8788.state.json`。
- **OpenCode 直連 8788**：`~\.um-opencode\opencode.json` 的 `baseURL` 是 `http://127.0.0.1:8788/v1`。
  DSH 的按需喚醒只在 DSH 發請求時生效；若你單獨開 OpenCode 而代理正好閒置關閉，OpenCode 會連不上
  （DSH 一用又會被叫起來）。要兩邊都無感，得再做一個「有人連埠就喚醒」的觸發器，目前沒做。

## 回復方式

1. 把外掛從 profile 拿掉：
   - `C:\Users\TINGOD\.dsh\profiles\desktop\package.json` 刪掉 `dependencies` 的
     `dsh-um-proxy-ondemand` 與 `dsh.bundle.bundles` 的同一項
     （備份：同目錄 `package.json.bak-before-ondemand-*`）。
   - 刪除 `C:\Users\TINGOD\.dsh\profiles\desktop\node_modules\dsh-um-proxy-ondemand`
     （目錄連結）。
2. 還原常駐排程（排程只是被停用，直接啟用即可；若已被刪除才需要重建）：
   ```powershell
   schtasks /Change /TN "\UMGenAIProxy" /ENABLE
   # 或從匯出的 XML 重建：
   schtasks /Create /TN "\UMGenAIProxy" /XML `
     "C:\Users\TINGOD\Documents\deepseek-harness\default-workspace\cmd_popup_diagnosis\um-proxy-task-export.xml"
   ```
