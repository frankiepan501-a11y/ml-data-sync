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

## 部署

Zeabur 项目 `frankiepan501` 下 service `ml-sync`，详见 [zeabur-deploy-workflow](../../.claude/projects/C--Users-Administrator/memory/zeabur-deploy-workflow.md)。

## License

Internal use only · Powkong & Funlab.
