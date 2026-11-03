# dsh-um-proxy-ondemand

讓 UM GenAI 本機代理（`http://127.0.0.1:8788`）**只在你真的用到它的時候才活著**。

DSH 的 `um-genai` provider 指向這個代理；以前它靠一個「登入時啟動 ＋ 每 2 分鐘保活」
的排程工作常駐，即使整天沒用到該模型也一直佔著一個 Python 行程（以及它自己的
headless Chrome）。這個外掛把它改成 **按需喚醒 ＋ 閒置 15 分鐘自動關閉**。

## 安裝

### 前置條件

| 需要 | 說明 |
| --- | --- |
| **UM 帳號** | 本外掛服務的是澳門大學的 GenAI 校園 gateway（`https://chat.genai.um.edu.mo`）。沒有 UM 帳號就沒有東西可以代理。 |
| **`um_opencode_setup.py`** | 代理本體（OpenAI 相容的本機服務），UM 校內發放的單檔工具。**外掛自帶一份在 [`vendor/um_opencode_setup.py`](vendor/um_opencode_setup.py)**，所以乾淨的機器裝完就能直接跑。`~/.um-opencode/` 那份優先（你自己更新過、或 UM 發了新版就以它為準），家目錄沒有才退回自帶副本；**只有在兩邊都沒有時才會失敗**。 |
| **Node.js 22 以上** | 宿主本身的需求；跑本 repo 的測試要 24（用到實驗性的 module mock）。 |
| **Python 3.10 以上** | 建議 3.12。Windows 安裝時勾 **Add python.exe to PATH**，或稍後在設定頁直接填絕對路徑。 |

> 外掛**不是**模型、不產生 token、不代理其他 provider。它管的只有一件事：那個本機代理行程的生死。代理本體是 UM 發放的 `um_opencode_setup.py`，本 repo 只附一份原封不動的副本（見 [`vendor/`](vendor/)）。

### 安裝指令

```powershell
dsh plugin --profile desktop add dsh-um-proxy-ondemand
```

這是首選：從 npm registry 抓發佈快照（`latest` 標籤，目前是 1.4.x），不必讓 pnpm 從 git 建置，也吃得到 registry 的快取。

想跟 `main` 最新 commit（例如還沒發版的修正）就改用 git 來源：

```powershell
dsh plugin --profile desktop add github:TINGOD2006/dsh-um-proxy-ondemand
```

兩者差別只在版本來源：npm 版是發佈當下的快照，`github:` 版跟著 `main` 走、每次裝都是當下最新的 commit。功能上沒有其他差異，`vendor/um_opencode_setup.py` 兩邊都有。

`dsh plugin` 會把參數原樣轉給 profile 目錄裡的 pnpm，裝好後 `dsh.profile.bundles` 會自動補上這一項。
裝完重新啟動 DSH（新增設定分頁需要重啟一次，之後改設定不用）。

兩個常見的卡點：

- **要用桌面版內建的 `dsh`。** 桌面版的 profile 由 Electron 獨佔，npm 全域那份會直接拒絕：
  `error: profile "desktop" is managed exclusively by the Electron application`。
  確認 `Get-Command dsh` 第一個是 `...\DeepSeek Harness\resources\runtime\cli\bin\dsh.cmd`。
- **檔案被佔用導致換不上**（DSH 正開著、pnpm 拿不到檔案鎖）。完全退出 DSH，然後在 profile 目錄手動裝：

  ```powershell
  cd $env:USERPROFILE\.dsh\profiles\desktop
  pnpm add dsh-um-proxy-ondemand --config.minimum-release-age=0
  ```

  再啟動 DSH。

### 第一次設定

外掛啟動時會自己推導預設值，多數情況不必填任何東西：

- **家目錄**：`UM_OPENCODE_HOME` 有設就用它，否則 `~/.um-opencode`。
- **Python**：`UM_PYTHON` → `%LOCALAPPDATA%\Programs\Python\Python3*`（取版本最高）→ `C:\Python3*` → `PATH` 上的 `pythonw.exe`。
- **代理腳本**：`~/.um-opencode/um_opencode_setup.py` 優先，沒有才用外掛自帶的 `vendor/um_opencode_setup.py`（`lib/paths.js` 的 `resolveScript()`）。
- **日誌／狀態檔**：都在家目錄下，狀態檔帶埠號（`ondemand-8788.state.json`）。腳本自己的資料（token、`credentials.dat`、`proxy.log`）一律寫在家目錄，不會寫進外掛目錄。

要覆寫就到 **DSH 設定 → 外掛 → 「UM 代理」** 分頁，那裡有狀態列、一般設定（埠號、常駐模式、閒置分鐘、啟動逾時、Python 路徑、腳本路徑、日誌、狀態檔）、UM 帳號（寫入 `um_opencode_setup.py set-credentials`，密碼不經過命令列）、代理金鑰（`UM_GENAI_API_KEY`）。

同樣的值也可以直接寫進 profile 的 `cordis.patch.yml`：

```yaml
- insert:
    - id: dsh-um-proxy-ondemand
      name: dsh-um-proxy-ondemand
      config:
        port: 8788
        alwaysOn: true
        # python: 'C:\Users\you\AppData\Local\Programs\Python\Python312\pythonw.exe'
        # script: 'C:\Users\you\.um-opencode\um_opencode_setup.py'
```

### 模型選單（`um-genai` provider）

外掛**不註冊 provider** —— 它只負責那個代理行程的生死。DSH 模型選單裡的那些 UM 模型，來自內建的 `@deepseek-ai/dsh-llm-pi-ai` 底下的一段 `providers.um-genai` 設定，而那段設定寫在 profile 的 `cordis.patch.yml`，新裝的機器上本來沒有，所以選單會是空的。

補的方法有兩種：

1. **設定頁按鈕（建議）**：DSH 設定 → 外掛 →「UM 代理」→「模型選單（um-genai provider）」那張卡 → 按 **建立 um-genai provider**。它會用 `configEditor` 把 `displayName` / `apiKeyEnv` / `api` / `baseURL` / `models` 寫進 profile patch，接著跑 `scripts/um-models-sync.py --apply` 連本機代理抓 gateway 目前提供的模型 id（代理沒開會自己醒），最後提示你重啟 DSH。
2. **手動**：把下面這段貼進 profile 的 `cordis.patch.yml`，再跑 `python -B scripts/um-models-sync.py --apply` 補模型清單。

