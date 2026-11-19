# BU_Keep 凍結檔規則

檔名以 `BU_Keep_` 開頭的檔案是**凍結的 legacy 備份**，擁有者明確要求永遠保留：

1. **不修改、不重新命名、不刪除。** 要改內容，請另外建立新檔，不要動凍結檔。
2. 清單與雜湊在 `docs/BU_Keep.sha256`；`tests/test_bu_keep.py` 會檢查雜湊與檔案是否存在，被動到測試就會失敗。
3. 新增凍結檔：複製成 `BU_Keep_<名稱>_<版本>_legacy.*`，重新產生雜湊清單（只新增、不改既有列），並在下面登記。

## 已凍結

| 檔案 | 說明 | 線上原版 |
|---|---|---|
| `BU_Keep_MERGE_DECISIONS_v1_legacy.html` | 整合決策板 v1（兩套實作如何運作、衝突與決策卡）。加了一條 legacy 橫幅。2026-11-19 凍結。 | https://claude.ai/artifact/4632F8pJHaVQhyTXxLjQNB（不再更動） |
