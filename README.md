# Auto WikiGacha Research Automation

> 以 Playwright 驅動的 WikiGacha 瀏覽器自動化研究腳本。  
> 本專案僅供研究、教學與自動化流程觀察使用；不建議、也不授權用於商業、營利、流量操作、廣告收益操作或任何違反服務條款／法規的用途。

## 專案概述

`auto_wikigacha.py` 是一個針對 WikiGacha 網站流程所撰寫的瀏覽器自動化腳本。它會使用持久化 Chrome / Chromium profile 保存本機 session、cookies、local storage、Google 登入狀態與可選的伺服器同步狀態，方便在研究或課堂示範中重複觀察同一個使用者環境下的自動化行為。

這個工具的重點不是破解、繞過或操縱網站機制，而是展示如何以較保守的方式處理真實網頁自動化中的常見問題，例如：入口通知、動態 DOM、導頁造成的 execution context 中斷、抽卡流程狀態切換、錯誤證據保存、隱私遮罩，以及人工登入與自動化流程的切分。

## 功能特色

- **持久化瀏覽器 profile**：重複使用同一個 profile 目錄，保留 cookies、local storage 與登入狀態。
- **Bot / Manual 雙模式**：Bot 模式執行自動化流程；Manual 模式使用一般 Chrome 開啟網站，便於手動登入或設定伺服器同步。
- **Setup 模式**：專門用一般 Chrome 完成 Google 登入／同步設定，再交給 Bot 模式沿用相同 profile。
- **抽卡流程控制**：可指定固定抽取次數，也可以依頁面顯示的剩餘卡包數進行自適應流程。
- **入口通知處理**：嘗試辨識並關閉公告、活動、更新通知等遮罩層。
- **卡包不足與廣告恢復流程偵測**：可辨識卡包不足狀態、恢復按鈕、廣告關閉與獎勵確認等流程節點。
- **路由與結果頁恢復**：遇到結果頁、返回卡包頁或目標元素失效時，嘗試以較保守方式重新定位流程。
- **隱私導向證據紀錄**：錯誤時會保存 JSON 證據；例行證據、截圖、完整路徑與完整 traceback 預設關閉或遮罩。
- **可配置 XPath / Locale / Browser Channel**：當網站 UI 結構變動時，可透過 CLI 參數調整定位邏輯。

## 適用情境

本專案適合：

- 課堂或研究中展示 Playwright 自動化設計模式。
- 分析瀏覽器 profile、local storage、session 與登入流程的保存方式。
- 觀察動態網頁中的 modal、iframe、navigation、動畫與狀態切換。
- 討論自動化工具的隱私紀錄、錯誤證據與合規邊界。

本專案不適合：

- 商業營利或代刷服務。
- 產生不自然流量、廣告曝光、廣告點擊或獎勵互動。
- 規避網站限制、反自動化機制、驗證流程或存取控制。
- 任何違反 WikiGacha、Google、廣告網路、瀏覽器平台、第三方服務條款或當地法律的用途。

## 專案結構

```text
.
├── auto_wikigacha.py      # 主要自動化腳本
├── requirements.txt       # Python 套件依賴
└── README.md              # 專案說明文件
```

建議另外加入 `.gitignore`，避免把本機 profile、證據檔與截圖提交到 GitHub：

```gitignore
.wikigacha-profile/
wikigacha_results/
*.log
*.png
__pycache__/
.venv/
```

## 系統需求

- Python 3.10 或以上版本。
- Google Chrome 或 Chromium。
- Playwright Python 套件。
- 可正常開啟 WikiGacha 的網路環境。

`requirements.txt` 目前鎖定的主要套件包含：

```text
greenlet==3.5.0
playwright==1.59.0
pyee==13.0.1
typing_extensions==4.15.0
```

## 安裝方式

建議使用虛擬環境：

```bash
python -m venv .venv
source .venv/bin/activate
```

Windows PowerShell：

```powershell
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
```

安裝依賴：

```bash
pip install -r requirements.txt
python -m playwright install chromium
```

如果 `pip install -r requirements.txt` 因為檔案編碼出現錯誤，請先將 `requirements.txt` 另存為 UTF-8，再重新安裝。

## 快速開始

### 1. 首次登入／同步設定

使用一般 Chrome 開啟 WikiGacha，手動完成 Google 登入或伺服器同步：

```bash
python auto_wikigacha.py --setup --profileDir .wikigacha-profile
```

完成後請先關閉 Chrome 視窗，確保 profile 狀態完整寫入磁碟。

### 2. Dry Run 檢查定位邏輯

正式執行前建議先用 dry run，確認入口通知、卡包目標與證據紀錄可正常解析：

```bash
python auto_wikigacha.py --executionMode bot --dryRun --profileDir .wikigacha-profile
```

### 3. 指定抽取次數

例如只執行 3 次 pack-opening lifecycle：

```bash
python auto_wikigacha.py --executionMode bot --drawCount 3 --profileDir .wikigacha-profile
```

### 4. 依剩餘卡包數自適應執行

不指定 `--drawCount` 時，腳本會嘗試依頁面顯示的剩餘卡包數執行，直到觀察到完成狀態：

```bash
python auto_wikigacha.py --executionMode bot --profileDir .wikigacha-profile
```

### 5. Manual 模式

Manual 模式會用一般 Chrome 開啟網站，不透過 Playwright 控制，適合人工檢查登入狀態或同步設定：

```bash
python auto_wikigacha.py --executionMode manual --profileDir .wikigacha-profile
```

## 常用參數