```yaml
- insert:
    - id: llm-pi-ai
      name: "@deepseek-ai/dsh-llm-pi-ai"
      config:
        providers:
          um-genai:
            displayName: UM GenAI (local proxy)
            apiKeyEnv: UM_GENAI_API_KEY
            api: openai-completions
            baseURL: http://127.0.0.1:8788/v1
            models: []
```

`models: []` 只是佔位，`um-models-sync.py --apply` 會把它換成真正的清單（並留下 `.bak-models-<時間>` 備份）。跑完要重啟 DSH，選單才會重讀。

按鈕已經有 provider 時只會補缺的鍵，你手動調過的 `displayName`、埠號、模型子集都不會被蓋掉。

如果按鈕**剛建立** provider 卻抓不到模型清單（最常見是還沒登入 UM 帳號、代理起不來），它會把剛寫進去的 provider 收回，而不是留下一個 `models` 空白的 provider —— 後者會讓 DSH 載入時直接報 `provider "um-genai" resolves no models`。先到「UM 帳號」登入、確認代理跑得起來，再按一次就好。

### 環境變數

| 變數 | 預設 | 作用 |
| --- | --- | --- |
| `UM_OPENCODE_HOME` | `~/.um-opencode` | 代理腳本、日誌、狀態檔、續期看門狗紀錄的家目錄。JS 與 Python 兩邊共用同一條規則。 |
| `UM_PYTHON` | 自動偵測 | 明確指定 Python 解譯器；設了就跳過偵測。 |
| `UM_UPSTREAM_BASE` | `https://chat.genai.um.edu.mo` | 上游 gateway。換校區或測試環境才需要改。 |
| `UM_HIDDEN_LOGIN` | `1` | 設 `0` 可關掉「隱藏桌面登入」，回到會彈出可見視窗的舊行為。 |
| `UM_TEST_PYTHON` | — | 只影響測試，不影響正式代理。 |

### 驗證裝好了

```powershell
# 代理本身（要先被喚醒過一次；alwaysOn: true 的話 DSH 一開就有）
curl.exe http://127.0.0.1:8788/health
# {"status":"ok","service":"um-opencode-proxy"}

# 外掛自己（把埠換成你 DSH 的 GUI 埠）
curl.exe http://127.0.0.1:<GUI埠>/um-proxy-ondemand/api/status
```

> 用 `curl.exe` 而不是 `curl`：Windows PowerShell 5.1 把 `curl` 別名成 `Invoke-WebRequest`，
> 參數與輸出都不是 curl 的樣子。

設定頁的「狀態」區塊看得到同樣的資訊：`running`、`pid`、`alwaysOn`、`idleTimerArmed`、請求數與喚醒數。`/api/status` 另外回報 `host.script: { path, exists }`——`path` 是**實際會跑的那一份**（家目錄或 `vendor/`），所以正常安裝下 `exists` 是 `true`；`false` 代表兩邊都找不到腳本，設定頁會同時顯示紅字警告。實際用法是回 DSH 選一個 `um-genai` 的模型送一句話——第一次會冷啟動（幾秒），之後走熱路徑。

### 常見錯誤

| 症狀 | 原因 | 處理 |
| --- | --- | --- |
| 設定頁顯示「找不到腳本」 | `~/.um-opencode/um_opencode_setup.py` 不存在，**而且**外掛自帶的 `vendor/um_opencode_setup.py` 也不在（例如只裝了 `lib/`、或有人刪掉了 `vendor/`） | 通常不必處理——自帶副本會接手。真的兩邊都沒有時：重裝外掛補回 `vendor/`，或把 `UM_OPENCODE_HOME` / 設定頁的「腳本路徑」指到正確位置 |
| 喚醒失敗，錯誤訊息是 `proxy script not found: <路徑> — this file is not part of the plugin` | 家目錄與 `vendor/` 都沒有代理腳本，所以連 spawn 都沒發生。正常安裝下不該出現 | 這句是外掛自己給的（不是 Python 的 `can't open file`）。重裝外掛取回 `vendor/` 副本，或把腳本放到那個路徑、設 `UM_OPENCODE_HOME`。狀態端點會回報 `host.script: { path, exists: false }`，設定頁狀態區塊同時顯示紅字警告 |
| 冷啟動失敗，日誌有 `ENOENT` | Python 路徑不對 | 在設定頁填 `pythonw.exe` 的絕對路徑，或設 `UM_PYTHON` |
| 回 401 | 上游 UM 登入 token 過期（不是 API key 錯） | 跑 `powershell -NoProfile -ExecutionPolicy Bypass -File scripts\um-recover.ps1 -Fix`，或等續期看門狗；詳見下方〈反代 401〉一節 |
| 埠被佔用 | 8788 上已經有別的行程在聽 | 設定頁改 `port`，或先找出佔用者；外掛不會殺不屬於自己的行程 |
| 回 502／503／504 | UM 自己的模型後端連不上（503 的 body 是 Envoy 的 `upstream connect error`），不是本地設定錯 | 代理會自動重試 3 次（1s／3s／6s）；仍失敗就是 UM 那邊還沒恢復，等幾分鐘再試。詳見下方〈上游 5xx〉一節 |
| 設定頁整頁空白 | 分頁沒註冊成功（少見） | 重啟 DSH；仍空白就查 `~/.dsh/profiles/desktop` 的 log |

### 移除

```powershell
dsh plugin --profile desktop remove dsh-um-proxy-ondemand
```

移除只拿掉外掛本身。已經在跑的代理行程不會被帶走（`alwaysOn: true` 的設計就是不讓 DSH 的生命週期管它），
要一起收掉就 `python "$env:USERPROFILE\.um-opencode\um_opencode_setup.py" stop`；排程工作（如果建過）用
`schtasks /Delete /TN "\UMGenAIProxy-Renew" /F` 清掉。

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
| `/health` 要求 HTTP 200 且 `service` 相符 | 健康探測有獨立的總逾時、64 KiB 上限，503／截斷／異常 JSON 都不算健康。終止行程另核對命令列，不能只信 service 標籤。 |
| 用自己的 state 檔，不用 `proxy.pid` | `proxy.pid` 是 8787 的 `um_windows.py ensure` 在用的單一檔案，共用會互相覆蓋。 |
| 停止時檢查所有監聽者，只終止確認屬於此代理者 | 每個 PID 都須符合 Python 路徑、完整腳本路徑、`serve` 與埠號；外來服務會保留並回報停止未完成。 |
| 不以 `state.pid` 作為終止依據 | Windows 會回收 PID。netstat 或 CIM 無法確認時會回報失敗，不退回舊狀態檔猜測。 |
| 冷啟動時 `detached + windowsHide`，對象是 `pythonw.exe` | GUI 子系統 ＋ `DETACHED_PROCESS` ⇒ 不產生任何主控台視窗（已實測，見下）。 |
| 家目錄那份腳本優先，`vendor/` 只是後備 | 使用者可能自己更新過那份工具，或 UM 發了新版；自帶副本只負責讓乾淨的機器能直接跑。反過來（自帶優先）會讓使用者的更新被悄悄忽略。 |
| spawn 前先檢查代理腳本存在 | 家目錄與 `vendor/` 都沒有時（例如只裝了 `lib/`、或有人刪掉 `vendor/`），自己先報 `proxy script not found: <路徑>` 比讓使用者去讀 Python 的 `can't open file` 或一句 `did not become healthy` 有用得多。 |

