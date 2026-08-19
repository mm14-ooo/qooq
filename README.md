# qooq

有長期記憶的互動小說引擎。敘事由 Claude 即時生成，但故事的「事實」存在一個
可查詢的 SQLite 記憶庫裡 —— 所以第 80 回合的 NPC 仍然記得第 3 回合你答應過他什麼。

## 為什麼需要記憶系統

把整段對話塞回 context 只能撐到上下文用完為止，而且模型會被最近幾回合的細節
淹沒，忘掉更早但更重要的承諾。qooq 的做法是把「記得什麼」變成一個明確的、可以
檢查與除錯的子系統：每回合結束後有一個**記錄員**把剛剛的散文讀回成結構化紀錄，
下一回合再依相關性把該記得的部分取回來。

## 安裝

```bash
pip install -e .
export ANTHROPIC_API_KEY=sk-ant-...     # 或執行 ant auth login
```

## 開始玩

```bash
qooq worlds                 # 列出內建世界
qooq new mystory            # 建立存檔（不需連線，不花錢）
qooq play mystory -v        # 開始遊玩，-v 會顯示每回合取用與寫入了哪些記憶
```

遊玩中的指令：

| 指令 | 作用 |
| --- | --- |
| `/memory` | 印出整個記憶庫：實體、屬性、關係、旗標、章節、近期事件 |
| `/state` | 以 JSON 顯示注入 prompt 的狀態快照 |
| `/recall <關鍵字>` | 顯示檢索排序與各項分數，用來調權重 |
| `/log` | 最近的敘事紀錄 |
| `/quit` | 離開（隨時都已存檔） |

存檔外也可以直接查：

```bash
qooq memory mystory              # 檢視記憶
qooq recall mystory "銅鑰匙"      # 測試檢索排序，含分數拆解
qooq log mystory --limit 20
```

## 記憶架構

記憶分三層，因為它們的用途完全不同：

| 層 | 內容 | 注入策略 |
| --- | --- | --- |
| **狀態** `entities` `facts` `relations` `flags` | 現在是真的事：誰活著、誰欠誰、哪扇門開了 | 每回合**全部**注入。它小而穩定 |
| **情節** `events` | 曾經發生的事，一回合可能好幾則 | 依相關性**取回前 K 則** |
| **章節** `chapters` | 舊情節壓縮成的前情提要 | 全部注入。它取代了它壓縮掉的東西 |

再加上最近 N 回合的**逐字原文**，用來維持語氣與代名詞的連貫。

### 屬性是 append-only

改變一項屬性不會覆寫舊值，而是插入新值並把舊值標為 superseded。所以
「她在第二章時相信什麼？」永遠答得出來，而 prompt 裡只會出現當前值。

```
facts: (老陳, 傷勢, 無, turn=1, superseded_by=7)
       (老陳, 傷勢, 左肩中刀, turn=5, superseded_by=NULL)   ← 只有這行進 prompt
```

### 檢索：四個訊號的加權和

單一訊號都不夠用，所以 `MemoryStore.retrieve` 把四種混在一起（權重見
`qooq/config.py` 的 `RetrievalWeights`）：

- **字面比對** — FTS5 over 預先分詞的文字，找對主題。
- **實體重疊** — 這一回合提到的人事物參與過哪些事件；用詞不同也抓得到。
- **重要度** — 記錄員給的 1–5 分，讓劇情關鍵永遠搆得著，不會被日常瑣事推出去。
- **時間衰減** — 其他條件相同時偏好剛發生的事。

外加同地點加分。查詢字串是「玩家輸入 + 上一段敘述」，因為玩家常常只打
「問他那件事」，缺的名詞得由場景補上。

中英混排的分詞在 `qooq/textutil.py`：SQLite 內建的 `unicode61` 會把整串中文
當成一個 token，`trigram` 又讓英文排序失準，所以改成在 Python 端切成
單字 + 雙字組再餵給 FTS5，讀寫兩側用同一個函式。

### 壓縮

活躍事件累積到門檻（預設 60 則）時，把 15 回合以前的事件交給編輯模型寫成一段
前情提要，原事件留在資料庫但退出檢索池。故事再長，context 也有上界。

### Prompt 分層

```
system   世界設定 + 敘事守則        ← 固定不變，掛 cache_control
messages 最近 N 回合的逐字原文
         【玩家行動】…              ← user，不可信輸入
         記憶簡報                   ← system（operator channel）
```

在支援 mid-conversation system message 的模型（Opus 5 / Opus 4.8 / Fable 5 /
Mythos 5）上，記憶簡報走 operator channel，玩家沒辦法打一段看起來像記憶紀錄的
字就把它變成正史。不支援的模型會自動退回把簡報併進 user message。

## 寫自己的世界

複製 `qooq/worlds/lantern.json` 改就好：

```json
{
  "title": "作品名",
  "premise": "一段話說清楚玩家是誰、現在卡在什麼處境",
  "setting": "世界觀",
  "tone": "文體與基調 —— 這欄很有用，直接決定句子長相",
  "rules": ["敘事引擎絕不可違背的設定"],
  "opening": "第 0 回合的開場白，逐字使用",
  "seed_entities": [
    {"name": "掌櫃", "kind": "character", "importance": 5,
     "aliases": ["你"], "facts": {"身分": "當鋪掌櫃"}}
  ],
  "seed_flags": {"夜次": "第七夜"}
}
```

`kind` 可用 `character` / `location` / `item` / `faction` / `concept`。
`qooq new` 完全離線：seed 直接寫進記憶庫，開場白逐字當第 0 回合，不呼叫 API。

## 調整

`qooq/config.py` 是唯一的旋鈕集中處：

| 設定 | 預設 | 說明 |
| --- | --- | --- |
| `verbatim_turns` | 4 | 逐字保留的回合數。調高語氣更連貫，成本上升 |
| `retrieved_memories` | 8 | 每回合取回的往事數 |
| `consolidate_after_events` | 60 | 觸發壓縮的活躍事件數 |
| `narrator_effort` | `high` | 大場面可調 `max` |
| `RetrievalWeights` | 見檔案 | 用 `qooq recall` 觀察分數拆解後再調 |

環境變數 `QOOQ_NARRATOR_MODEL`、`QOOQ_ARCHIVIST_MODEL`、`QOOQ_EDITOR_MODEL`
可以分別換模型，`QOOQ_SAVE_DIR` 換存檔位置。三者預設都是 `claude-opus-5`；
記錄員與編輯是機械性工作，若要省成本可以單獨降級：

```bash
export QOOQ_ARCHIVIST_MODEL=claude-sonnet-5
```

## 每回合的成本結構

一個回合是**兩次** API 呼叫：敘事（串流）與記錄員（結構化輸出）。壓縮觸發時多一次。
世界設定掛了 `cache_control`，但 prompt caching 的最小前綴約 1024 tokens ——
設定太短的世界不會命中快取，這是預期行為。

## 測試

```bash
pip install pytest && python3 -m pytest tests/ -q
```

32 個測試，全部離線：`tests/fake_client.py` 是一個會錄下 request 的假 client，
所以整條 turn pipeline（檢索 → 組 prompt → 敘事 → 抽取 → 壓縮）都不需要金鑰
就能驗證，包括送出的 request 形狀。

## 已知限制

- 檢索是字面 + 圖關聯的混合式，沒有語意向量。同義改寫（「那個承諾」對上
  「他答應過的事」）目前靠實體重疊與重要度兜住，不是靠語意相似度。
- 記錄員可能漏記。`-v` 會即時顯示寫入了什麼，發現漏掉可以下一回合補述。
- 沒有 undo。回合一寫入就是正史。
