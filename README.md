# ML Direct Sync · 美客多直连同步

直连 Mercado Libre Open API 的轻量服务，把 4+ 店铺的销售数据按 `SKU × 平台 × 月份` 自动汇总到飞书多维表格。

## 项目背景

详见 `~/.claude/projects/C--Users-Administrator/memory/project_ml_direct_sync.md`。

- 启动：2026-05-09
- 业务体量：ML 月单 3000-10000，4+ 店（本土 + 跨境混合）
- 替代品：领星 ERP 不暴露 ML 数据；蓝鲸 BI 不支持 API

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

## 部署

Zeabur 项目 `frankiepan501` 下 service `ml-sync`，详见 [zeabur-deploy-workflow](../../.claude/projects/C--Users-Administrator/memory/zeabur-deploy-workflow.md)。

## License

Internal use only · Powkong & Funlab.
