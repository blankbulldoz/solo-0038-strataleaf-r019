# 受限 SQL 查询入口

`Database.sql(statement, limit=100, cursor=None, revision=None, force_scan=False)` 为已有的
`Database.query` 与 `Database.aggregates.group` 提供一个文本入口。它不是 SQLite 直通：语句由本仓库中的
封闭语法解析成与 Python 入口相同的谓词对象、读版本、索引选择和签名游标，再走同一条执行路径。
用户文本从不拼接进内部 SQLite 语句；SQLite 只收到仓库自身写死的常量查询，解析出的字面量始终以
JSON 值形式作为参数或谓词值传递。

Python 查询 API 完全保留，两个入口共享语义，可以按场景任选。

## 支持范围

单个集合、一条 `SELECT`：

```
SELECT * FROM collection [WHERE condition] [LIMIT n]
SELECT path [AS alias], ... FROM collection [WHERE condition] [LIMIT n]
SELECT group_path [AS alias], ... aggregate(...) [AS alias], ...
  FROM collection [WHERE condition] GROUP BY group_path [, ...]
```

- 字段路径为点分隔的 `a.b.c`；`SELECT *` 返回完整文档。
- 比较运算符：`=`、`==`、`!=`、`<>`、`<`、`<=`、`>`、`>=`。
- 成员与包含：`IN (...)`、`NOT IN (...)`、`CONTAINS`、`NOT CONTAINS`。
- 空值/缺失：`IS NULL`、`IS NOT NULL`、`IS MISSING`、`IS NOT MISSING`。
- 布尔组合：`AND`、`OR`、`NOT`、圆括号，与 Python 谓词的嵌套规则一致（嵌套 16 层、每个布尔节点 64 项）。
- 字面量：单引号字符串（`''` 转义单引号）、整数、有限浮点、`TRUE`、`FALSE`、`NULL`。
- 聚合：`count(*)`、`count(path)`、以及 `path` 上的 `sum`、`min`、`max`、`avg`。

分组查询的输出列按 SELECT 顺序排列，列名取 `AS` 别名，否则取字段路径或函数名。文档查询每行是
`{"key", "revision", "document"}`，`document` 为完整文档或所选字段的子集。

## 明确不支持的语法

返回 `UnsupportedSql`（语句看起来是 SQL，但超出子集）：多语句、注释、绑定参数（`?`、`$1`、`:name`）、
JOIN、多集合/逗号连接、子查询、`ORDER BY`、`OFFSET`、`HAVING`、`DISTINCT`、`TOP`、`LIKE`、`BETWEEN`、
`IN` 中的 `NULL`、与 `NULL` 的比较、非 SELECT 语句、分组查询上的 `LIMIT` 与游标续页。

返回 `SqlParseError`（语法本身不成立）：空语句、缺少必需子句或标点、非法数字/字符串字面量、未知语句
关键字、结构合法但运算符缺失等。两者都继承 `SqlError`，且都不会触碰存储。

子集把部分单词当作保留字，但仅按位置生效：`FROM`、`WHERE`、`GROUP`、`LIMIT` 等出现在子句位置时被解释为
子句关键字，因此直接 `SELECT from FROM n` 会失败；同名字段仍可参与筛选、`SELECT *` 或作为 `AS` 别名
（如 `SELECT msg AS from FROM n`）。`AND`、`OR`、`NOT`、`IN`、`IS`、`NULL`、`MISSING`、`TRUE`、`FALSE`
在表达式位置解释为运算符或常量，若字段名与之相同，请用 `AS` 别名区分或改变字段名。

`LIMIT` 越界与 Python 入口一致，抛 `InvalidDocument("page size must be between 1 and 1000")`；
未知集合抛 `NotFound`。游标相关错误仍为 `InvalidCursor`。

字段路径使用点分隔语法，因此 `SELECT meta.depth FROM ...` 选择嵌套值是支持的；`SELECT *` 只作用于文档选择，
分组查询中必须显式列出分组字段。注意 `*` 只在 `count(*)` 中表示“整行计数”，不与 `SELECT *` 混用。

## 缺失字段、显式空值与数值比较

这些语义直接复用 `predicates.matches`，因此两个入口完全一致：

- **缺失字段**：路径不存在时任何比较（等值、范围、`IN`、`CONTAINS`）都不匹配。区分两种情况必须用
  `IS MISSING` / `IS NOT MISSING`，或 Python 的 `{"op": "exists"}`。
- **显式空值**：JSON `null` 是一个普通值，`IS NULL` 匹配它；`IS NULL` 不匹配缺失字段。
  `region = NULL` 被拒绝（`UnsupportedSql`），因为 SQL 三值逻辑会静默不匹配，容易误用；
  要匹配 null 请写 `region IS NULL`，要在 Python 中表达则用 `{"field": "region", "op": "eq", "value": None}`。
- **数值比较**：布尔值不是数字，`TRUE`/`FALSE` 与任何数值比较都不匹配；数字与数字比较，字符串只与
  字符串比较，跨类型不匹配。`1` 与 `1.0` 视为相等数值。