| 參數 | 預設值 | 說明 |
|---|---:|---|
| `--drawCount` | `None` | 指定最多完成幾次開包流程；不指定時依頁面剩餘卡包數自適應執行。 |
| `--profileDir` | `.wikigacha-profile` | 持久化瀏覽器 profile 目錄。請勿提交到 Git。 |
| `--evidenceDir` | `wikigacha_results` | 錯誤證據與可選例行證據輸出目錄。 |
| `--executionMode` | 互動選擇 | `bot` 或 `manual`。非互動 stdin 預設 Bot 模式。 |
| `--setup` | `false` | 使用一般 Chrome 開啟網站，供手動登入／同步設定。 |
| `--dryRun` | `false` | 解析入口與目標，但不點擊抽卡目標。 |
| `--headless` | `false` | 以無頭模式執行瀏覽器。預設會顯示視窗。 |
| `--browserChannel` | `chrome` | 優先使用已安裝的 Google Chrome；失敗時退回 Playwright Chromium。 |
| `--locale` | `zh-TW` | 瀏覽器 locale。 |
| `--saveRoutineEvidence` | `false` | 保存例行 JSON 證據；預設只在錯誤時保存證據。 |
| `--saveScreenshotEvidence` | `false` | 保存截圖證據。截圖可能包含帳號狀態、廣告內容與頁面文字，預設關閉。 |
| `--evidencePrivacyMode` | `redacted` | 可選 `minimal`、`redacted`、`full`。`full` 僅適合私人本機除錯。 |
| `--showSensitiveConsolePaths` | `false` | 是否在 console 顯示完整本機路徑。 |
| `--showFullTraceback` | `false` | 是否顯示完整 Python traceback。 |

進階定位參數包含：

- `--returnToPackPageXPath`
- `--remainingPackCountXPath`
- `--insufficientPackHeadingXPath`
- `--recoverPackButtonXPath`
- `--adRewardConfirmButtonXPath`
- `--adOverlayCloseButtonXPath`

當 WikiGacha 的 DOM 結構改版時，可優先調整這些參數，而不是直接修改程式碼。

## 證據與隱私設計

腳本會在錯誤或指定保存例行證據時輸出 JSON 報告。預設採用 `redacted` 隱私模式，會遮罩或摘要化 URL、路徑、DOM 文字、localStorage key、user-agent 與 iframe URL 等資訊。

請注意：

- `--saveScreenshotEvidence` 會保存頁面截圖，可能包含個人帳號狀態、抽取結果、廣告內容或其他敏感資訊。
- `--evidencePrivacyMode full` 可能輸出完整 URL、路徑、文字與環境資訊，僅建議在私人本機除錯使用。
- `.wikigacha-profile/` 可能包含登入 session、cookies、local storage 與同步狀態，不應提交、分享或上傳到公開 repo。
- `wikigacha_results/` 可能包含除錯資料，也不應提交到公開 repo。

## 合規與安全建議

1. 執行前先閱讀並遵守 WikiGacha、Wikipedia / Wikimedia、Google、廣告網路、瀏覽器平台與相關第三方服務的條款與政策。
2. 優先使用 `--dryRun` 與小的 `--drawCount`，確認腳本只在預期範圍內運作。
3. 不要長時間無人監督執行。
4. 不要用於廣告曝光、點擊、獎勵、流量或任何互動指標的人為操作。
5. 不要繞過驗證、存取控制、速率限制、反自動化措施或其他保護機制。
6. 若網站、服務提供者、權利人或管理者要求停止，應立即停止使用。
7. 若你不確定特定用途是否合規，請勿執行，並先取得法律或服務提供者的明確許可。

## 免責聲明

本專案僅以「研究、教學、個人非商業測試」為目的提供。作者與貢獻者不鼓勵、不授權、也不承擔任何因使用者將本工具用於商業營利、流量操作、廣告操作、未授權自動化、規避限制、干擾服務、蒐集個資、侵害智慧財產權或其他違法／違規用途所產生的責任。

本專案與 WikiGacha、Wikipedia、Wikimedia Foundation、Google、Playwright 及其相關公司、組織或服務沒有任何從屬、代理、合作、授權、背書或保證關係。所有商標、服務名稱、網站內容與第三方素材均屬其各自權利人所有。

使用本工具前，使用者應自行確認所在地法律、網站服務條款、平台政策、廣告網路政策、個資保護法規、著作權規範與其他適用規範。任何使用、修改、散布或衍生行為所導致的帳號限制、資料遺失、法律爭議、民刑事責任、第三方索賠或其他損害，均由使用者自行承擔。

本專案以「現況」提供，不提供任何明示或默示保證，包括但不限於可用性、正確性、穩定性、安全性、合規性、特定用途適用性或不侵權保證。本 README 不構成法律意見；若需法律判斷，請諮詢合格法律專業人士。

## 授權

除非 repo 另有 `LICENSE` 文件，本專案預設僅供研究與教學用途參考，保留所有權利。若你要公開散布此專案，建議另外加入明確的授權條款，並清楚標示「Research & Educational Use Only」與「Non-Commercial Use Only」等限制。

## 貢獻原則

歡迎針對以下方向提出改進：

- 更穩健的錯誤處理與狀態診斷。
- 更清楚的 CLI 文件與執行範例。
- 更嚴格的隱私遮罩與本機資料保護。
- 合規風險提示與安全預設值。

請不要提交任何用於規避限制、繞過驗證、操縱廣告／流量、批量濫用服務或侵害第三方權益的變更。
