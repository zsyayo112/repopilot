# `<=` 查询漏掉了恰好等于边界值的记录

## 问题描述

用 `<=` 做范围查询时，字段值**恰好等于**比较值的记录不会被返回。
`<`、`>`、`>=` 都正常，只有 `<=` 有这个问题。

## 复现步骤

```python
from tinydb import TinyDB, Query
from tinydb.storages import MemoryStorage

db = TinyDB(storage=MemoryStorage)
db.insert({'name': 'alice', 'age': 22})
db.insert({'name': 'bob', 'age': 30})

User = Query()
print(db.search(User.age <= 22))
```

## 预期结果

返回 alice（age=22，满足 age <= 22）。

## 实际结果

返回空列表 `[]`。但 `db.search(User.age <= 23)` 能返回 alice，
所以看起来是边界值本身被排除了。

## 环境

- tinydb：本仓库当前版本
- Python 3.12
