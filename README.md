# Agent Desk / IslandWorld

以三間 3D 辦公室呈現 Codex、Claude 與 Antigravity 的工作狀態，透過原生 CLI 交辦單人或團隊任務。團隊可以提出程式修改、進行靜態審查並統整結果；修改先保存在隔離副本，經使用者檢視差異後才套用至原專案。

另外保留 IslandWorld 小島模擬：Python 世界規則、A* 尋路、快慢雙迴圈，以及 Three.js／Pygame 畫面。角色閒置動作與台詞是程式設定的演出，不會呼叫模型。

這份公開快照包含程式碼、測試與假設定範例。登入憑證、工作歷史、橋接權杖、遠端裝置設定與執行產物由使用者在本機建立。

## 網頁版快速開始

需要 Python 3.11 以上。下列命令使用 Windows PowerShell；其他系統請調整虛擬環境路徑。Windows 桌面視窗與登入捷徑另見後節。

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-web.txt
.\.venv\Scripts\python.exe web_server.py
```

開啟 [模型辦公室](http://127.0.0.1:8000/monitor)。原小島位於 [/island](http://127.0.0.1:8000/island) 與 [根網址](http://127.0.0.1:8000/)。後文的 `python` 請使用同一虛擬環境中的解譯器，例如 `.\.venv\Scripts\python.exe`。

啟動網頁預設使用小島 `demo` 大腦，不需要模型登入。辦公室必須手動交辦才會建立模型任務。Three.js 與 OrbitControls 使用固定版本 `0.180.0` 的 CDN，載入 3D 畫面需要網路；不需要 Node.js、npm 或建置步驟。中文字型取自本機系統。CDN 或 WebGL 載入失敗時，辦公室卡片與任務表單仍可使用。

## CLI 與模型

需自行安裝可用的原生 Codex CLI 或 Antigravity CLI，並使用自己的帳號登入。程式使用 CLI 的既有登入，不要求另外建立模型 API 金鑰；實際任務仍使用提供者的網路服務、帳號權限與用量。

```powershell
python tools/setup_providers.py status
python tools/setup_providers.py login codex
python tools/setup_providers.py login claude
python tools/setup_providers.py login antigravity
```

Codex 使用 Codex CLI；Claude 與 Antigravity 都使用 `agy` 及同一個 Antigravity 帳號，不要求另行登入 Claude Code。找得到 CLI 只代表已安裝，登入與模型實際存取需由任務結果確認。模型及 Codex 思考深度以後端列出的選項為準。

Windows 可選用 `python tools/setup_providers.py install-antigravity`，依官方 manifest 下載並驗證 SHA-512，放入本專案 `.tools`，不修改全域 PATH。只有 IDE 啟動器不足以提供 headless 任務串流。

Codex 模型清單先查詢原生 CLI 中繼資料，再退回本機快取；都不可用時只提供已儲存的模型／深度設定。清單有短期快取，不代表所有選項都已獲帳號授權。模型與深度由後端允許清單驗證，每個任務固定保存交辦時的選擇；切換表單不會更改既有任務，也不會改寫 CLI 的預設設定。

## 交辦與團隊協作

1. 查看工作目錄與 CLI 狀態，選擇辦公室、模型及支援的思考深度。
2. 選「單人任務」或「團隊協作」。團隊以目前房間為主導者，另選一至兩位空閒夥伴。
3. 輸入任務並啟動。可查看公開回覆、工具事件、耗時與 CLI 回報用量。
4. 團隊紀錄可切換總任務與子任務；取消其中一個協作任務會停止整個團隊。
5. 修改完成後先檢視差異，確認才按「套用修改」。

辦公室只追蹤從本介面或 MCP 啟動的任務，不自動接管外部 IDE 聊天。每間辦公室同時只接受一份工作。團隊會保留所選成員；等待交接的成員顯示等待狀態，只有實際 CLI 工作驅動工作演出。主導者不一定使用所有勾選成員，但至少要完成一次真實委派。

團隊最多四輪主導決策、六件委派工作，總計最多 20 分鐘。主導者以 JSON 決定下一步，夥伴依序在共用副本執行，再由主導者統整。工作者不能自行遞迴啟動其他模型。

工作目錄在服務啟動時固定；以下路徑只是通用範例：

```powershell
python web_server.py --monitor-workspace "C:\Projects\example-project" --monitor-timeout 300
```

副本只收錄允許的文字來源檔與文件，排除隱藏目錄、憑證類名稱、環境檔、產物與檔案系統連結；來源範圍最多 2000 檔、每檔 1 MiB、合計 20 MiB。每輪提供有界的實際來源內容及截斷資訊。唯讀工作者需交付 `summary`、`findings`、`limitations`、`complete` 報告，未完成或格式無效會標記失敗。公開紀錄不包含私有推理。

實作工作預設保持 CLI 唯讀，由模型提出檔案內容，主程式驗證後寫入自己持有的隔離副本。每次提案最多 32 KiB UTF-8 JSON、12 項檔案操作；支援 `create`、`replace`、`delete`，建立／替換須提供完整內容。主程式不把提案當作 shell 指令執行。

patch、新檔內容與雜湊清單儲存在工作目錄的 `artifacts/collaborations/{任務ID}/`。只有成功完成的團隊可套用：先顯示相符產物的差異，再由使用者按鈕確認。套用前檢查原檔雜湊，遇到衝突回傳 `409`；失敗或取消的產物可檢視與下載，不能套用。報告會保留未執行測試等限制，請依實際內容審查。

## Windows 桌面版：選擇性使用

桌面版以 pywebview／EdgeChromium 與 pystray 包裝本機服務，需要 WebView2 Runtime。先安裝依賴；測試不同副本時建議使用自己的資料目錄：

```powershell
python -m venv .venv-desktop
.\.venv-desktop\Scripts\python.exe -m pip install -r requirements-desktop.txt
.\.venv-desktop\Scripts\pythonw.exe desktop.py --workspace . --data-dir "$env:LOCALAPPDATA\AgentDeskPublic"
```

`--data-dir` 應位於專案外，儲存設定、公開歷史、日誌、WebView 快取與本機橋接資訊。相同資料目錄只啟動一個實例，重複開啟會顯示原視窗。不同副本的預設資料目錄與捷徑名稱可能相同，使用獨立目錄可避免誤用另一份工作區設定。

按視窗 X 會隱藏至系統匣並繼續既有工作；系統匣「離開」會關閉服務並取消它擁有的任務。沒有可用系統匣時，X 直接退出。啟動桌面不會自動交辦模型任務，恢復歷史也不會重播中斷工作。

只有確定需要**預設桌面設定與登入自動啟動**時，才選用：

```powershell
.\.venv-desktop\Scripts\python.exe desktop.py --install
```

這個命令會建立目前使用者的桌面 `Agent Desk` 捷徑，並啟用 Windows 登入後開啟辦公室的 Startup 捷徑；不需要管理員服務。它使用預設資料目錄，不能與自訂 `--data-dir` 合用。系統匣的登入選項也管理這個預設捷徑。助手遇到不屬於此副本的同名捷徑會拒絕覆寫；請先規劃好副本與資料目錄再安裝。

停用此副本管理的登入捷徑：

```powershell
.\.venv-desktop\Scripts\python.exe desktop.py --disable-startup
```

這些桌面命令不會安裝 Git 自動備份。WebView2 與桌面平台需求見 [pywebview 安裝文件](https://pywebview.flowrl.com/guide/installation.html)。

## MCP 與遠端助理

先開啟桌面版，再設定專案範圍的 MCP。若使用前節的獨立資料目錄，兩者必須指向相同位置：

```powershell
.\.venv-desktop\Scripts\python.exe tools/setup_remote.py setup --project . --data-dir "$env:LOCALAPPDATA\AgentDeskPublic"
.\.venv-desktop\Scripts\python.exe tools/setup_remote.py status --project . --data-dir "$env:LOCALAPPDATA\AgentDeskPublic"
```

助手只管理 `.agents/mcp_config.json` 的 `island-office` 項目與自己的標記，保留其他伺服器及 Ask 權限。設定是本機檔案，不隨公開快照提供。MCP 不自動啟動後端；`office_ready` 必須由實際執行的桌面擁有者提供。

| 工具 | 功能 |
| --- | --- |
| `island_status` | 查詢辦公室及可用模型 |
| `island_start_collaboration` | 建立團隊任務，立即取得總任務 ID |
| `island_get_task` | 查詢團隊、子任務與公開結論 |
| `island_cancel_task` | 明確取消團隊 |
| `island_review_changes` | 預覽隔離副本差異 |

MCP 沒有套用原檔的工具。工作目錄與資料目錄在程序啟動時固定，不接受任意網址、命令或 cwd。斷線／取消等待不會取消辦公室工作，需用取消工具明確停止。

MCP 採 UTF-8 逐行 JSON-RPC stdio，支援初始化握手規格至 `2025-11-25` 與較早版本。輸入最多 128 KiB，公開結果會依 96 KiB 全域預算節錄，完整回應最多 512 KiB；節錄會保留任務識別碼、狀態與提示，大型報告請回到辦公室查看。橋接只連 loopback，權杖由桌面在本機私人資料目錄產生。

若要從手機連入主機，請另外依 Antigravity 的遠端控制流程設定自己的裝置與帳號；本專案提供 MCP 入口，不自動註冊遠端裝置，也不附帶任何裝置連線資料。

## 原有小島、Pygame 與 headless

小島使用 `IslandWorld → AgentBrain → JSON 驗證 → A*／動作佇列 → Tick`。快迴圈預設每 0.5 秒移動、採集或休息；慢迴圈只在沒有目標時背景思考。網頁 HUD 顯示動作、公開角色心聲、體力、無聊值與小遊戲。Space 暫停／繼續，B 增加無聊值。

Pygame 與純 CLI 範例：

```powershell
python -m pip install -r requirements.txt
python main.py --brain demo
python main.py --brain demo --headless --ticks 30
python web_server.py --brain codex --boredom 85
```

`main.py` 預設是 Codex 大腦；前兩個執行範例明確選擇不呼叫模型的 demo。最後一個命令需 CLI 登入，會依小島進程自動發出角色決策請求，與辦公室手動任務分開。`--headless` 不匯入 Pygame，完整參數見各入口的 `--help`。

## 假設定與可選 Git 工具

[examples/](examples/README.md) 提供桌面設定、AGY MCP、協作請求與 Git 備份的格式範例。`PLACEHOLDER_PROJECT`、`PLACEHOLDER_PRIVATE_DATA` 都是假路徑，需替換後才可使用；範例不會自動載入。不要將正在使用的登入檔、橋接記錄或工作歷史複製成範例。

**公開版不安裝或預設啟用 Git 自動備份。** `tools/git_autosave.py` 與相關助手保留供自行選用；即使執行 `--init-config`，預設仍是 `enabled: false`、`push: false`，每 2 秒檢查、安靜 8 秒後處理。

使用者需自行確認儲存庫、初始提交與遠端，再手動啟用。僅啟用 `enabled` 時建立本機快照，另啟用 `push` 才會嘗試一般推送。目標是該儲存庫自行設定的 upstream，不會同步到另一份私人備份庫；公開快照不攜帶舊 Git 歷史、私人遠端設定或憑證。Git 備份登入捷徑也需要另外明確安裝。

## 測試

```powershell
python -m pip install -r requirements-dev.txt
python -m pytest -q
```

有 Node.js 時可執行前端行為測試，不需要 npm 套件：

```powershell
node --experimental-vm-modules --test tests/web_app.test.mjs tests/monitor_app.test.mjs tests/office_scene.test.mjs
```

一般測試使用假的 CLI／GUI 或暫存專案，不需要模型登入；部分平台能力不足時會跳過相關案例。這些測試不代表使用者的原生視窗、瀏覽器、帳號或所有模型組合皆已驗證。`tools/smoke_collaboration.py` 屬於另行選用的真實 CLI 測試，會使用帳號用量與一次性玩具專案。