## 效率：熱路徑與喚醒延遲

原本的版本每個請求都會做一次 `/health`（正確但也最貴的地方在**冷喚醒**與**同步 netstat**）。
以下是實測後的優化，數字都是本機量到的（Python 3.14 已快取、代理在 143ms 內可服務）：

| 項目 | 舊版 | 現在 | 說明 |
| --- | --- | --- | --- |
| 冷喚醒（spawn → 可服務） | 281–344 ms | **177 ms** | 輪詢排程由固定 250ms 改成 20→250ms 前密後疏，且 spawn 後先立刻探一次。 |
| 並行冷啟動 | 每個請求各 spawn 一次（實測 2 個 python，1 個 bind 失敗退出） | **只 spawn 1 次** | 同埠 `singleFlight()`：進行中的喚醒共用同一個 promise。 |
| 熱路徑（代理已活著） | 1 次 health ＋ 同步 `netstat`（~31ms，卡 event loop）＋ 每請求同步寫 state | **1 次 health；自啟代理可沿用存活的子行程 handle** | `healthDetail()` 分辨 `ECONNREFUSED`＝真的沒人聽 ⇒ 直接 spawn，省掉 netstat；只有「有回應但不是我們的代理」才去查誰佔著埠。 |
| 狀態檔寫入 | 每請求同步寫一次 | 記憶體最新，磁碟最多每 15s | 喚醒、停止、DSH 關閉時 `flushState()` 立刻落盤。 |

其餘刻意**不做**的取捨：`/health` 用 keep-alive agent 實測沒有增益（0.25 對 0.25ms）；
health 快取（例如 2 秒內跳過）能再省 0.3ms，但代理若剛好死掉會讓請求直接失敗而不是自動冷啟動，不值得。

另外，`python` 的直譯器啟動就佔了冷喚醒的大半——那是代理本身的成本，外掛只能把「其實已經好了卻還在等」的空窗拿掉。

## 檔案

| 檔案 | 作用 |
| --- | --- |
| `package.json` | DSH bundle 契約（`type: module`、`exports`、`dsh.bundle.patch`）與 `files`（含 `vendor/`）。 |
| `.gitattributes` | 讓 `vendor/um_opencode_setup.py` 以 LF 存放，這樣它的 sha256 才對得上 `vendor/README.md` 記的值。 |
| `cordis.patch.yml` | 插入條目與「每台機器都一樣」的設定（`port: 8788`、`alwaysOn: true`）。個人路徑不要寫在這裡。 |
| `lib/paths.js` | 可移植的預設值：`homeDir` / `scriptPath` / `bundledScriptPath` / `resolveScript` / `logPath` / `statePath` / `detectPython` / `defaultsOf`。從 `UM_OPENCODE_HOME`、`UM_PYTHON`、`%LOCALAPPDATA%`、`PATH` 推導，不寫死任何一台機器的路徑。 |
| `lib/proxy.js` | 代理生命週期：`healthDetail` / `health` / `ensureProxy`（single-flight） / `stopProxy` / `listeningPids`、狀態檔節流。不依賴 cordis。 |
| `lib/process.js` | 找 listener／找代理行程／安全終止：只殺認得出是自己那支腳本的行程。 |
| `lib/index.js` | 事件接線（`llm/stream`）、閒置計時器、唯讀狀態端點、啟動清理與關閉清理。 |
| `lib/settings-api.js` | 設定頁的後端：`/um-proxy-ondemand/api` 底下的 `/status`、`/settings`、`/account`、`/api-key`、`/provider`，含憑證寫入（走 stdin，不經過命令列）、金鑰儲存，以及寫入 `um-genai` provider 後跑模型同步。 |
| `lib/client.js` | 設定頁的前端 bundle（手寫的模組載入器格式，只依賴 `react`，沒有建置步驟）。 |
| `test/ondemand_check.mjs` | 生命週期：冷啟動、沿用、停止、冪等、重複監聽。 |
| `test/plugin_wiring.mjs` | 用 mock ctx 跑真的 `apply()`：非目標 provider 零成本放行、目標 provider 喚醒並轉送串流。 |
| `test/idle_timeout.mjs` | 閒置自動關閉（用 `idleSeconds: 3` 壓縮週期）。 |
| `test/coldstart.mjs` | 冷啟動計時，供無視窗驗證使用。 |
| `test/efficiency_unit.mjs` | 純單元測試（不寫檔、不起代理）：single-flight、輪詢排程、狀態檔節流、`healthDetail` 判定。 |
| `test/paths_unit.mjs` | 純單元測試：家目錄與四個路徑的推導、Python 偵測順序、`defaultsOf` 不含作者機器的路徑、`resolveScript` 的「家目錄優先、`vendor/` 後備」順序。 |
| `vendor/um_opencode_setup.py` | UM 校內發放的單檔工具（安裝器＋本機代理＋診斷），原封不動放進來當後備，讓乾淨的機器裝完就能跑。著作權屬原發放單位。 |
| `vendor/README.md` | 那份副本的來源、載入順序、更新方式（含 sha256 前 16 碼 `1b4555a48a999254`）與授權說明。 |
| `scripts/um-recover.ps1` | 反代自救腳本：診斷 401（上游 UM token 過期 vs `local-api-key` 不對），可 `-Fix` 自動重新登入。 |
| `scripts/um-renew.py` | 續期看門狗（獨立於代理）：每 5 分鐘檢查 JWT `exp`、**距上次成功續期的時間**和 gateway 是否真的接受 token，需要時自動跑 `login`、清掉殘留的登入 Chrome、先把登入瀏覽器放到隱藏桌面，並在續期後實測新 token。 |
| `scripts/um_hidden_window.py` | 把登入用的 Chrome 開在私有的 Win32 桌面上（使用者看不到，但渲染與 DevTools 照常），並把它的 debug 埠發佈到 `browser-debug-port`；可 `--status`／`--ensure`／`--close`／`--probe`。 |

