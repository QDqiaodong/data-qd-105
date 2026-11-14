# data-qd-105 — Land-cover Cube / 土地覆盖空间管线

将虚构本地平面地块与统计区叠加，生成可查询的空间碎片和覆盖面积指标。

已实现：明确 LOCAL_METERS 坐标、有效 Polygon/MultiPolygon 校验、同层重叠拒绝、区/地块交集、显式未分类几何、面积守恒、按区分类面积、来源哈希、GeoJSON 和 SQLite。另提供可离线的分块入口（`landcover_cube.tiled`），用固定网格缩小几何计算范围，结果业务粒度与全量入口一致。另提供独立连通性脆弱分析入口（`landcover_cube.connectivity`），按统计区为同目标类地块建立共边图并逐宗删除评估剩余最大连通面积。另提供离线采集入口（`landcover_cube.acquisition`），由只含节点与无序界线的测绘交接重建现有覆盖入口可直接读取的 `parcels.geojson` 及逐边审计 JSON。另提供两期覆盖转移入口（`landcover_cube.transition`），接收同一统计区与前后两期地块，按空间叠加生成转移 GeoJSON、面积矩阵 JSON 和 SQLite。

## 独立安装

需要 Python >=3.11（先确认 python3 --version）。运行依赖和传递依赖已按 requirements.lock 固定；仅使用安装的包，无服务集群。 构建工具也已固定版本。
```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-build.lock
.venv/bin/python -m pip install -r requirements.lock
.venv/bin/python -m pip install --no-build-isolation --no-deps .
```

首次安装可能需要联网；安装后核心演示与测试可离线运行。每仓独立 `.venv`，不使用其他项目源代码或环境。macOS/Linux 使用相同命令；具体已验证环境不由此声明。

## 独立离线发布（无网工作站）

`offline_release/` 是纯标准库的离线发布 CLI（`pack`/`verify`/`install`），在没有 pip、没有第三方包的目标解释器上也能运行。发布包在打包时绑定**同一 CPython 小版本、操作系统与架构**（Linux 另绑定 glibc/musl），wheel 标签、`METADATA`、`RECORD`、pyproject 与锁文件全部交叉核验；缺包、多版本、源码包、损坏 wheel、目标不兼容均拒绝。受管路径拒绝符号链接与目录逃逸。安装时 `venv --without-pip` 建隔离环境，用包内 pip wheel 自举，全程 `--no-index --only-binary=:all:`（禁网络、禁源码构建、禁系统包），并自动烟测四类 CLI；安装或烟测失败只清理本次环境，旧环境与发布包保留。完整契约见 `docs/offline_release.md`。

联网预备机一条命令制包（产物全部在独立 work 目录，不夹带既有环境或仓库目录）：

```sh
scripts/build_offline_bundle.sh ./build-work ./build-work/release-bundle
# 等价手工步骤：
python3 -m venv ./build-work/venv && ./build-work/venv/bin/python -m pip install -r requirements-build.lock
./build-work/venv/bin/python -m pip download --only-binary=:all: -r requirements.lock -d ./build-work/wheel-staging
./build-work/venv/bin/python -m pip download --only-binary=:all: "pip==24.3.1" -d ./build-work/wheel-staging
./build-work/venv/bin/python -m pip wheel --no-deps --no-build-isolation --wheel-dir ./build-work/wheel-staging .
mkdir -p ./build-work/inputs-stage && cp data/*.json data/*.geojson ./build-work/inputs-stage/
python3 -I offline_release/__main__.py pack --wheel-dir ./build-work/wheel-staging \
  --input-dir ./build-work/inputs-stage --pyproject pyproject.toml \
  --requirements-lock requirements.lock --output ./build-work/release-bundle
```

断网目标机（解释器无需自带 pip）：

```sh
python3 -I release-bundle/offline_release/__main__.py verify release-bundle
python3 -I release-bundle/offline_release/__main__.py install release-bundle \
  --python /usr/bin/python3 --env-dir /opt/landcover-venv
```

## 演示、输出与测试

在仓库根目录执行。首次 `run/` 不存在即为干净数据状态；需要干净重演可指定新的输出路径。

