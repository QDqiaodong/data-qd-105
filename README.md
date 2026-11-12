# data-qd-105 — Land-cover Cube / 土地覆盖空间管线

将虚构本地平面地块与统计区叠加，生成可查询的空间碎片和覆盖面积指标。

已实现：明确 LOCAL_METERS 坐标、有效 Polygon/MultiPolygon 校验、同层重叠拒绝、区/地块交集、显式未分类几何、面积守恒、按区分类面积、来源哈希、GeoJSON 和 SQLite。

## 独立安装

需要 Python >=3.11（先确认 python3 --version）。运行依赖和传递依赖已按 requirements.lock 固定；仅使用安装的包，无服务集群。 构建工具也已固定版本。
```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-build.lock
.venv/bin/python -m pip install -r requirements.lock
.venv/bin/python -m pip install --no-build-isolation --no-deps .
```

首次安装可能需要联网；安装后核心演示与测试可离线运行。每仓独立 `.venv`，不使用其他项目源代码或环境。macOS/Linux 使用相同命令；具体已验证环境不由此声明。

## 演示、输出与测试

在仓库根目录执行。首次 `run/` 不存在即为干净数据状态；需要干净重演可指定新的输出路径。

```sh
.venv/bin/python -m landcover_cube --output run/coverage
.venv/bin/python -m landcover_cube --output run/replay
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -c 'import sqlite3; db=sqlite3.connect("run/coverage/coverage.sqlite"); print(db.execute("SELECT * FROM zone_summary ORDER BY zone_id").fetchall())'
```

2 个各 100m² 的区域，5 个碎片总计 200m²。Z1 woodland=50、grass=50，覆盖 100%；Z2 woodland=10、water=80、未分类=10，覆盖 90%。共享边不产生面积。

相同输入重放得到一致 JSON；允许同路径全量重算。输入契约失败在创建新报告前报错，已有报告不会因此被伪装为新结果。运行 SQLite 与报告均在忽略的 `run/` 内。

## 结构与业务契约

- `landcover_cube/source.py：坐标、属性及同层拓扑约束`
- `landcover_cube/overlay.py：相交/差集、碎片与守恒`
- `landcover_cube/pipeline.py：空间证据和可查询面积数据`
- `docs/contract.md`：完整字段/格式、业务键、边界、输出含义与当前约束。
- `data/`：小型确定性合成输入和 `PROVENANCE.md`。
- `tests/`：真实业务值、重复运行、重要边界和失败行为测试。

当前范围：仅自制平面米制坐标；不是 RFC7946 经纬度数据，不声称真实 EPSG、地理面积或自动投影。当前无空间索引、拓扑修复、分块并行或时态变化检测。

全部示例为原创合成数据，不代表真实客户、监管标准、实际测量或线上历史。项目没有真实账号、密钥或付费依赖；不包含第三方原始数据。代码及自制数据采用 MIT；依赖保留自身许可。
