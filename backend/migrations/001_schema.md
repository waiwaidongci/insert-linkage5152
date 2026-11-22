# 001 初始表结构

应用首次启动以 SQLAlchemy 模型创建表与唯一约束。现有数据表只补建缺失表，升级到修改过字段的版本需要显式迁移。

- `users` 账号角色和加盐密码摘要。
- `datasets` 不可变记录批次，以名称和 SHA-256 去重。
- `runs` 记录参数、数据批次、运行租约、分析结果、导出文件及复核决定。
- `schedules` 保存定时计划和下一到期时间。
- `deliveries` 保存站内分发回执，每次运行最多一个回执。
- `audit_events` 记录登录、数据入库、运行、复核和报表分发动作。

## 002 批次更正版本链

支持在同一业务批次上逐版更正适配特征，新增以下结构（新环境由 `create_all` 直接建出，存量库需显式执行对应 DDL）：

- `dataset_revisions`：批次的逐版特征快照，主键为 `(dataset_id, revision)`，第 1 版在导入时写入；`(dataset_id, checksum)` 唯一，保证同内容重复提交复用同一版。
- `pair_distances`：冻结到版本的规格对距离，按 `(dataset_id, revision, left_index, right_index)` 唯一，支撑中断恢复与下一版只重算受影响规格对。
- `published_revisions`：每个批次至多一条的已发布版本指针，只接受版本号更大的运行，晚完成的旧版不能顶掉已发布新版。
- `runs` 新增可空列 `revision`（关联 `dataset_revisions.revision`，旧数据为空表示按批次原始行计算）、`superseded`（布尔，标记该版本已被更新版取代）与 `pair_progress`（JSON，记录本版实际重算/沿用的规格对数，中断恢复时只统计补算部分）。

对应 MySQL 8 显式 DDL（在已有库上升级时执行）：

```sql
CREATE TABLE dataset_revisions (
  dataset_id INT NOT NULL,
  revision INT NOT NULL,
  checksum VARCHAR(64) NOT NULL,
  rows JSON NOT NULL,
  row_count INT NOT NULL,
  author VARCHAR(64) NOT NULL,
  created_at DATETIME NOT NULL,
  PRIMARY KEY (dataset_id, revision),
  UNIQUE KEY uq_dataset_revision_checksum (dataset_id, checksum),
  CONSTRAINT fk_dataset_revisions_dataset FOREIGN KEY (dataset_id) REFERENCES datasets (id)
);

CREATE TABLE pair_distances (
  id INT NOT NULL AUTO_INCREMENT,
  dataset_id INT NOT NULL,
  revision INT NOT NULL,
  left_index INT NOT NULL,
  right_index INT NOT NULL,
  distance DOUBLE NOT NULL,
  PRIMARY KEY (id),
  UNIQUE KEY uq_pair_distance (dataset_id, revision, left_index, right_index),
  KEY ix_pair_distances_dataset_revision (dataset_id, revision),
  CONSTRAINT fk_pair_distances_dataset FOREIGN KEY (dataset_id) REFERENCES datasets (id)
);

CREATE TABLE published_revisions (
  dataset_id INT NOT NULL,
  revision INT NOT NULL,
  run_id INT NOT NULL,
  published_at DATETIME NOT NULL,
  PRIMARY KEY (dataset_id),
  CONSTRAINT fk_published_dataset FOREIGN KEY (dataset_id) REFERENCES datasets (id),
  CONSTRAINT fk_published_run FOREIGN KEY (run_id) REFERENCES runs (id)
);

ALTER TABLE runs ADD COLUMN revision INT NULL,
  ADD COLUMN superseded BOOL NOT NULL DEFAULT 0,
  ADD COLUMN pair_progress JSON NULL;
CREATE INDEX ix_runs_revision ON runs (revision);
```
