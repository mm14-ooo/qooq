# qooq

有長期記憶的互動小說引擎。**不接 API、不需要金鑰**——你和 AI 之間用複製貼上傳接球，
所有資料存在本機一個 SQLite 檔裡。

```
你寫行動 → app 產生 prompt → 你貼給 AI → AI 回一段故事 + JSON → 你貼回 app → app 存起來
```

重點在於 app 每次產生的 prompt 不是把整段對話重貼一遍，而是**依這一回合的內容，
從記憶庫裡挑出該記得的部分**。所以第 80 回合的 NPC 仍然記得第 3 回合你答應過他什麼，
而 prompt 不會越來越長。

## 開始

```bash
pip install -e .          # 零依賴，只用標準函式庫
qooq new mystory          # 建立存檔
qooq serve mystory        # 開啟網頁介面，瀏覽器會自動打開
```

介面分兩步：寫下行動 → 按「產生 Prompt」（會自動複製到剪貼簿）→ 貼給任何 AI →
把整段回覆貼回下面的框 → 送出。散文和 JSON 一起貼就好，app 會自己拆開。

右側欄即時顯示記憶庫的內容：現在有哪些人物、他們的屬性、關係、劇情旗標、近期事件。
最下面的「檢索測試」可以輸入關鍵字，看檢索排序和每項分數，用來判斷記憶有沒有正常運作。

也有終端機版本（適合寫腳本）：

```bash
qooq prompt mystory "掀開麻布" > prompt.txt   # 產生 prompt
qooq ingest mystory --file reply.txt          # 讀入 AI 的回覆
qooq memory mystory                           # 檢視記憶
qooq recall mystory "銅鑰匙"                   # 測試檢索排序
```

## 一回合貼一趟：散文和記憶同時產生

最直覺的做法是貼兩趟——一趟要故事，一趟要 AI 把故事轉成紀錄。qooq 只貼一趟：
prompt 要求 AI 先寫散文，然後在同一則回覆尾巴附一段 JSON：

````
你把麻布掀開一角。鐵櫃裡那段記憶沒有形狀，只有溫度——比手心涼一點。

老陳的聲音從門口傳來：「別碰它。」他站在雨裡，沒有進來。

```json
{"scene": "當鋪後間",
 "events": [{"summary": "掌櫃掀開麻布查看無名記憶，老陳在門口出聲阻止",
             "importance": 4, "participants": ["掌櫃", "老陳", "無名記憶"]}],
 "entities": [{"name": "老陳", "facts": {"態度": "阻止掌櫃碰觸無名記憶"}}],
 "flags": {"無名記憶": "麻布已掀開"}}
```
````

app 把散文存進故事、JSON 存進記憶庫，你在介面上只會看到散文。

### 解析器必須夠韌

真實的聊天 AI 不會每次都乖乖照格式。`qooq/protocol.py` 的解析分四段降級：
有 `json` 標籤的程式碼區塊 → 任何能解析的區塊 → 沒包區塊的裸 JSON → 只有散文。

實測涵蓋的狀況（`tests/test_protocol.py`，18 個測試）：AI 在 JSON 後面多講一句話、
用了沒有標籤的圍籬、JSON 有多餘的逗號、散文裡本身就有大括號、JSON 字串裡包著大括號、
先示範一次格式範本再給真的紀錄、`flags` 給成 `[{key, value}]` 而不是物件、
`relations` 用 `source`/`target` 而不是 `from`/`to`。

**JSON 壞掉不會吃掉你的回合**：散文一定先存進資料庫，記憶寫入失敗只會跳一個提示。

## 記憶架構

記憶分三層，因為用途不同：

| 層 | 內容 | 進 prompt 的方式 |
| --- | --- | --- |
| **狀態** `entities` `facts` `relations` `flags` | 現在是真的事：誰活著、誰欠誰、哪扇門開了 | **全部**放進去。它小而穩定 |
| **情節** `events` | 曾經發生的事 | 依相關性**只挑前 8 則** |
| **章節** `chapters` | 舊情節壓縮成的前情提要 | 全部放進去。它取代了它壓縮掉的東西 |

再加上最近 4 回合的**逐字原文**，維持語氣與代名詞的連貫。

### 屬性是 append-only