執行測試（前三支會實際啟停 8788 代理）：

```powershell
cd <本 repo 目錄>
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
        alwaysOn: true
```

可用欄位：`port`、`alwaysOn`、`idleMinutes`、`idleSeconds`（測試用短週期覆寫）、`provider`
（預設比對 `um-genai`）、`python`、`script`、`logFile`、`stateFile`、`startupTimeoutMs`。

### 常駐模式（`alwaysOn: true`，目前預設）

按需喚醒省資源，但代價是「DSH 重啟或關掉之後，8788 沒人聽」——使用者會遇到 401／連不上，
而且外掛自己的 token 看門狗也只在代理活著時才跑。要它長期有效就開常駐：

| 行為 | 按需（`idleMinutes: 15`） | 常駐（`alwaysOn: true`） |
| --- | --- | --- |
| 宿主啟動 | 等第一個 um-genai 請求才喚醒 | **立刻喚醒**（`ensureProxy`），DSH 一開就好 |
| 閒置 | 15 分鐘後關掉 | **不關**（`idleTimerArmed` 永遠 `false`） |
| 宿主關閉 | 把代理一起關掉 | **不關**，留下來繼續服務 |
| 代理真的掛掉 | 沒人補 | 排程 `\UMGenAIProxy-Renew` 每 5 分鐘跑 `um-renew.py`，順便 `ensure_proxy()` 補位 |
| 補位延遲 | — | 最多 5 分鐘（可用 `schtasks /Change /TN "\UMGenAIProxy-Renew"` 調整） |

`idleMinutes: 0` 也等於常駐（比較好記）。狀態端點會回報 `alwaysOn: true`，
`idleTimerArmed` 應為 `false`——如果還是 `true`，代表宿主跑的是舊版外掛，重啟 DSH 即可。

> 常駐模式不會阻止你手動 `stop`；但下次看門狗（5 分鐘內）會再把它叫起來。
> 要暫時停用，先把排程 Disable：`schtasks /Change /TN "\UMGenAIProxy-Renew" /DISABLE`。


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
cd <本 repo 目錄>

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
- 手動等價指令：`python %USERPROFILE%\.um-opencode\um_opencode_setup.py login --timeout 170`
  （`login` 不吃 `--port`）。
- 若在 DSH 的受限沙箱裡跑，login 會死在 `os.chmod('%USERPROFILE%\.um-opencode')`
  → `PermissionError: [WinError 5]`；請在一般 PowerShell／cmd 視窗執行。
- 想根治：登入後把那個 UM 登入視窗留著（有 debug port 可重複用）或完全關掉再讓它靜默續期；
  最怕的是「活著但沒有 debug port」的殘留 Chrome。

## 上游 5xx：UM 自己的後端掛掉

UM 的 gateway 在自家模型後端連不上時，會回 **503**，body 是 Envoy 的
`upstream connect error ... transport failure reason: ...`；連線層失敗是 **502**，逾時是 **504**。
這三種都**不是**本地設定或 token 的問題，重登也沒有用。

代理的處理（`vendor/um_opencode_setup.py` 的 `ProxyHandler.upstream_with_retry()`）：

1. 502／503／504 或連線例外 → 重試 3 次，退避 1s／3s／6s。
2. 重試前先跑一次 `probe_um_token()`（`GET /api/models`）判斷 token 是否真的被拒：
   - 回 `False`（401／403）→ 才呼叫 `renew_token_once()`，換 token 後立刻重試。
   - 回 `None`（網路錯誤或 5xx）→ **不重登**，只退避重試。
3. 三次都失敗才把最後那個 5xx 回給呼叫端。

第 2 點是刻意的：UM 後端掛掉時盲目重登會變成登入風暴（每次重登都要開隱藏瀏覽器、可能觸發 Duo），
而且那段時間登入本身也會失敗。所以「上游錯誤就重取 token」只在 probe 證明 token 被拒時才做。

`GET /v1/models` 與 `POST /v1/chat/completions` 都走這條路徑；串流回應在開始串流前就決定要不要重試，
所以不會出現「已經送出一半才重試」的情況。

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
| **8788 沒人聽（DSH 關掉／重啟中／代理掛掉）** | **先把代理叫起來**（`ensure_proxy()`：`serve --port 8788`，最多等 30 秒），log `proxy was down; started it (pid ...)`；失敗也只記一行，不影響續期 |
| JWT 還有效、距到期 > 20 分鐘、**距上次成功續期未滿 25 分鐘**，**且 gateway 實測接受** | 讀一次 JWT ＋ 一次 `GET /api/models`，靜默 `exit 0`（實測 **315 ms**，不開任何視窗） |
| 距到期 ≤ 20 分鐘、已過期、**距上次成功續期超過 25 分鐘**、或 JWT 讀不到 | 執行 `python um_opencode_setup.py login --timeout 120`，把過程寫進 log |
| **JWT 沒到期但 gateway 回 401／403** | 一樣當作需要續期（見下節：JWT `exp` 不是有效性證明） |
| 續期後 gateway 仍拒絕新 token | 記成失敗（`token rejected by the gateway right after login`）並進入退避，不再假裝成功 |
| 私有 profile 被殘留 Chrome 佔住、開不出 debug port | 自動 `taskkill` 那個殘留視窗再開新的（`closing stale Chrome holding the private profile: pid ...`） |
| 需要登入（token 到期、或 gateway 拒收） | 先把登入瀏覽器開在**隱藏桌面**上（`um_hidden_window.py`），登入腳本直接接管那個瀏覽器：不彈窗。詳見「隱藏桌面登入」 |
| 隱藏桌面那次登入沒完成（Duo 沒人按、密碼變了） | 關掉隱藏瀏覽器，改用一般可見視窗重試**一次**；10 分鐘內不再彈（記錄在 `renew-watchdog.visible-prompt`） |
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

### 續期時鐘：按上次成功續期計時，不看 JWT `iat`

同一份 `browser-profile` 重新登入，gateway 會把**同一顆 session token** 發回來，`iat` 因此不會前進。
「簽發超過 25 分鐘就續期」這條規則一旦真的重登成功，`iat` 還停在幾小時前，條件永遠成立：看門狗每
5 分鐘重登一次（隱藏桌面下則是每 5 分鐘白跑一次登入）。

