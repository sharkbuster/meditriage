# 门诊预约与智能分诊系统

[![CI](https://github.com/sharkbuster/meditriage/actions/workflows/ci.yml/badge.svg)](https://github.com/sharkbuster/meditriage/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/python-3.11%20%7C%203.12%20%7C%203.14-blue)](https://www.python.org/)
[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)

一个医院挂号后端：患者描述症状 → 系统推荐科室 → 预约号源 → 取消归还号源。

做这个项目主要是想把两件事讲清楚：

1. **号源扣减在并发下怎么保证不超卖**（不是"用了 Redis 所以很快"，而是说清竞态窗口在哪、怎么堵住）
2. **症状分诊怎么做出可评测的效果**（不是"接了个大模型"，而是给出准确率和置信度阈值）

> 当前状态：核心功能、并发测试、分诊评测均已完成。分诊默认走零依赖模式，
> 在 104 条评测集上 Top-1 94.8% / Top-3 100%。待办见文末。

---

## 快速开始

不需要装数据库、不需要 API key，clone 下来就能跑：

```bash
python -m venv .venv
.venv\Scripts\activate            # Windows
# source .venv/bin/activate       # macOS / Linux

pip install -r requirements.txt

python -m scripts.seed            # 生成演示数据：14 个科室 / 28 名医生 / 280 条排班
python -m uvicorn app.main:app --reload
```

打开 <http://127.0.0.1:8000/docs> 就能试所有接口。分诊可以直接在文档里试：

```
POST /api/triage
{"symptom": "咳嗽三天了还发烧，喘不上气"}
```

自检（文件完整性 + 建表 + 分诊 + 测试一把跑完）：

```bash
python -m scripts.selfcheck
```

跑评测：

```bash
python -m scripts.evaluate        # 输出准确率 + 阈值扫描表，结果写入 data/eval_result.json
```

---

## 号源并发：问题与做法

### 问题在哪

最直觉的写法是"先查库存再扣减"：

```python
schedule = db.get(Schedule, schedule_id)
if schedule.remaining > 0:              # 读
    schedule.remaining -= 1             # 写 —— 中间是可被其它请求插入的窗口
```

读和写之间没有任何保护。两个请求可以同时读到 `remaining=1`，然后各自扣一次，
号源总数只有 1 个，却产生了 2 条预约记录。

这个问题**不需要真的并发就能证明**，用两个独立事务按顺序"读-读-写-写"即可复现。
`tests/test_concurrency.py::test_check_then_act_oversells` 就是这么做的。

### 做法

把判断和扣减合并成一条 SQL，靠数据库行锁保证原子性：

```sql
UPDATE schedule SET remaining = remaining - 1
WHERE id = ? AND remaining > 0
```

用影响行数判断有没有抢到：0 行就是号已放完。

防重复预约不用应用层判断（同样有竞态窗口），而是靠 `(patient_id, schedule_id)` 唯一索引兜底。
两个防线各管一件事：原子扣减防超卖，唯一索引防同一人重复占号。

### 实测结果

`pytest tests/test_concurrency.py -v -s` 的实际输出：

| 场景 | 结果 |
|---|---|
| 先查再改（错误写法） | 号源 1，**落库 2 条预约** → 超卖复现 |
| 原子扣减，40 号源 40 人抢 | 成功 40，落库 40，剩余 0 → 不多不少 |
| 原子扣减，5 个号 30 人抢 | 成功 5，被拒 25，剩余 0 |
| 同一患者并发 8 次 | 成功 1，落库 1，剩余 9 → 幂等成立 |
| 取消后重复取消 | 号源不重复归还 |

**关于并发规模的一点说明**：测试里只用 8 个线程，不是偷懒。
SQLite 同一时刻只允许一个写事务，线程数开大以后测试会从"验证不超卖"
退化成"验证等锁"，既慢又不说明问题。所以自动化测试负责验证**正确性**；
要出"上千并发不超卖"这类**性能数据**，得换 PostgreSQL 单独压测（见「待办」）。

---

## 症状分诊：把检索问题当检索做

分诊本质上是**检索**：主诉是 query，科室是 document，排序取 top-k。
所以这里复用的是 RAG 的思路，只是生成环节可选。

### 三个模式

| 模式 | 依赖 | 说明 |
|---|---|---|
| `rule`（默认） | 无 | 字符 n-gram + IDF 加权重叠排序，规则拼装建议 |
| `local` | Ollama | 排序不变，只让本地模型把建议说得更自然，数据不出本机 |
| `api` | 自己的 key | 同上，走 OpenAI 兼容接口 |

**大模型只参与生成建议，不参与排序。** 这样即使模型不可用，分诊结果依然稳定，
也避免模型凭空编造科室——分诊错了是医疗风险，不能交给概率模型。

### 置信度怎么算

置信度 = **首选科室对主诉的覆盖率**：主诉里有实义的内容单元，有多大比例出现在该科室语料中。

一开始想用"首选项得分占前三名总分的比例"，但实测发现不行：打分做了按 query 长度归一化，
主诉越长分数越小，不同长度之间不可比，没法定阈值。

算覆盖率时踩了两个坑：

1. 直接用字符 bigram 会把停用词和术语粘成"了还""还喘"这类碎片，它们在任何科室语料里都不存在，
   只会稀释分母。结果是"心慌胸闷"这种很明确的主诉覆盖率只有 0.28，看起来像"说不太清"。
   加上停用字过滤后升到 0.5。
2. 有些主诉去掉停用字后只剩单字片段（"肝功能异常" → 肝/功/能/异/常），一个 bigram 都凑不出来，
   覆盖率直接变成 0 —— 等于把完全正常的主诉判成"说不清楚"。所以长度不足 2 的片段按单字保留。

覆盖率低于阈值时 `need_human=true`，前端应提示到导诊台，而不是硬给一个推荐。

### 评测结果

评测集在 `data/triage_eval.csv`，共 104 条（96 条有明确期望科室 + 8 条故意模糊的主诉）。
跑法：`python -m scripts.evaluate`

**96 条明确主诉：**

| 指标 | 结果 |
|---|---|
| Top-1 准确率 | **91/96 = 94.8%** |
| Top-3 准确率 | **96/96 = 100%** |
| 自动接诊（覆盖率 ≥ 0.25） | 72 条，其中判对 **100%** |
| 模糊主诉被正确拦下 | 7/8 = 87.5% |

**阈值是扫出来的，不是猜的**（`evaluate.py` 会打印这张表）：

| 阈值 | 自动接诊 | 接诊准确率 | 漏给人工 | 拦下模糊 |
|---|---|---|---|---|
| 0.00 | 96 | 94.8% | 0 | 0/8 |
| 0.10 | 89 | 97.8% | 7 | 7/8 |
| 0.15 | 87 | 97.7% | 9 | 7/8 |
| 0.20 | 78 | 97.4% | 18 | 7/8 |
| **0.25** | **72** | **100%** | **24** | **7/8** |
| 0.30 | 50 | 100% | 46 | 7/8 |
| 0.35 | 48 | 100% | 48 | 8/8 |
| 0.50 | 35 | 100% | 61 | 8/8 |

选 0.25 的理由：它已经让自动接诊的准确率达到 100%。再往上调到 0.35 只能多拦下 1 条模糊主诉，
代价是多把 24 条明确主诉推给人工 —— 对导诊台来说不划算。

这张表也说明**阈值不能只看准确率**：把阈值一路调高，"自动接诊准确率"会一直好看，
但那只是因为它越来越不敢接，实际是漏诊率上升。所以扫描表里同时给出「自动接诊量」和「漏给人工」两列。

### 改进过程

第一版 Top-1 只有 81.2%，靠评测报告里按科室拆分的错误明细定位问题，改了两轮：

| 轮次 | 改动 | Top-1 |
|---|---|---|
| 第一版 | —— | 81.2% |
| 第二轮 | 补口语语料（"天旋地转""后脑勺疼""咽口水""肝功能异常"…）；儿童改为**候选收敛到接诊儿童的科室**（原来只给儿科乘系数，评测证明扳不动权重差） | 92.7% |
| 第三轮 | 覆盖率增加单字兜底；继续补内分泌/精神心理科的语料 | **94.8%** |

剩下的错例集中在**内分泌科**（4/6）：口语描述太分散（"脖子粗""脚趾红肿热痛"），
这类词得靠持续补语料，或者换成语义向量检索才能再上一个台阶。

---

## 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/health` | 健康检查 |
| GET | `/api/departments` | 科室列表 |
| GET | `/api/doctors?department_id=` | 医生列表 |
| GET | `/api/schedules?doctor_id=&work_date=&only_available=` | 排班与剩余号源 |
| POST | `/api/schedules` | 放号（管理员） |
| POST | `/api/triage` | 症状分诊 |
| POST | `/api/appointments` | 预约（收姓名 + 手机号） |
| POST | `/api/appointments/{id}/cancel` | 取消（幂等） |
| GET | `/api/appointments?phone=` | 我的预约 |
| GET | `/api/medicines?keyword=&only_in_stock=` | 药品目录 |
| GET | `/api/medicines/low-stock` | 需补货的药品（给药房） |
| GET | `/api/pickup-options` | 可选的取药时段 |
| POST | `/api/prescriptions` | 开处方（医生） |
| POST | `/api/prescriptions/{id}/pay` | 处方缴费（此时才扣库存） |
| POST | `/api/prescriptions/{id}/pickup` | 选取药时间 |
| POST | `/api/prescriptions/{id}/picked` | 药房发药 |
| POST | `/api/prescriptions/{id}/cancel` | 作废（已缴费的退还库存） |
| GET | `/api/prescriptions?phone=` | 我的处方 |

患者首页 `/` 是纯中文界面（找科室 / 预约挂号 / 取药 / 我的），`/docs` 是自动生成的接口文档。

---

## 药房与取药

患者端第四个标签。状态机只有一条主线，不搞分支：

```
待缴费 ──缴费──> 待取药 ──药房发药──> 已取药
   └── 作废（退还库存）
```

界面上的按钮跟着状态走：待缴费显示「去缴费 45.3 元」，待取药显示「选取药时间」，
已取药就只剩一行字 —— 患者不需要理解状态码，只需要知道下一步做什么。

### 两个关键决策

**1. 库存在「缴费」时扣，不在「开方」时扣。**

处方开了不缴费很常见（患者改主意、去外面药店买）。如果开方就锁库存，
一堆废处方会把药房库存占死，真正要买药的人反而买不到。

**2. 扣库存复用号源那套原子 UPDATE。**

两个窗口同时发同一盒药是真实场景。先查库存再扣同样会超发，
而「发成负数」比「号源超卖」更难收场 —— 药已经到患者手里了。

### 实测结果

`pytest tests/test_pharmacy.py -v -s` 的实际输出：

| 场景 | 结果 |
|---|---|
| 并发缴费：库存 10，30 张处方抢 | 成功 10，失败 20，剩余 0 → **不超发** |
| 库存不足时缴费 | 整单回滚，另一种药**没被扣一半** |
| 重复缴费 | 被拒，库存不重复扣 |
| 已缴费处方作废 | 库存 10 → 7 → 10 |
| 未缴费就选取药时间 | 被拒（状态机挡在入口） |
| 已取药的处方作废 | 被拒 |

### 金额为什么用整数「分」

药品计价是最容易出精度事故的地方：`0.1 + 0.2 != 0.3`。
金额一旦用 float，对账时一定出现几分钱的差异，而且很难查。
所以 `Medicine.price_cents`、`Prescription.total_cents` 全是整数分，展示时再除以 100。

处方明细还存了**单价快照**（`unit_price_cents`）：药品调价后，历史处方的金额必须还能对得上。
测试里专门验证了这条 —— 把药价改成 99.99 元后，老处方金额不变。

### 处方号为什么用自增 id 生成

号形如 `RX20261012000001`（RX + 开方日期 + 6 位序号），对外用它而不是直接露主键。

一开始是"数当天记录条数 + 1"，两个问题：

1. 删掉一张处方后数量少 1，新号会和当天已有的号撞上；
2. 并发开方时各个事务看不到彼此未提交的 INSERT，算出的号完全一样，**重试也还是同一个号**。
   实测 24 个并发请求只有 10 个能成功。

现在让数据库发号（`db.flush()` 拿到自增 id 再拼号），没有"先查再写"，也就没有冲突和重试。
并发测试里 24 个请求 24 个成功、号全唯一。

序号补到 6 位而不是 4 位：补 4 位时 id 过万之后 `"10000"` 会排在 `"9999"` 前面
（字符串比较），药房按号叫号就乱了。`test_recent_changes.py` 里有一条专门盯这个边界。

脚本（`scripts/`）里常用的几个：

| 脚本 | 用途 |
|---|---|
| `python -m scripts.seed` | 灌演示数据（科室/医生/排班/药品/演示处方） |
| `python -m scripts.seed --reset-pharmacy` | 把处方和药品库存恢复成初始状态（演示前用） |
| `python -m scripts.selfcheck` | 一键自检：文件、建表、分诊、药房、测试 |
| `python -m scripts.evaluate` | 分诊评测 + 阈值扫描 |
| `python -m scripts.smoke_live` | 对运行中的服务做端到端验证（42 项） |
| `python -m scripts.loadtest` | PostgreSQL 并发压测 |
| `python -m scripts.show_data` | 看一眼库里有什么 |
| `python -m scripts.cleanup_test_data` | 清理测试残留 |

---

## 目录结构

```
app/
  core/config.py        配置（全部走环境变量）
  db/session.py         引擎与会话
  domain/models.py      数据模型（并发控制与金额精度的说明写在文件头部注释里）
  domain/schemas.py     出入参
  knowledge/departments.py  科室语料 + 口语同义词表
  services/appointment.py   号源原子操作（含反面教材 take_slot_naive）
  services/booking.py       预约业务（幂等、取消归还）
  services/triage.py        分诊检索与置信度
  services/pharmacy.py      处方、库存扣减、取药状态机
  services/llm.py           可插拔生成层
  api/routes.py             HTTP 接口
  static/                   患者端页面（纯 HTML/CSS/JS，无构建步骤）
scripts/
  seed.py               演示数据（科室/医生/排班/药品/演示处方）
  evaluate.py           分诊评测 + 阈值扫描
  selfcheck.py          一键自检
  loadtest.py           PostgreSQL 并发压测
  smoke_live.py         对运行中的服务做端到端验证
  show_data.py          看一眼库里有什么
  cleanup_test_data.py  清理测试残留
data/
  triage_eval.csv       分诊评测集（104 条标注）
  eval_result.json      评测结果（脚本生成）
tests/
  test_concurrency.py   号源并发安全（核心证据）
  test_pharmacy.py      药房状态机、库存并发、金额精度
  test_api.py           接口冒烟
  test_frontend.py      前端静态一致性（防改名/删元素导致静默失效）
  test_edge_cases.py    对抗性测试：边界、异常路径、外键、输入清洗
```

---

## 测试与踩过的坑

`pytest tests/` 共 123 条。`test_edge_cases.py` 是专门"想办法把代码弄坏"的对抗性测试，
`test_recent_changes.py` 盯最近改动过的地方，`test_llm_and_boundaries.py` 覆盖生成层的
异常路径和时间/数值边界。下面这些是真在开发过程中被测试抓出来的，都补了回归测试：

| 问题 | 根因 | 现在怎么防 |
|---|---|---|
| 确认弹层从页面加载起就一直显示、关不掉 | `.sheet{display:flex}` 是类选择器，优先级高于 `[hidden]` 的浏览器默认样式 | CSS 里加 `[hidden]{display:none !important}`；`test_frontend.py` 检查它还在 |
| 删了一个下拉框，但 JS 里还在 `appendChild`，导致基础数据加载中断 | 改 HTML 忘了改 JS，错误被 try/catch 吞掉，只表现为"没数据" | `test_frontend.py` 交叉检查 JS 引用的 id 是否都在 HTML 里 |
| 取消过一次预约后**再也约不上** | `(patient_id, schedule_id)` 唯一索引把已取消的记录也算进去 | 重约时复用那条已取消的记录，而不是插新行 |
| 给不存在的患者开处方**返回 201 成功** | SQLite 默认 `PRAGMA foreign_keys=0`，外键根本不生效；换 PostgreSQL 就会报错 | 连接时显式 `PRAGMA foreign_keys=ON`（`enable_sqlite_pragmas`），并加测试钉住它 |
| 删处方时 `FOREIGN KEY constraint failed` | 删主表前没先删明细。同一份删除逻辑在三个脚本里各写了一遍，三份都错 | 删除收敛到 `pharmacy.delete_prescriptions()` 一个入口 |
| 并发开方撞号，24 个请求只成功 10 个 | 开方号"先查再写"算出来，并发事务看不到彼此未提交的 INSERT，算出的号永远一样，重试也没用 | 改用自增 id 生成号（`_make_code`），数据库发号就没有冲突 |
| 开方号过万后**排序错乱** | 序号补 4 位，id 到 10000 时 `"10000" < "9999"`（字符串比较），药房按号叫号就乱 | 补到 6 位；测试同时断言"不截断"和"排序与数值顺序一致" |
| 模型返回空内容时，**患者看到空的就医建议** | 原来只在"抛异常"时回退规则话术；返回空串不算异常，advice 直接是空的 | 生成后统一判空再回退，不再依赖"必须报错"这个假设 |
| 能约到**过去的号** | 页面上只列未来几天的号，但直接调接口能传任意 `schedule_id` | 校验放在 `booking.book` 里（所有预约的必经之路），而不是接口层 |
| 放号时**下班早于上班**、能给昨天放号 | `period` 校验了上午/下午，日期和时间先后没管 | `ScheduleCreate` 加字段校验器 |
| 症状填三个空格能通过校验 | Pydantic 的 `min_length` 只管字符个数，不做 strip | `field_validator` 显式 strip 后再判断 |
| 分诊准确率只有 81% | 口语词没进语料；儿童患者只给儿科乘系数，扳不动权重差 | 改成候选收敛到接诊儿童的科室；按评测报告的错误明细补语料 |

> 教训都指向同一件事：**同一个规则写在多个地方，就会错多次**。
> 删除逻辑曾经在三个脚本里各写一份，同一个外键顺序问题犯了三次；
> 开方号曾经"先查再算"，并发下必然撞。现在这两处都只有一个入口。
>
> 另一条：**"没报错"不等于"结果正确"**。模型返回空串、校验只管长度不管内容、
> 接口不校验日期，这几种都是"跑得通但结果是错的"，只有主动构造异常输入才能发现。

---

## 待办

- [ ] **PostgreSQL 压测出性能数字**：脚本已就绪（`scripts/loadtest.py`），
      但需要先起一个 PostgreSQL（`docker compose up -d db`）才能跑出真实 QPS / P95
- [ ] **医生端界面**：现在开方只能调接口，没有给医生用的页面
- [ ] **药房端界面**：发药、补货提醒现在只有接口（`/api/medicines/low-stock`）
- [ ] 语料持续补充：错例集中在内分泌科（4/6），口语说法太分散
- [ ] 尝试语义向量检索替换字符 n-gram，对比两者在同一评测集上的差距
- [ ] 号源分时段（现在只到"上午/下午"粒度，没有精确到分钟的时间片）
- [ ] Alembic 迁移（现在启动时 `create_all` 建表，不适合线上变更）

---

## 已知问题

- SQLite 只适合开发和测试，别用在生产：它同一时刻只允许一个写事务
- 分诊语料（`app/knowledge/departments.py`）是手写的，覆盖面有限；
  同义词表也一样，靠实际答错的案例慢慢补
- 没有做鉴权。真实系统里"谁能放号""谁能看别人的预约"都需要权限控制

## 许可

MIT
