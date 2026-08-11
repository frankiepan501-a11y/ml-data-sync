# Funlab ML Direct Sync · 美客多直连同步（FUNLAB 单品牌）

直连 Mercado Libre Open API 的轻量服务，把 FUNLAB 在 ML 多店铺的销售数据按 `SKU × 平台 × 月份` 自动汇总到飞书多维表格。

> **Scope 注**：v1 只接入 FUNLAB 品牌的 ML 店铺。Powkong 跨境店以后会另注册独立 ML App + 单独跑一份 DPP 申请，本 repo 不混合两个品牌的凭证。

## 项目背景

详见 `~/.claude/projects/C--Users-Administrator/memory/project_ml_direct_sync.md`。

- 启动：2026-05-09
- 业务体量：FUNLAB 在 ML 月单数千-上万，多店（本土 + 跨境混合）
- 替代品：领星 ERP 不暴露 ML 数据；蓝鲸 BI 不支持 API

## ML 开发者 App

- App 名：`Funlab Internal Data Sync`
- 注册位置：https://developers.mercadolibre.com.ar/devcenter（用墨西哥主店账号）
- 跨境店扩展能力靠 ML Developer Partner Program (DPP) — 走 [Inhouse Development 通道](https://docs.google.com/forms/d/e/1FAIpQLSftLvUCWc3GMwag-dbArKgeliAKNqTWB2LgTOS1mrAKQF8AvA/viewform)，最长 6 个月审核

## 架构（仿 KOL 自动化）

```
ML 4+ 店铺 OAuth tokens
        │
        ▼
  FastAPI on Zeabur
  ├── /oauth/callback         OAuth code → token 交换
  ├── /oauth/refresh           access_token 主动续期 cron
  ├── /sync/orders             拉某店订单（按月增量）
  ├── /sync/items              拉某店 Listing
  ├── /aggregate/sku-monthly   SKU × 平台 × 月份聚合
  └── /report/feishu           写飞书多维表格
        │
        ▼
  飞书多维表格「跨平台 SKU 月销表」
        ▲
        │
  n8n 月度 cron（每月 1 号 09:00 BJ）
```

## 技术栈

- FastAPI + httpx (async)
- SQLite（OAuth token 存储 + 增量同步缓存）
- Zeabur（部署）
- 飞书 Bitable API（报表写入）
- n8n（cron 调度）

## 里程碑

- [x] M0 项目骨架 (2026-05-09)
- [ ] M1 OAuth + 单店 PoC (2026-05-19)
- [ ] M2 批量 + SKU 聚合 (2026-05-23)
- [ ] M3 飞书写入 + cron (2026-05-26)

## ML API 关键约束

- `access_token` 6 小时过期 → 必须主动续期 cron
- `refresh_token` 6 个月，**单次使用**（每次刷新换新的，旧的失效）
- 必须申请 `offline_access` scope 才会下发 refresh_token
- 5 站点（MLA/MLB/MLM/MLC/MCO）OAuth 分站独立
- 没有沙箱，用 test users 在生产模拟（≤10 个/app）
- 限流：单 seller 1500 req/min

## 跨境店额外门槛

跨境店（Global Selling）需要 ML 商务团队邀请 + Security Assessment（10 天 SLA）。本土店（MLM/MLB 等）自助 OAuth 即可。

## 开发

```bash
# 本地启
pip install -r requirements.txt
cp .env.example .env  # 填 ML_APP_ID / ML_APP_SECRET 等
uvicorn app.main:app --reload --port 8000
```

## 美客多毛利月结闭环

2026-07 起，月结不再只靠普通通知。服务端新增一组闭环端点：

- `POST /report/ml-close/audit`：审计指定月份报表行，输出店铺覆盖、缺口 SKU、采购成本缺口、头程/海外仓缺口，并可写入「美客多毛利月结状态台」。
- `POST /report/ml-close/recalc-cost`：重跑美通/墨客多/三沐成本同步后再审计。
- `GET|POST /report/ml-close/card`：生成或发送飞书交互卡，卡片类型包括操作指引、成本缺口、运营终稿确认、财务终稿确认。
- `GET|POST /report/ml-close/status`：给全渠道汇总器做 gate，只有 `运营已确认` 或 `财务已确认终稿` 才允许放行财务终稿。
- `POST /report/ml-close/confirm`：处理飞书按钮回调，并把原卡 PATCH 成结果态。

CBT-FULL 仍以官方导出 3 文件为准；本土店和巴西店走 ML API/cache，不要求运营上传。`/report/cbt-ingest?commit=true` 成功后会自动触发 `sync-meitong-cost(commit=true)` 和月结审计，避免 CBT 入表后成本状态停在旧版本。

### 2026-07-07 广告费修复记录

- 问题：2026-06 报表里本土 3 店 `DISTRIBUIDOR VALMIGOZ` 和巴西店广告费显示为 0，运营审核时无法判断是否真实无广告费。
- 根因：本土 3 店未配置 ML Ads advertiser 映射；同时广告 API 拉取异常此前会被静默吞掉，失败结果容易被误写成 0。
- 修复：补充本土 3 店 advertiser `2909534 / MLM / MXN`；报表同步返回 `ad_error`、`ad_items_count`、`ad_total_local`；月结审计卡片展示每个覆盖店铺的行数、订单数、营收和广告费。
- 验证：ML Ads API 已确认 2026-06 本土 3 店和巴西店均有 cost；重新同步对应店铺后，运营确认卡应直接列出 `ML CBT-FULL`、`ML 巴西本土店 AIRSOFT COMERCIAL`、`ML 本土3店 DISTRIBUIDOR VALMIGOZ`。

### 2026-07-09 运营确认与卡片反馈修复记录

- 问题：运营 2026-07-08 已点击「确认运营终稿」，但 2026-07-09 定时卡又显示「待运营确认」；同时原卡没有稳定变成“已处理”结果态，运营难判断点击是否生效。
- 根因：`audit(commit=true)` 每次无缺口重算都会写回 `状态=待运营确认`，覆盖了已确认状态；美客多 event-hub payload 只传 `message_id`，未传 `chat_id/open_chat_id` fallback 上下文，PATCH 原卡失败时没有可见回执。
- 修复：无缺口重算遇到 `运营已确认/财务已确认终稿` 时保留终态并返回 `next_card=none`；`/report/ml-close/card?kind=none` 直接跳过发送；确认回调改为 PATCH 原卡，失败则向源群发送灰色“已处理”结果卡；运营确认卡增加“实时重算快照、较上次重算、确认后不再重复发卡”的说明。
- 验证：`python -m py_compile app/ml_close.py` 通过；n8n event-hub `ML Profit Payload` 已补传 `open_message_id/chat_id/open_chat_id` 并保持 active。

### 2026-08-10 本土3店广告费漏抓与重复行修复

- 问题：2026-07 本土3店有官方广告花费，但月度飞书报表写成 0，导致整月毛利被高估。
- 根因一：广告 API 异常虽然写入 `ad_error`，调用方仍继续替换飞书记录，把未知值降级成 0。
- 根因二：Product Ads 搜索接口会重复返回相同逻辑广告行；直接逐行相加会高于官方 `metrics_summary`。
- 修复：广告失败立即以 502 阻止飞书写入；按 `item_id + campaign_id + ad_group_id` 去重；去重后逐指标对齐官方汇总；未经汇总校验的缓存不能被严格月度同步复用；新增 `commit=false` 只读预览。
- 失败展示：接口统一返回“广告费抓取失败”；`commit=true` 同步还会把受影响店铺写入 SQLite 与独立原子文件两份持久失败标记、月结异常状态，并发送红色失败卡。任一持久标记都能在服务重启后继续阻断确认；持久写入暂时异常时，本进程还会立即启用应急阻断。只读审计、后续成本审计、卡片生成和历史确认卡都不能绕过该异常；同月的失败、解除、红卡发送和确认动作会串行执行，只有对应店铺在该失败之后启动的成功同步完成飞书回读，才能解除其标记。
- 飞书安全替换：先创建新行，再删除旧行；删除失败会删除本次新行并保留旧快照；成功后自动回读本店本月的行数和广告费合计。
- 回归测试：`python -m unittest discover -s tests -v` 共 33 个场景，覆盖重复行、汇总不一致、缺字段、空明细异常、未校验缓存、广告接口失败、失败卡、SQLite/独立文件/应急失败标记、双写失败后的模拟重启、状态台失败时红卡兜底、只读/写入月结拦截、`none` 卡片和重复点击旁路、确认末次复查、失败等待锁时的先后顺序、新失败覆盖旧成功、状态写失败时保留持久闸、多店失败独立解除、只读预览，以及飞书查询/创建/删除失败、回滚和完整翻页。
- 生产补数边界：先对单个 `seller_id + month` 预览并保存旧行快照，再提交替换、回读飞书金额，最后重跑月结审计；不做跨店或跨月批量覆盖。

### 2026-08-11 财务统一格式月报与 ERP 品名/类目映射

- 财务确认的 2026-07 表格已固化为模板：未来月份生成 47 列主表，并保留 `数据源`、`检查` 两个 Sheet。
- `中文名称`、`分类` 按精确优先级读取 ERP 产品资料：产品信息维护表 ERP SKU → 产品采购成本台 ERP SKU → 分销报价单 SKU。采购成本仍只沿用生产毛利表按 ERP SKU 算出的值，不使用品名或类目匹配成本。
- 未命中、名称/分类为空、同层级冲突时，生成器在创建飞书报表前停止，月结不能写成 `财务已确认终稿`。
- 财务点击确认时，系统先生成并回读验证月报，再写终稿状态；成功卡片直接链接新月报。`检查!E1` 保存完成标记，同月重复触发只返回原报表。
- 只读预演：`POST /report/ml-unified-monthly?period=month_YYYY-MM&commit=false`。已财务确认月份可用 `commit=true` 幂等回放；未确认月份禁止直接提交。
- 2026-07 真实数据只读回归：57 行、44 个唯一 SKU、3 店，ERP 映射缺口/冲突/空值均为 0；佣金换算最大舍入差 `0.034328 RMB`，毛利重算最大差 `0`。

## 部署

Zeabur 项目 `frankiepan501` 下 service `ml-sync`，详见 [zeabur-deploy-workflow](../../.claude/projects/C--Users-Administrator/memory/zeabur-deploy-workflow.md)。

## License

Internal use only · Powkong & Funlab.