現在時鐘換成**上次續期真的成功**的時刻：`renew-watchdog.last-success`，只有 `attempt_login()` 被
gateway 確認之後才寫入；檔案不存在就回退到 token 檔本身的 mtime，所以舊機器升級上來也不會誤判。
`--status` 兩個時間都印出來：

```text
token state: valid until 2026-11-03 07:59:32 (539 min left)
issued at  : 2026-11-02 22:59:32 (0 min ago, not the renewal clock: a re-login hands back the same session token)
renewed at : 2026-11-02 22:58:08 (2 min ago)
renew when : expired, less than 20 min left, 25 min after the last renewal, or the gateway rejects it
login      : hidden desktop (UM_HIDDEN_LOGIN)
```

`needs_renewal(exp, now, renewed_at)` 的順序不變：`exp` 剩下不到 20 分鐘就續期，其餘看距上次成功
續期是否已滿 25 分鐘。gateway 探測仍是最終裁判：`exp` 說沒事、gateway 卻回 401／403 一樣續期。

### 隱藏桌面登入：續期不再彈窗

登入腳本原本每次都要在使用者螢幕上開一個 Chrome。Windows 上一個視窗可以建立在沒人看的桌面上：
`CreateDesktopW` 造一個私有桌面、`STARTUPINFO.lpDesktop` 讓子行程在那裡開視窗——視窗是真的
（Chrome 照常渲染、跑 JS、DevTools 照常在 localhost 接命令），只是永遠不出現在你的螢幕上。

`scripts/um_hidden_window.py` 只負責這一件事：把 Chrome 放到隱藏桌面，並把它的 debug 埠寫進
`~\.um-opencode\browser-debug-port`。登入腳本看到已經有可用的 debug 埠就直接接管那個瀏覽器
（`open_login_window()` 回 `None`、`auto_login()` 直接 attach），不會再開第二個視窗；代理自己的
401 補救路徑走的也是同一個埠，所以一樣不彈窗。

桌面句柄必須活到瀏覽器自己接手為止：`CreateProcessW` 只是把行程丟出去就返回，新行程還沒開啟那個
桌面，這時 `CloseDesktop` 會把桌面連底下正在啟動的 Chrome 一起拆掉，瀏覽器以 `0xC06D007E`
（`STATUS_DLL_NOT_FOUND` 形式的崩潰碼）當場結束、永遠不會發佈 debug 埠。外觀上就只是「Chrome
起不來」，但實際上是我們自己把它的桌面關掉了。所以 `ensure_hidden_chrome()` 把
`wait_for_devtools()` 包在 `try/finally` 裡，**確認瀏覽器起來之後才** `CloseDesktop`。

什麼時候還是會彈：隱藏桌面那次登入沒完成。最常見是 Duo 推送到手機但沒人按，或密碼／MFA 狀態變了
——只有人能處理，而且從腳本這邊分不出是哪一種。於是看門狗會關掉隱藏瀏覽器，改用一般可見視窗重試
**一次**，並記下 `renew-watchdog.visible-prompt`：`VISIBLE_PROMPT_COOLDOWN = 600` 秒內不再彈第二次，
避免變成每 5 分鐘騷擾一次。

| 指令 | 作用 |
| --- | --- |
| `python -B scripts/um_hidden_window.py --status` | 隱藏瀏覽器還活著嗎（JSON：`ok`／`port`／`pid`／`browser`／`desktop`） |
| `python -B scripts/um_hidden_window.py --ensure` | 需要時啟動一個；已在跑就沿用（`reused: true`） |
| `python -B scripts/um_hidden_window.py --close` | 關掉它，清掉埠檔與 PID 檔 |
| `python -B scripts/um_hidden_window.py --probe` | 自我測試：開起來、確認 DevTools 有回應、再收乾淨 |

`UM_HIDDEN_LOGIN=0`（或 `false`／`no`／`off`）可關掉這一層，回到「直接開可見視窗」的舊行為；
`--status` 的 `login` 那行會顯示目前是 `hidden desktop` 還是 `visible window`。隱藏桌面只是把視窗
移出你的螢幕，沒有繞過任何驗證：該按的 Duo 核准還是要按。

檔案位置（都在 `%USERPROFILE%\.um-opencode\`）：`renew-watchdog.log`（每次判斷與結果）、
`renew-watchdog.lock`、`renew-watchdog.last-fail`、`renew-watchdog.last-success`（上次續期成功的時刻）、
`renew-watchdog.visible-prompt`（上次彈出可見視窗的時刻）、`hidden-window.pid`、`browser-debug-port`。

```powershell
# 只看狀態，不動任何東西
python -B scripts\um-renew.py --status

# 問 gateway 這個 token 還算不算數（exit 0 接受 / 2 被拒 / 3 連不上）
python -B scripts\um-renew.py --probe

# 立刻續期一次（不必等排程）
python -B scripts\um-renew.py --force
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
  /TR '"pythonw" -B "%USERPROFILE%\<本 repo 目錄>\scripts\um-renew.py"'
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
cd <本 repo 目錄>

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
- `%USERPROFILE%\.um-opencode\proxy.pid` 是 8787／8788 共用的舊 pid 檔；排程停用後它會停在最後
  一個死 pid。本外掛刻意不讀它，只用 `ondemand-8788.state.json`。
- **OpenCode 直連 8788**：`~\.um-opencode\opencode.json` 的 `baseURL` 是 `http://127.0.0.1:8788/v1`。
  DSH 的按需喚醒只在 DSH 發請求時生效；若你單獨開 OpenCode 而代理正好閒置關閉，OpenCode 會連不上
  （DSH 一用又會被叫起來）。要兩邊都無感，得再做一個「有人連埠就喚醒」的觸發器，目前沒做。

## 回復方式

1. 把外掛從 profile 拿掉：
   - `%USERPROFILE%\.dsh\profiles\desktop\package.json` 刪掉 `dependencies` 的
     `dsh-um-proxy-ondemand` 與 `dsh.bundle.bundles` 的同一項
     （備份：同目錄 `package.json.bak-before-ondemand-*`）。
   - 刪除 `%USERPROFILE%\.dsh\profiles\desktop\node_modules\dsh-um-proxy-ondemand`
     （目錄連結）。
