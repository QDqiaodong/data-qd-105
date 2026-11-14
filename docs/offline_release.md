# 离线发布 CLI（offline_release）

`offline_release` 是纯标准库实现的离线发布工具，为无网测绘工作站提供
`pack` / `verify` / `install` 三个子命令。它本身不依赖 pip 或任何第三方
包，因此可以在只带 CPython 标准库的目标机上直接运行。

## 绑定目标

每个发布包在打包时绑定一个具体目标（写入 `MANIFEST.json.target`）：

- CPython 小版本，如 `cp311`（`3.11.x`）。不同小版本一律拒绝，即使 wheel
  本身标了通用的 `py3`；
- 操作系统族：`linux` / `darwin` / `windows`；
- 机器架构：`platform.machine()`，如 `aarch64`、`x86_64`；
- Linux 另记录 libc（`glibc` / `musl`），manylinux 与 musllinux 互不兼容。

wheel 兼容性按 PEP 425 标签判定：必须至少有一个
`(python tag, abi tag, platform tag)` 三元组匹配目标。异架构、异小版本、
异 libc 的 wheel 在 `pack` 与 `verify` 阶段都会被拒绝。

## 包结构

```text
release-bundle/
├── MANIFEST.json                # 唯一事实来源（模式见下）
├── wheels/                      # 项目 wheel、锁定运行依赖、恰好一个 pip wheel
├── inputs/                      # 四类 CLI 的合成输入（来自仓库 data/）
├── meta/
│   ├── pyproject.toml           # 打包时的项目身份/依赖来源副本
│   └── requirements.lock        # 精确运行锁副本
└── offline_release/             # 工具自身副本（纯 .py，目标机无 pip 可用）
```

打包只读取三个显式给定位置：预备 wheel 目录、输入目录、以及
pyproject/锁文件。不读取任何已安装环境，也不读取仓库其他目录；非 wheel
文件不得出现在预备目录中，源码包（sdist/egg/松散文件）一律拒绝。

## MANIFEST.json 记录内容

- `manifest_schema`：当前为 `1`；
- `target`：绑定的 CPython 小版本/系统/架构/libc 及 `triple`；
- `project`：发行名（原始名与 PEP 503 规范化名）、版本、`requires-python`、
  声明依赖；
- `runtime_lock`：锁文件中每个 `name==version`；
- `wheels`：每个 wheel 的文件路径、角色（project/runtime/pip/extra）、
  发行名/版本、完整标签集合、是否 purelib、`Requires-Python`、解析后的
  `Requires-Dist`（含 marker）、文件长度与 SHA-256；
- `inputs` 与 `files`：每个受管文件的相对路径、长度与 SHA-256（MANIFEST
  自身除外）。

## 校验规则（pack 与 verify 同一套）

1. 受管路径的每个组件都用 `lstat` 检查，符号链接（含悬空链接）拒绝；
   词法归一化后逃逸出根目录（`..`、绝对段）的路径拒绝。
2. 每个 wheel：必须是 `.whl`；zip 结构边界完整（拒绝前置/追加在 zip 结构
   之外的字节，追加垃圾无法靠 CRC 发现，因此单独检查 EOCD/中央目录边界）；
   成员路径不得绝对化或含 `..`，不得是符号链接成员；`RECORD` 中每个成员
   的 SHA-256 与长度逐一重算核对，RECORD 未列出的成员拒绝；恰好一个
   `.dist-info`；文件名与 `METADATA` 的 Name/Version 一致；文件名标签必须
   是 `WHEEL` 元数据 `Tag:` 的子集。
3. 依赖策略：恰好一个项目 wheel 且版本等于 pyproject；恰好一个 pip wheel；
   锁文件每项都有对应且同版本的 wheel；pyproject 依赖与锁一致；按目标
   环境求值全部 PEP 508 marker 后做完整闭包检查，缺包、版本不满足、多版本
   共存、未声明的多余 wheel 全部拒绝。
4. `verify` 不信任清单：重算全部文件长度/SHA-256，重开并重检每个 wheel，
   重新解析包内的 pyproject/锁文件并与清单比对，未声明文件与缺失文件都
   报错。可用 `--python /path/to/python` 额外要求某个解释器的目标三元组
   与包一致。

## install 语义

```sh
python -I /path/to/bundle/offline_release/__main__.py install \
    /path/to/bundle --python /usr/bin/python3 --env-dir /path/to/new-venv
```

- 先 `verify` 预检并通过目标解释器自报的三元组比对，失败则什么都不创建；
- 用指定解释器 `venv --without-pip` 建隔离环境（解释器无需自带 pip 或
  ensurepip），`include-system-site-packages=false`；
- 以标准库直接运行包内 pip wheel（wheel 即 zip）引导安装 pip 自身，再用
  `pip install --no-index --only-binary=:all: --no-deps <显式 wheel 路径>`
  安装其余 wheel：无索引、无缓存、只接受二进制 wheel，因此禁网络、禁止
  源码构建、系统包无法进入；
- 安装期间在环境旁的私有目录生成一个按**显式文件路径**加载的套接字阻断
  模块与 launcher（`-I` 下 PYTHONPATH 被忽略、发行版自带 sitecustomize 又会
  遮蔽同名模块，因此不依赖这两种机制）。阻断器保留 socket 构造器（stdlib
  的 `ssl` 在导入期就需要它），只把 `socket.connect`/`connect_ex` 与
  `create_connection` 替换为直接抛 `OSError`，从而 ssl 可正常导入而任何出站
  连接都建立不起来；私有目录在结束时删除。可传 `--allow-network` 关闭该
  阻断器，`--no-index` 仍然强制生效；
- 烟测在环境内实际运行四类 CLI（见下），并直接 import 项目包；
- 安装或烟测失败：删除本次新建的环境目录；已存在的环境在创建前即被拒绝，
  因此旧环境与发布包始终保留。

## 烟测断言（安装时自动执行）

1. 采集入口：三级同心环宗地 `P_NEST` 面积 **68**；
2. 原例覆盖入口：两区总面积 **200**；
3. 分块入口与全量入口在同一输入上逐键等价（五类区指标完全相等）；
4. 连通链 A-B-C：删除 B 的绝对损失 **2**，且 B 排名第一。

## 制包命令（联网预备机）

```sh
scripts/build_offline_bundle.sh ./build-work ./build-work/release-bundle
# 等价手工步骤见 README“独立离线发布”一节
```

跨目标平台制包需在匹配目标 CPython 小版本/系统/架构/libc 的机器上进行；
本机不做跨平台 wheel 伪造。

## 目标机验证与安装（断网）

```sh
python -I bundle/offline_release/__main__.py verify bundle
python -I bundle/offline_release/__main__.py verify bundle --python /usr/bin/python3
python -I bundle/offline_release/__main__.py install bundle \
    --python /usr/bin/python3 --env-dir /opt/landcover-venv
```

退出码：成功 0；verify 发现问题 1；参数/打包/安装错误 2。