- **分组**：分组字段缺失或值不可序列化时跳过该文档；显式 `null` 形成自己的分组（`IS NULL` 组）。
  数值度量只统计真正的数字，布尔与字符串不计入 `sum`/`avg` 的分母；`count(*)` 统计组内所有文档，
  `count(path)` 只统计该路径存在的文档。空输入上的全局聚合返回一行（`count` 为 0，其余为 `null`）。

## 固定修订与分页

- `revision=` 固定读版本，越界或早于回收水位时与 `Database.query` 一样抛 `InvalidDocument` / `SnapshotTooOld`。
- 不带 `revision` 时读取当前 head；首个分页由此固定，后续页通过签名游标携带同一修订，写入不会改变后续页内容。
- `LIMIT n` 决定页大小；未写 `LIMIT` 时使用调用的 `limit=` 参数（1..1000）。
- 游标与 Python 入口使用同一 `cursors` 签名方案，绑定数据库身份、谓词摘要、读版本与最后键。把 SQL 得到的
  游标交给谓词不同的查询（或交给 Python 入口的不同谓词）会抛 `InvalidCursor`。
- 分组结果一次计算完成，不返回游标；对分组计划传入 `cursor` 会得到 `UnsupportedSql`。

## 两种入口何时等价

**等价**是指返回相同的键集合/行、相同的修订与相同分页边界，且同样的错误类别。以下配对逐字可互换。

1. 单字段等值：

   ```python
   db.sql("SELECT * FROM segments WHERE region = 'east' LIMIT 2")
   db.query("segments", {"field": "region", "op": "eq", "value": "east"}, limit=2)
   ```

2. 范围加布尔组合（注意优先级：`AND` 高于 `OR`，与 SQL 一致）：

   ```python
   db.sql("SELECT sequence FROM segments WHERE sequence >= 2 AND sequence < 5 OR code = 'c0'")
   db.query("segments", {"or": [
       {"and": [{"field": "sequence", "op": "ge", "value": 2},
                {"field": "sequence", "op": "lt", "value": 5}]},
       {"field": "code", "op": "eq", "value": "c0"}]})
   ```

3. 显式空值与缺失字段（**不等价**于把两者混为一谈的写法）：

   ```python
   db.sql("SELECT * FROM notes WHERE region IS NULL")      # 只有显式 null
   db.query("notes", {"field": "region", "op": "eq", "value": None})
   db.sql("SELECT * FROM notes WHERE region IS MISSING")   # 只有缺失
   db.query("notes", {"field": "region", "op": "exists", "value": False})
   ```

   若改用 `region != 'east'` 或 `NOT (region = 'east')`，缺失字段同样会命中（`ne`/`not` 对缺失字段为真），
   这与 `region IS NOT NULL` 不同；需要“存在且不等于”时应写
   `region IS NOT MISSING AND NOT (region = 'east')`。

4. 字符串/数组成员包含：

   ```python
   db.sql("SELECT * FROM segments WHERE tags CONTAINS 'hot'")
   db.query("segments", {"field": "tags", "op": "contains", "value": "hot"})
   ```

5. 分组计算：

   ```python
   db.sql("SELECT region, count(*) AS total, avg(sequence) AS average FROM segments GROUP BY region")
   db.aggregates.group("segments", "region", "sequence")
   ```

   SQL 的 `total`/`average` 分别对应 Python 的 `count`/`average`；两者都跳过缺失的分组字段、保留显式 null
   分组、并按同样的 `sum/numeric_count` 口径计算平均。

**不等价**之处（不要假设二者可互换）：

- SQL 分组查询支持多分组字段与任意组合聚合；`Database.aggregates.group` 只有一个分组字段和一个度量。
  多字段分组使用 `db.sql("... GROUP BY a, b")`，目前没有等价的单次 Python 调用，可用谓词 + 手工归并。
- SQL 只支持单集合；Python 入口同样。跨集合比较需要调用方分别查询后合并。
- SQL 不支持 `ORDER BY`、`OFFSET`、`LIKE`、子查询和集合操作；需要这些能力时应使用 Python 调用方逻辑，
  而不是期待 SQL 入口兼容 SQLite 全语法。
- 空输入的全局聚合在 SQL 中返回一行（`count` 为 0，`sum` 为 0，`min`/`max`/`avg` 为 `null`），
  而 `Database.aggregates.group` 的分组模式返回空 `groups` 列表；这只是“按分组键输出”与“整表一行”的形状差异。

## 索引选择

当读版本等于当前 head 且谓词含顶层 `AND` 链上的等值项时，两个入口都会尝试同名二级索引，并把
`plan.access` 标为 `"equality_index"`；否则为 `"version_scan"`。索引只是候选收缩，最终仍由完整谓词复核。
`force_scan=True` 在两个入口中具有相同含义。

## 安全边界

- 解析器不接受 SQLite 扩展、PRAGMA、ATTACH、临时表、函数调用或任何写入语句。
- 语句长度上限 100000 字符；超出视为 `UnsupportedSql`。
- 集合名与字段路径按 `codec` 规则校验；字段路径必须由非空点分隔段组成。
- 结果中的文档来自正常读取路径，不会暴露 `versions`、`commits`、`meta` 等内部表。