2. 還原常駐排程（排程只是被停用，直接啟用即可；若已被刪除才需要重建）：
   ```powershell
   schtasks /Change /TN "\UMGenAIProxy" /ENABLE
   # 或從匯出的 XML 重建：
   schtasks /Create /TN "\UMGenAIProxy" /XML `
     "<你匯出的 um-proxy-task-export.xml>"
   ```

## 穩定性硬化（2026-10-25 第三輪）

2026-10-25 的事故：JWT 顯示還剩 4.7 小時，gateway 卻從發放後約一小時起一路 401；同時一個殘留的
Chrome 佔住私有 profile 卻沒有 debug port，續期每次都 `auto-login skipped`。除了修掉那兩件事，
這一輪把「同樣的坑不會再靜默發生」補起來：

| 改動 | 解決什麼 |
| --- | --- |
| `\UMGenAIProxy-Renew`、`\UMGenAIProxy-Models` 都設 `StartWhenAvailable` | 機器休眠／關機期間錯過的排程，恢復後立刻補跑（10-23 曾因此 11 小時沒續期） |
| `/diagnostics` 新增 `proxy.build`（`um_opencode_setup.py` 的 SHA256 前 12 碼） | 分得出「跑著舊碼的代理」與「已修好但沒重啟」 |
| `um-health.py` 新增 `proxy build`、`proxy restarts (24h)` 兩列 | 前者對不上就 FAIL 並提示重啟；後者用「啟動次數多 + 目前 uptime 很短」判斷崩潰迴圈 |
| `serve` 啟動時把超過 8 MB 的 `proxy.log` 輪替成 `proxy.log.1` | 日誌不再無限長大（24h 掃描本來就只讀尾端 4 MB） |
| `um-models-sync.py` 代理睡著時改直連 `https://chat.genai.um.edu.mo/api/models` | 夜間 `--auto` 不再整段 `skipped: the proxy is not reachable` |
| `setup status` / `stop` 自動偵測 8788／8787，不再假設 `DEFAULT_PORT=8787` | 以往會對健康的 8788 代理回報 `Proxy: stopped`，`stop` 也殺不到（PID 檔與 listener 不一致時改以命令列找 `serve` 進程） |

用法：

```powershell
python scripts\um-health.py
python %USERPROFILE%\.um-opencode\um_opencode_setup.py status     # 自動認 8788
python -C "import py_compile; py_compile.compile(r'%USERPROFILE%\.um-opencode\um_opencode_setup.py', doraise=True)"
```

`proxy.build` 對不上時，重啟代理讓它載入新檔：

```powershell
python %USERPROFILE%\.um-opencode\um_opencode_setup.py stop
python -B %USERPROFILE%\.um-opencode\um_opencode_setup.py serve --port 8788 --ignore-console-events --log-file %USERPROFILE%\.um-opencode\proxy.log
```


## 穩定性修正（2026-10-27）

這輪保留 DSH 的 provider／串流介面，修正生命週期與輔助腳本：

- 健康檢查要求 HTTP 200、正確 service、完整 JSON，限制回應 64 KiB；總逾時不會被持續傳送的小封包延長。`service` 設定也會實際生效。
- 每個埠的啟動／停止依呼叫順序排隊，並行喚醒共用結果。Python 啟動失敗會正常回傳錯誤；提早退出不再等完整逾時，逾時後先回收自己啟動的子行程，最多補救一次。
- 實際生命週期使用非同步 netstat／CIM／taskkill，避免 Windows 子命令阻塞 DSH 的 event loop。自啟代理的熱路徑沿用 ChildProcess handle；沿用外部代理時可能另查非同步 netstat。前文延遲數字是早期版本的歷史測量，未代表本輪效能基準。
- 終止前逐一核對 Python 安裝路徑（同目錄的 python.exe／pythonw.exe 視為同一組）、完整腳本路徑、`serve` 與唯一埠號。無法確認時保留行程，不使用舊 PID 檔補猜。
- 啟動清理讓新請求優先；閒置停止開始後，新請求等待停止完成再喚醒。串流進行中不啟動閒置計時；卸載時等待目前串流結束，再清理代理，不重新布署計時器。停止失敗會保留錯誤並於下個閒置週期重試。
- 狀態檔以暫存檔原子替換；節流期間讀取可取得記憶體中的最新狀態。port、啟動逾時與閒置時間會先驗證。
- 續期改用作業系統檔案鎖，無法取得鎖時不登入；行程退出後自動釋放，不因鎖檔年齡而驅逐仍在工作的行程。`renew-watchdog.lock` 會保留在磁碟，檔案存在不代表鎖仍被持有。
- Chrome 清理限定完整 `--user-data-dir` 路徑，不匹配其他同名 browser-profile；保留 debug 模式與 renderer／utility 子行程。gateway 探測須取得真正的模型 JSON；網路故障回傳 3，登入後無法確認 token 時回報失敗並退避。
- 健康工具對異常 JSON／JWT 回報失敗項目並繼續檢查；HTTP 503 不再當成休眠，停用排程會警告，CSV 日期中含逗號也能解析；看門狗日誌只讀最後 64 KiB。
- 模型同步以獨立的 OS 鎖保護每個設定檔，使用唯一暫存檔與備份名稱；先備份全部目標，再寫入。中途失敗時回復已寫入且未被別人再修改的檔案，並保留備份。`.models-sync.lock` 側檔會持續存在；純檢查模式不建立鎖、備份或日誌。`--apply`／`--auto`／`--check` 互斥；異常或重複模型 ID 會拒絕同步。上游明確撤回圖片能力時也會覆寫註冊表中的舊宣告。
- `um-recover.ps1` 區分 gateway 拒絕與網路未知，不以模糊的 `healthy` 字串或本機 models 200 就宣告上游正常；登入後會再探測 gateway。

### 2026-10-27 17:55：token 在 `exp` 之前就被 gateway 丟掉

兩次事故（10-25 13:07 發、14:08 起 401；10-27 16:52 發、約 17:52 起 401）都是同一個形狀：
JWT 的 `exp` 還有 8 小時以上，gateway 卻已經不認這個 session。也就是說 **`exp` 是上限，不是保證**，
伺服端會在簽發後約 30–60 分鐘內結束 session（10-27 晚間又觀測到 17:57 簽發、18:27 就被拒，只撐了 30 分鐘）。

- `um-renew.py` 改為「按年齡續期」：`MAX_TOKEN_AGE_SECONDS = 25 分鐘`（原本 50 分鐘，30 分鐘那次事故後收緊），
  超過就主動換，不等 `exp` 逼近；加上每 5 分鐘一次的 gateway 探測，最壞情況的 401 窗口約 5 分鐘。
- gateway 探測失敗會重試一次（15 秒逾時 × 2）：10-27 那次就是一顆 15 秒逾時把續期跳過去了，
  下一秒請求就吃到 401。