```sh
.venv/bin/python -m landcover_cube --output run/coverage
.venv/bin/python -m landcover_cube --output run/replay
.venv/bin/python -m landcover_cube.tiled --tile-size 3 --output run/tiled
.venv/bin/python -m landcover_cube.tiled --zones data/tiled_zones.geojson --parcels data/tiled_parcels.geojson --tile-size 4 --output run/tiled-demo
.venv/bin/python -m landcover_cube.connectivity --zones data/connectivity_zones.geojson --parcels data/connectivity_parcels.geojson --class forest --threshold 1 --output run/connectivity/chain.json
.venv/bin/python -m landcover_cube.acquisition --network data/acquisition_network.json --output run/acquisition
.venv/bin/python -m landcover_cube --parcels run/acquisition/parcels.geojson --output run/acquisition-coverage
.venv/bin/python -m landcover_cube.transition --output run/transition
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

## 离线采集入口

`landcover_cube.acquisition` 读取只含节点、无序界线和地块边引用的 `SurveyNetwork` JSON（`type`、`coordinate_system=LOCAL_METERS`、`nodes`、`edges`、`parcels`），重建并发布两个产物：

- `parcels.geojson`：与现有输入同契约的 FeatureCollection（属性恰为 `{parcel_id,class}`），可直接交给 `python -m landcover_cube --parcels ...`。
- `acquisition_audit.json`：原文 SHA256、节点/边来源位置、逐宗环层级（深度、外壳/孔洞、父环）、每个规范逆时针/顺时环的逐段边 ID、遍历方向与来源节点位置。

节点记录恰为 `{id,x,y}`，边恰为 `{id,a,b}`（`a`/`b` 为节点引用），地块恰为 `{parcel_id,class,edge_ids}`；ID 非空且在各自集合内唯一，引用完整，边至多属两宗，同一宗不重复引用同一边。边是两节点坐标间的直线，不吸附、不补线。每宗边集必须分解为闭环，所用节点度恰为 2；零长、自交、环间相触或穿越、非正面积均拒绝。严格包含深度奇偶决定外壳（偶）、孔洞（奇）与孔中岛（偶、独立成面）；重建结果再经现有 `parse_document` 与同层正面积重叠校验，故共享边界合法、重叠非法。规范环方向、起点与排序后，乱序输入或端点反转不改变 `parcels.geojson`，但审计仍如实记录来源。

demo 数据 `data/acquisition_network.json` 为三层同心正方形（边长 10/6/2，同宗面积 68）加一宗共享外环右边界的 100m² 地块。发布要求目标目录不存在：两产物先写入目标旁的私有暂存目录，全部完成后再重命名发布；失败清理暂存、不留半套，输入字节不变。`tests/test_acquisition.py` 覆盖 68m² 三级环层级、两宗共边、分离多面、断链/自交/相触/穿越拒绝、乱序与端点反转不改变 GeoJSON、失败原子性，以及真实 CLI 产出再接原覆盖入口核对面积。

## 两期覆盖转移

`landcover_cube.transition` 接收同一统计区与前后两期地块（三份输入均先按旧契约全量校验），按空间叠加计算转移，不按跨期地块 ID 配对。每区把一期内同类裁剪面积并成 (coverage_kind, class) 类目，未覆盖为 (`unclassified`, null) 空类；真实名为 `unclassified` 的类保持 classified，绝不与空类混入。转移事实是前后类目几何的正面积交集，按 `(zone_id, 前期类目, 后期类目)` 归并为单个 Polygon/MultiPolygon，保留孔洞与分离分量；未变组合（如 forest→forest）是普通单元，边界接触零面积不产生事实。

```sh
.venv/bin/python -m landcover_cube.transition --zones data/transition_zones.geojson --before data/transition_parcels_before.geojson --after data/transition_parcels_after.geojson --output run/transition
```

每区矩阵行为前期类目、列为后期类目；行和等于前期分类及未覆盖面积，列和等于后期，总和沿用 rel 1e-9 / abs 1e-8 容差守恒；两期互换得到转置矩阵且事实几何不变。三个产物 `transitions.geojson`、`matrix.json`、`transitions.sqlite` 来自同一批归并事实并逐键一致，均含三输入 SHA256（zones、parcels_before、parcels_after）。目标目录必须不存在：全部文件在目标旁私有暂存目录写好并关闭数据库后才重命名发布；任一期非法或写入失败不留半套、不改动输入。合成示例为 100m² 矩形：前期左 60 林、右 40 空，后期左 40 林、右 60 草，转移为 林→林 40、林→草 20、空→草 40。`tests/test_transition.py` 覆盖该矩形位置与矩阵、地块拆并换 ID、孔洞、多面、空期、边界接触、同名类、两期互换转置、重放确定性、失败原子性与真实 CLI。

## 结构与业务契约

- `landcover_cube/source.py：坐标、属性及同层拓扑约束（包围盒候选筛选 + 精确相交检查）`
- `landcover_cube/overlay.py：相交/差集、碎片与守恒`
- `landcover_cube/pipeline.py：空间证据和可查询面积数据`
- `landcover_cube/tiled.py：分块构建、私有 SQLite 暂存、按业务键归并、校验与原子发布`
- `landcover_cube/connectivity.py：按区共边图、逐宗删除脆弱性与确定性 JSON 报告`
- `landcover_cube/acquisition.py：测绘节点/无序界线重建宗地、规范环与逐边审计`
- `landcover_cube/transition.py：两期覆盖转移几何、面积矩阵、SQLite 与原子发布`
- `offline_release/：纯标准库离线发布 CLI（pack/verify/install），见 docs/offline_release.md`
- `scripts/build_offline_bundle.sh：联网预备机一键制包脚本`
- `docs/contract.md`：完整字段/格式、业务键、边界、输出含义与当前约束。
- `data/`：小型确定性合成输入和 `PROVENANCE.md`。
- `tests/`：真实业务值、重复运行、重要边界和失败行为测试。

当前范围：仅自制平面米制坐标；不是 RFC7946 经纬度数据，不声称真实 EPSG、地理面积或自动投影。全量、分块与采集入口均为单进程；无拓扑修复、栅格化、数据库服务、并发执行或中断续传。分块模式与采集入口沿用 `LOCAL_METERS` 坐标与 GEOS 浮点面积模型，不引入新的空间依赖；采集入口不做吸附、补线或面积阈值修复，仅按节点坐标重建直线边。

全部示例为原创合成数据，不代表真实客户、监管标准、实际测量或线上历史。项目没有真实账号、密钥或付费依赖；不包含第三方原始数据。代码及自制数据采用 MIT；依赖保留自身许可。