改變屬性不會覆寫舊值，而是插入新值並把舊值標成 superseded。所以
「她在第二章時相信什麼？」永遠答得出來，而 prompt 裡只出現當前值。

```
facts: (老陳, 態度, 冷淡,  turn=1, superseded_by=7)
       (老陳, 態度, 阻止,  turn=5, superseded_by=NULL)   ← 只有這行進 prompt
```

### 檢索：四個訊號的加權和

這部分**完全是程式，沒有 AI 參與**（`qooq/memory.py` 一次網路呼叫都沒有）：

- **字面比對** — FTS5 over 預先分詞的文字，找對主題。
- **實體重疊** — 這回合提到的人事物參與過哪些事件；換句話說也抓得到。
- **重要度** — 1–5 分，讓劇情關鍵永遠搆得著，不會被日常瑣事推出去。
- **時間衰減** — 其他條件相同時偏好剛發生的事。

排序是確定性的，同樣的查詢永遠得到同樣分數，所以介面上的「檢索測試」可以拿來調權重。
但它排序的**特徵**（重要度、參與者、事件摘要）是 AI 在寫入時給的——
**AI 在寫入時判斷，程式在讀取時排序**。

中英混排的分詞在 `qooq/textutil.py`：SQLite 的 `unicode61` 會把整串中文當成一個 token，
`trigram` 又讓英文排序失準，所以在 Python 端切成單字＋雙字組再餵 FTS5，讀寫共用同一個函式。

### 壓縮

活躍事件累積到門檻（預設 60 則）時，介面會出現「記憶壓縮」區塊，
產生第二種 prompt 讓你貼一趟，把舊事件換成一段前情提要。原事件留在資料庫但退出檢索池。
故事再長，prompt 都有上界。這一步很少觸發，大約每 60 則事件一次。

## 寫自己的世界

複製 `qooq/worlds/lantern.json` 改：

```json
{
  "title": "作品名",
  "premise": "一段話說清楚玩家是誰、現在卡在什麼處境",
  "setting": "世界觀",
  "tone": "文體與基調 —— 這欄很有用，直接決定句子長相",
  "rules": ["敘事絕不可違背的設定"],
  "opening": "第 0 回合的開場白，逐字使用",
  "seed_entities": [
    {"name": "掌櫃", "kind": "character", "importance": 5,
     "aliases": ["你"], "facts": {"身分": "當鋪掌櫃"}}
  ],
  "seed_flags": {"夜次": "第七夜"}
}
```

`kind` 可用 `character` / `location` / `item` / `faction` / `concept`。
然後 `qooq new mystory --world 你的檔案.json`。

## 調整

`qooq/config.py` 是唯一的旋鈕集中處：

| 設定 | 預設 | 說明 |
| --- | --- | --- |
| `verbatim_turns` | 4 | 逐字保留的回合數。調高語氣更連貫，prompt 變長 |
| `retrieved_memories` | 8 | 每回合取回的往事數 |
| `consolidate_after_events` | 60 | 觸發壓縮的活躍事件數 |
| `RetrievalWeights` | 見檔案 | 用介面的「檢索測試」觀察分數後再調 |

`QOOQ_SAVE_DIR` 可以換存檔位置。

## 資料就是一個 SQLite 檔

存檔是自足的：世界設定和記憶都在同一個檔裡，可以直接用 `sqlite3` 打開查。

```bash
sqlite3 saves/mystory.db "SELECT turn, importance, summary FROM events ORDER BY importance DESC LIMIT 10"
```

## 測試

```bash
pip install pytest && python3 -m pytest tests/ -q
```

53 個測試，全部離線、不需要金鑰：記憶與檢索、解析器韌性、完整的回合週期
（產 prompt → 中途關掉重開 → 貼回 → 寫入 → 壓縮）。

## 已知限制

- 檢索是字面＋圖關聯的混合式，**沒有語意向量**。同義改寫（「那個承諾」對上
  「他答應過的事」）靠實體重疊與重要度兜住，不是靠語意相似度。
- AI 可能漏記或記錯。介面每回合會顯示寫入了什麼，發現不對可以直接改資料庫。
- 沒有 undo。回合一寫入就是正史。
- 單人本機使用，伺服器綁在 127.0.0.1，沒有帳號或權限機制。