- 若兩次探測都連不上，但 token 已超過 15 分鐘，仍照樣續期（寧可多換一次，也不要讓使用者看到 401）。
- `open_login_window()` 的「等除錯埠」由 8 秒放寬到 20 秒，`auto_login()` 找不到視窗時最多重試 3 次並各等 1 秒：
  17:55 那次代理端記的是 `auto-login skipped: no login window with a debug port is open.`，
  其實只是 Chrome 還沒把埠開出來。
- 續期後仍會用 gateway 驗一次；被拒就記 `token rejected by the gateway right after login` 並退避，不寫假的成功。
- 同一輪把「代理本身」也納入看門狗：`ensure_proxy()` 在每次執行開頭檢查 `http://127.0.0.1:8788/health`，
  沒人聽就自己 `serve` 起來並等 `/health`（最多 30 秒）。搭配外掛的常駐模式，DSH 關著或重啟中也不會出現
  「8788 沒人聽」的空窗（實測：`taskkill` 掉代理後跑一次看門狗 → `proxy was down; started it (pid 6920)`）。

驗證：`um-renew.py --status` 顯示 `issued at` 與新的續期條件（`older than 25 min`）；`--probe` 回 `gateway: ok (HTTP 200)`；
token 有效時排程本體 372 ms 靜默結束；`um-health.py` 的 `gateway accepts token` 為 HTTP 200。

`alwaysOn` 的行為由 `node test/always_on.mjs` 驗證（9 項：啟動即喚醒、閒置不關、狀態端點 `alwaysOn`／
`idleTimerArmed: false`、卸載不帶走代理）。

可重複執行的隔離回歸測試：

```powershell
npm test
```

需要 Node.js 24（外掛接線測試使用實驗性的 module mock）與 `python`。
`npm test` 不啟停正式 8788、不開真實登入瀏覽器、不讀真實憑證：生命週期測試在隨機回環埠啟停 `test/fixtures/proxy.py`，其餘登入／排程／設定檔故障由 mock 與暫存檔驗證。自訂測試 Python 可設定 `UM_TEST_PYTHON`；這不改變正式代理的 Python 設定。

本輪部署後須重啟 DSH host 才會重新載入 JS；排程腳本於下次排程執行時載入新碼。這裡的自動化測試不等於已完成真實 DSH 對話或 Duo 登入驗證。


## 自動續期穩定性修正（2026-10-28）

保留目前常駐保活與 token 25 分鐘年齡續期策略，補強排程執行的一致性：

- 整輪保活、gateway 探測、退避判斷與登入共用 OS 互斥鎖；重疊排程取得鎖失敗就退出，避免在鎖外重複啟動代理。取得鎖後重新讀取 token 時間資訊。
- 代理埠有監聽者卻不健康時不再重複啟動；子行程提早退出就立即回報，啟動逾時則終止並回收該次建立的子行程。代理無法恢復時，即使 token 可用也回傳結束碼 2。
- 登入輸出改寫暫存檔，僅讀最後 16 KiB；登入逾時會終止並等待登入 Python 父行程退出。既有 Chrome 仍依私有 profile 清理規則處理。
- 非法、超大或非有限 JWT 時間值會被忽略；退避時間若因時鐘回撥落在未來，會允許重新判斷。退避檔以原子替換寫入。
- `npm test` 自動收集 Python 穩定性測試。所有測試中的保活、gateway 與登入均隔離或 mock，不使用真實登入憑證。

排程下一次執行會自動載入新的 Python 腳本。本輪未觸發真實續期或 gateway token 探測；需要 Duo 核准時仍須人工完成。

## 隱藏桌面登入與續期時鐘（2026-11-02）

這一輪不改變「按需喚醒 ＋ 25 分鐘年齡續期」的策略，只修看門狗自己製造的兩個問題：

- **登入不再彈窗**：新增 `scripts/um_hidden_window.py`，把登入 Chrome 放到私有 Win32 桌面
  （`CreateDesktopW` ＋ `STARTUPINFO.lpDesktop`，桌面名 `um-login-hidden`），並把 debug 埠發佈到
  `~\.um-opencode\browser-debug-port`。`open_login_window()` 見到可用埠就不再開新視窗、`auto_login()`
  直接 attach，代理自己的 401 補救路徑同樣受益。只有隱藏桌面那次登入失敗（Duo 沒人按、憑證變了）
  才退回可見視窗，且 10 分鐘內只彈一次（`renew-watchdog.visible-prompt`）。`UM_HIDDEN_LOGIN=0` 可
  退回舊行為。
- **續期時鐘改用成功紀錄**：同一個 session 重登會拿到同一顆 token，JWT `iat` 不會前進；舊的
  「簽發 25 分鐘就續期」於是每 5 分鐘重登一次。現在改看 `renew-watchdog.last-success`
  （`attempt_login()` 經 gateway 確認後才寫入；檔案缺失就回退 token 檔 mtime），`--status` 同時印
  `issued at` 與 `renewed at`。

驗證：`test/hidden_window_stability.py` 15/15（離線，只驗桌面／埠檔／PID 檔邏輯與 `--status`、`--ensure`、
`--close` 的決策，不開真的 Chrome）；`test/renew_stability.py` 的 `AgeRule`（時鐘來源、`iat` 凍結不重登）
與 `LoginWindowStability`（先隱藏、失敗才一個可見窗、冷卻期不再彈、沒有隱藏瀏覽器就不重試）6/6。
`um-renew.py --status` 的實際輸出見「續期時鐘」。本輪沒有觸發真實續期或 Duo 登入。

## 隱藏桌面登入其實一次都沒成功（2026-11-03）

上一輪上線的隱藏桌面，在真實排程上一次都沒成功過：`renew-watchdog.log` 裡每一輪都是
`hidden login window unavailable (the browser did not publish a DevTools port); the window will be visible`
（當天 9 次），也就是每次都退回可見視窗——功能寫了但等於沒生效。

- **根因**：`ensure_hidden_chrome()` 在 `CreateProcessW` 之後立刻 `CloseDesktop`（原本寫在 `finally`
  裡），而新行程此時還沒開啟那個桌面。桌面被銷毀，正在啟動的 Chrome 以 `0xC06D007E` 結束，
  DevTools 埠自然永遠不會出現。`Popen` 走的是另一條路徑（`STARTUPINFO.lpDesktop` 由 `subprocess`
  處理、桌面已經由父行程建立），所以手動測試容易看起來是好的。
