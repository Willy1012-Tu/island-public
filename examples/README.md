# 範例設定

這些檔案只示範格式，不會自動載入。`PLACEHOLDER_PROJECT` 與 `PLACEHOLDER_PRIVATE_DATA` 都是假路徑；使用前請替換為自己的位置。範例不包含登入資料、API 金鑰、橋接權杖或遠端裝置設定。

| 檔案 | 用途與替換欄位 |
| --- | --- |
| `desktop-settings.example.json` | 桌面 `settings.json` 的格式。將 `workspace` 改為實際專案目錄；`port: 0` 讓系統分配本機連接埠。 |
| `agy-mcp-config.example.json` | Antigravity 專案範圍 `.agents/mcp_config.json` 的 stdio 設定。替換 Python、腳本、`--project`、`cwd` 與 `--data-dir` 路徑；保留五個工具名稱。 |
| `collaboration-request.example.json` | `island_start_collaboration` 或 `POST /api/bridge/start` 的請求內容。將 `prompt` 改為實際任務；主導者與成員須互不重複。 |
| `git-autosave.example.json` | Git 備份 `.git/island-autosave.json` 的格式。預設停用備份與推送，供使用者手動選用。 |

建議讓工具產生設定。從專案根目錄執行：

```powershell
python desktop.py --workspace "C:\PLACEHOLDER_PROJECT\island-public" --data-dir "C:\PLACEHOLDER_PRIVATE_DATA\AgentDeskPublic"
python tools/setup_remote.py setup --data-dir "C:\PLACEHOLDER_PRIVATE_DATA\AgentDeskPublic"
python tools/setup_remote.py status --data-dir "C:\PLACEHOLDER_PRIVATE_DATA\AgentDeskPublic"
```

公開版測試副本使用獨立的 `AgentDeskPublic` profile，避免與既有私人版本共用設定或連錯正在執行的辦公室。請讓桌面與 `setup_remote.py` 的 `--data-dir` 都指向這個相同目錄，並將私人資料目錄放在專案之外。

桌面程式會在指定的 profile 保存工作目錄；`setup_remote.py` 會使用目前專案、Python 與指定的資料目錄建立 MCP 設定，並保留其他 MCP 伺服器。

手動編輯 MCP 設定時，請合併 `mcpServers` 中的 `island-office` 項目，避免覆蓋其他伺服器。這份設定只提供開始協作、查詢、取消與變更預覽；套用原專案的修改仍由使用者在辦公室確認。桌面程式須保持執行，MCP 才能連到同一個任務服務。

協作範例省略 `model` 與 `reasoning_effort`，使用本機目前設定的預設值。若要指定模型，先使用 `island_status` 查詢選項；`reasoning_effort` 僅適用於 Codex。請先確認各提供者可用並完成 CLI 登入；Claude 與 Antigravity 使用同一個 `agy` CLI 與帳號。

Git 備份需先有正常 Git 儲存庫及初始提交。公開版的 `--init-config` 預設同樣為 `enabled: false`、`push: false`；確認要備份的檔案與遠端後，再手動變更設定。只開啟 `enabled` 時建立本機提交；另開啟 `push` 才會嘗試推送至符合檢查的 GitHub 遠端。這個範例不設定遠端、不登入帳號，也不安裝開機啟動。

請勿從其他電腦或舊專案複製 `office-bridge.json`、`instance.json`、登入檔或權杖。橋接資料由正在執行的桌面程式產生，MCP 在本機私人資料目錄讀取；不要把這些資料放進範例或提交到儲存庫。
