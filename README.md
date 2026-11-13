# data-qd-105 — Land-cover Cube / 土地覆盖空间管线

将虚构本地平面地块与统计区叠加，生成可查询的空间碎片和覆盖面积指标。

已实现：明确 LOCAL_METERS 坐标、有效 Polygon/MultiPolygon 校验、同层重叠拒绝、区/地块交集、显式未分类几何、面积守恒、按区分类面积、来源哈希、GeoJSON 和 SQLite。另提供可离线的分块入口（`landcover_cube.tiled`），用固定网格缩小几何计算范围，结果业务粒度与全量入口一致。另提供独立连通性脆弱分析入口（`landcover_cube.connectivity`），按统计区为同目标类地块建立共边图并逐宗删除评估剩余最大连通面积。

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
.venv/bin/python -m landcover_cube.tiled --tile-size 3 --output run/tiled
.venv/bin/python -m landcover_cube.tiled --zones data/tiled_zones.geojson --parcels data/tiled_parcels.geojson --tile-size 4 --output run/tiled-demo
.venv/bin/python -m landcover_cube.connectivity --zones data/connectivity_zones.geojson --parcels data/connectivity_parcels.geojson --class forest --threshold 1 --output run/connectivity/chain.json
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -c 'import sqlite3; db=sqlite3.connect("run/coverage/coverage.sqlite"); print(db.execute("SELECT * FROM zone_summary ORDER BY zone_id").fetchall())'
```

2 个各 100m² 的区域，5 个碎片总计 200m²。Z1 woodland=50、grass=50，覆盖 100%；Z2 woodland=10、water=80、未分类=10，覆盖 90%。共享边不产生面积。

分块模式读取同一契约的输入，按全局原点锚定的方形块（`[i*s,(i+1)*s] × [j*s,(j+1)*s]`，`i`/`j` 可为负）只处理与统计区有正面积交集的块。同层拓扑校验先用包围盒候选筛选，每个无序候选对至多精确检查一次；块内先用候选地块的真实几何并集判定未覆盖，再把每个 `(zone_id, coverage_kind, parcel_id)` 的所有正面积片段归并成单个 Polygon/MultiPolygon。最终 facts 仍为区/地块粒度，统计区与地块的关系不会因分块变成多条。构建在目标同目录的私有暂存目录中完成：局部事实先写入本次私有 SQLite，按业务键归并、校验通过且数据库关闭后才原子发布到新目标目录；构建失败清理暂存且不留下正式半成品。除 `fragments.geojson`、`coverage.json`、`coverage.sqlite` 外还输出 `build_diagnostics.json`（实际执行路径的逐块候选/裁剪/暂存计数，不含耗时或机器路径）。`tests/test_tiled.py` 覆盖负坐标、孔洞、多分量、跨区地块、网格线接触、小于 1e-8 的合法细片、200 区候选缩减、SQLite 暂存归并、失败注入与 CLI 逐键对照。

`run/tiled-demo` 的合成示例统计区为 `[-5,15]×[-5,5]`（面积 200），地块 `TP` woodland 为 `[-5,5]×[-5,5]`、`TQ` grass 为 `[5,10]×[-5,5]`，`tile-size=4`；结果为 TP=100、TQ=50、未分类=50。`run/` 被忽略，属于一次性运行产物，不提交。

`tile-size` 必须是 `(0,1000000]` 内的有限十进制数（拒绝布尔值和 NaN/Infinity 文本）。作为实现保护，单个统计区若会需要超过 5,000,000 个块，会在分配前被拒绝并提示增大 `tile-size`；这不改变数值契约本身。

相同输入重放得到一致 JSON；允许同路径全量重算。输入契约失败在创建新报告前报错，已有报告不会因此被伪装为新结果。运行 SQLite 与报告均在忽略的 `run/` 内。

## 连通性脆弱分析

`landcover_cube.connectivity` 读取与覆盖入口相同的两个 FeatureCollection，并先整体校验全部输入（含未被选中类的地块）。用语：
```sh
.venv/bin/python -m landcover_cube.connectivity --zones data/connectivity_zones.geojson --parcels data/connectivity_parcels.geojson --class forest --threshold 1 --output run/connectivity/chain.json
```
`--class`（别名 `--target-class`）为目标类字符串；`--threshold`（别名 `--shared-edge-threshold`）为严格大于 0 的有限十进制米数，拒绝布尔、NaN/Infinity 和畸形文本；`--output` 是单个 JSON 报告文件。

每个统计区各自成图：目标类地块先裁剪到该区，每个正面积 Polygon 分量独立成节点并保留孔洞；同一地块的多个分量不会暗中相连。仅当两节点边界交线的 LineString 部分并集总长 ≥ 阈值时才连边；点接触长度为 0，不连边。逐宗同时删除该区内该地块的全部节点，重新计算剩余连通分量，输出成员/面积、剩余最大组件面积、绝对损失与相对基线损失，按损失降序、parcel_id 升序排名；空图最大面积为 0。报告含规范化节点几何、逐边共边依据及两输入 SHA256，可据此独立重算。合成示例 `data/connectivity_zones.geojson` / `data/connectivity_parcels.geojson` 为 A-B-C 三个 1m×1m 正方形：阈值 1 时删除 B 后最大面积 1、绝对损失 2，B 排名第一。`tests/test_connectivity.py` 覆盖该链、环、多分量整宗删除、孔洞共边、点接触、阈值边界、全量校验、失败保留旧报告、确定性与 CLI。

共边只是几何结构指标，不保证物种通行或生态功能连通；报告内 `caveat` 明确说明。

## 结构与业务契约

- `landcover_cube/source.py：坐标、属性及同层拓扑约束（包围盒候选筛选 + 精确相交检查）`
- `landcover_cube/overlay.py：相交/差集、碎片与守恒`
- `landcover_cube/pipeline.py：空间证据和可查询面积数据`
- `landcover_cube/tiled.py：分块构建、私有 SQLite 暂存、按业务键归并、校验与原子发布`
- `landcover_cube/connectivity.py：按区共边图、逐宗删除脆弱性与确定性 JSON 报告`
- `docs/contract.md`：完整字段/格式、业务键、边界、输出含义与当前约束。
- `data/`：小型确定性合成输入和 `PROVENANCE.md`。
- `tests/`：真实业务值、重复运行、重要边界和失败行为测试。

当前范围：仅自制平面米制坐标；不是 RFC7946 经纬度数据，不声称真实 EPSG、地理面积或自动投影。全量与分块入口均为单进程；无拓扑修复、栅格化、数据库服务、并发执行或中断续传。分块模式沿用 `LOCAL_METERS` 坐标与 GEOS 浮点面积模型，不引入新的空间依赖。

全部示例为原创合成数据，不代表真实客户、监管标准、实际测量或线上历史。项目没有真实账号、密钥或付费依赖；不包含第三方原始数据。代码及自制数据采用 MIT；依赖保留自身许可。
