# 002 运行幂等键与日报发布版本

在 001 基础上补齐"重算不重复、发布一次性切换、旧版签收保留"所需的结构。
应用启动时由 `src/config/migrations.py` 幂等执行，重复运行无副作用；全新库仍由
`Base.metadata.create_all` 直接建出最终结构。

## 表变更

- `runs`
  - 新增 `idem_key VARCHAR(120)`：由营业日、台账快照校验和与计算参数派生，
    加唯一约束 `uq_runs_idem_key`（PostgreSQL 允许多个 NULL，历史运行不受影响）。
    手动重跑、定时物化与并发提交命中同一键时复用既有运行，不重复生成结果。
  - 新增 `publication_id INTEGER`：指向该运行最终对应的发布版本；未通过一致性
    门控时为空，读取方继续沿用旧版。
- `report_publications`（新表）
  - 按 `report_day` 维护版本序列，`(report_day, version)` 唯一，`run_id` 唯一。
  - `state` 取值：`active`（当前生效）、`superseded`（已被新版一次性替代，
    原 CSV 与签收记录保留）、`rejected`（桥接/渠道处置/CSV 一致性校验未通过）。
  - 保存台账快照校验和 `dataset_checksum` 与结果指纹 `fingerprint`，发布前
    重新计算指纹并确认 CSV 底稿存在且同源，全部一致后才在同一事务内切换。

## 行为约束

- 重算期间没有新版本 `active` 之前，页面与下载均返回旧版。
- 较早完成的旧运行若发现同营业日已有更大 `run_id` 的生效版本，拒绝发布。
- 定时计划每日物化时取最新数据批次，使晚到退款按原营业日进入新快照；
  同 `(schedule_id, scheduled_at)` 与同 `idem_key` 均跳过，断点恢复不重复落运行。
- `deliveries` 仍为每运行至多一条回执，重跑不重复生成；历史运行的复核结论、
  签收回执与导出文件随 `superseded` 版本一并保留。
