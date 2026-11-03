# vendor/

## `um_opencode_setup.py`

UM 校內發放的單檔工具：安裝器、本機 OpenAI 相容代理、OpenCode 啟動器、診斷與移除，全部在一個檔案裡（Python 3.10+，只用標準庫）。

**為什麼放在這裡**：這個外掛只是它的生命週期管理器（喚醒、閒置停止、續期、隱藏桌面登入、設定頁）。沒有這個腳本，外掛裝起來不會有任何行為。原本它只存在於每台機器的 `~/.um-opencode/`，所以新機器裝完外掛必定失敗 —— 現在附一份在這裡當後備。

**載入順序**（見 `lib/paths.js` 的 `resolveScript()`）：

1. `UM_OPENCODE_HOME` 或 `~/.um-opencode/um_opencode_setup.py` —— 優先。使用者自己更新過、或 UM 發了新版，就以那份為準。
2. 本目錄的副本 —— 家目錄沒有時才用。
3. 兩邊都沒有 → `lib/proxy.js` 的預檢丟出 `proxy script not found: <路徑>`。

腳本自己的資料（token、`credentials.dat`、`proxy.log`、狀態檔）仍然寫在 `~/.um-opencode/`，不會寫進這個目錄。

**更新方式**：把新版覆蓋到 `vendor/um_opencode_setup.py`，同時更新下面的雜湊，然後 commit。

**本專案的改動**（與原發放版本的差異，只有這一處）：`ProxyHandler.upstream_with_retry()` 在 502/503/504 或連線例外時重試 3 次（1s / 3s / 6s 退避），並在重試前用 `probe_um_token()` 確認 token 是否真的被拒 —— 只有 probe 回 `False`（401/403）才呼叫 `renew_token_once()`。UM 的 gateway 在自家後端掛掉時會回 503 `upstream connect error`，那種情況重登沒有用，盲目重登反而會變成登入風暴。`GET /v1/models` 與 `POST /v1/chat/completions` 都走這條路徑。

| 項目 | 值 |
| --- | --- |
| 大小 | 109856 bytes |
| 行數 | 2787 |
| sha256（前 16 碼） | `f9b90c96cd96c1b1` |
| 來源 | UM 校內發放（`chat.genai.um.edu.mo` 的課堂工具） |

**授權**：本檔案不是本專案所寫，著作權屬原發放單位（澳門大學）。放在這裡是為了讓外掛在乾淨的機器上能直接運作；若原發放單位要求移除，刪掉這個目錄即可，外掛會退回「家目錄必須自備腳本」的行為，並給出對應的錯誤訊息。