- **修法**：把 `wait_for_devtools()` 包進 `try/finally`，確認瀏覽器起來後才關桌面。
- **順帶**：命令列改用 `subprocess.list2cmdline()` 組（原本是手動加引號，profile 路徑含空格或引號會
  被切成多個參數）。

實測（同一台機器、同一份 profile）：修好前 3/3 次 `devtools=False`，修好後 3/3 次 `devtools=True`；
`--ensure` 起好的瀏覽器 20 秒後仍在（`--status` 的 `desktopWindows` 有 `GENAI Chat (Open WebUI)`）；
`um-renew.py --force` 端到端走完，日誌出現 `login browser started on the hidden desktop (port 5050)`
且沒有再退回可見視窗。回歸測試 `DesktopLifetime`（3 支）把修法釘住：把 `CloseDesktop` 改回原本位置，
`test_the_desktop_stays_open_while_the_browser_starts` 立刻失敗並指出
`desktop was closed before the browser came up`。

驗證：Python 全套 53 支（`hidden_window_stability` 15、`scripts_stability` 19、`renew_stability` 19）在
非沙箱環境全綠；`node test/efficiency_unit.mjs` 10/10。

## 殘留的 PID 檔會殺錯人（2026-11-03）

登入成功後，`um_opencode_setup.py` 會用 `Browser.close` 關掉那個瀏覽器，但它只清 `browser-debug-port`，
**`hidden-window.pid` 留了下來**。而 `stop_hidden_chrome()`（`--close` 與續期失敗退回可見窗時都走它）
原本是「讀到 pid 就 `taskkill /PID`」。Windows 會回收 pid，所以那個號碼遲早會屬於別的行程——屆時
`--close` 就會隨機殺掉一個跟你無關的程序。

- **修法**：只有當 `profile_browser_pids()` 真的從命令列認出這個 profile 時，該 pid 才具備被殺的資格；
  沒有任何行程持有該 profile 就只清檔案、不動手。
- **順帶**：`--status` 現在只在 DevTools 真的回應時才回報 `pid`，否則回 `null`——留著一個死掉的號碼
  只會誘導人（或腳本）去殺它。

實測：殘留 pid `18132`（已死）下 `--close` → `{"stopped": [], "cleared": [...]}`，什麼都沒殺；
`--ensure` 起一個真的（pid `5780`）再 `--close` → `{"stopped": [5780]}`，行程確實消失。回歸測試
`StalePidFile`（4 支）蓋住這兩條路徑與 `--status` 的兩種情況。

驗證：Python 全套 **57 支**全綠（`hidden_window_stability` 19、`scripts_stability` 19、`renew_stability` 19）。

## 給貢獻者

```powershell
npm test
```

`npm test` 依序跑五組：

| 指令 | 內容 | 碰得到 8788 嗎 |
| --- | --- | --- |
| `npm run test:unit` | 純單元，兩支：`efficiency_unit`（single-flight、輪詢排程、狀態檔節流、`healthDetail` 判定）＋ `paths_unit`（家目錄與四個路徑推導、Python 偵測順序、`defaultsOf` 不含作者機器的路徑、`resolveScript` 的家目錄／`vendor/` 順序） | 不會 |
| `npm run test:always-on` | 常駐模式 9 項（啟動即喚醒、閒置不關、狀態端點、卸載不帶走代理） | 隨機回環埠 |
| `npm run test:stability` | `proxy_stability` ＋ `plugin_stability`（後者要 `--experimental-test-module-mocks`） | 隨機回環埠 |
| `npm run test:settings` | 設定頁 API（`lib/settings-api.js`） | 不會 |
| `npm run test:scripts` | Python 全套 `*stability.py` | 不會 |

幾個約定：

- **要 Node.js 24** 跑測試（`plugin_stability` 用實驗性的 module mock）；外掛本身在 22 以上就行。
- **`UM_TEST_PYTHON`** 指定測試用的 Python；不影響正式代理用的那一個。沒設就自動偵測。
- 生命週期測試在隨機回環埠啟停 `test/fixtures/proxy.py`，不會動到正式 8788，也不會開真實登入瀏覽器或讀真實憑證。
- **測試不能依賴這台機器。** `detectPython` 會掃 `C:\Python3*`，所以在裝了 Python 的機器上「什麼都找不到」那條測試會假性失敗——用 `detectPython(env, { systemRoot: <不存在的目錄> })` 把那一檔關掉。同理 `renew_stability` 要把 `LAST_SUCCESS_FILE` 指到暫存目錄，否則「距上次成功續期超過 25 分鐘」會讓測試忽然開始登入，通過與否取決於上一次真實續期是多久以前。
- 單跑一支：`node test/always_on.mjs`、`node test/efficiency_unit.mjs`、`node test/paths_unit.mjs`、`python -m unittest test.hidden_window_stability` 之類。
- **不要 commit 本機化設定。** `cordis.patch.yml` 必須保持通用值（只有 `port` 與 `alwaysOn`）；`package.json` 的 python 指令不要寫死某台機器的路徑。個人路徑請留在**你自己 profile** 的 `cordis.patch.yml` 或設定頁裡——那裡本來就是拿來覆寫的。同理，README 裡的範例路徑請用 `%USERPROFILE%` 或 `<本 repo 目錄>`，不要貼自己的絕對路徑。
- 改 `lib/client.js` 不需要建置步驟（手寫的模組載入器格式），存檔後重新整理頁面就生效；**新增**設定分頁才要重啟 DSH。
- 行為改動請附一支釘住它的回歸測試。這個 repo 的既有慣例是「測試名稱直接說明它防的是哪個事故」。
- **換掉 `vendor/um_opencode_setup.py`** 時：同時更新 `vendor/README.md` 的大小、行數與 sha256 前 16 碼（`git show HEAD:vendor/um_opencode_setup.py | sha256sum`），並保留 `.gitattributes` 的 `text eol=lf`——換行被轉成 CRLF，雜湊就對不上了。`npm run test:unit` 只驗證副本存在、開頭是代理腳本；雜湊要靠上面那行手動核對。

## 授權

本專案的程式碼（`lib/`、`scripts/`、`test/`、文件）是 MIT，見 [LICENSE](LICENSE)。

`vendor/um_opencode_setup.py` **不是**本專案所寫：它是 UM 校內發放的工具，著作權屬原發放單位（澳門大學）。放在這裡只是為了讓外掛在乾淨的機器上能直接運作；若原發放單位要求移除，刪掉 `vendor/` 目錄即可，外掛會退回「家目錄必須自備腳本」的行為並給出對應的錯誤訊息。詳見 [`vendor/README.md`](vendor/README.md)。


