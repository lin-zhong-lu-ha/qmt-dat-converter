# QMT DAT → Parquet 离线转换工具

版本 **0.2.1**。把本机已经下载的 QMT `.DAT` 行情转换成 Parquet，支持首次全量转换、日常增量、指定日期范围和强制核验。提供 Windows 窗口和命令行。

转换过程只读取指定源目录，不改写 DAT，不连接 MiniQMT/QMT 服务、不读取交易账户、不下单、不发送通知，也不会自动下载行情。安装依赖、Git 拉取代码需要联网；转换本身可离线运行。

本仓库只包含通用源码、合成测试和说明。没有预置本机目录、真实行情、账号、令牌、运行报告或旧开发仓库历史。每台电脑独立保存 `config.local.json`，该文件不进入 Git。

## 台式机首次使用

准备 Windows 和 **Python 3.12（含 Tcl/Tk）**。以下命令在拉取后的工具目录里运行。示例路径可以更换；不要把工具放进 QMT 的安装目录。

1. 从你的私人仓库克隆，或解压源码包。进入包含 `run_converter.py` 的目录。

   使用 Git 时先安装 Git for Windows，并确保当前 GitHub 账号有该私人仓库权限。把下一行的中文占位符替换为你的仓库地址，按 Git 提示完成授权，或使用已经配置好的 SSH；无需把访问令牌写进工具配置。

   ```powershell
   git clone "你的私人仓库地址" qmt-dat-converter
   cd qmt-dat-converter
   ```

   使用 ZIP 时直接解压后打开该目录的 PowerShell，无需执行这两行。

2. 创建该工具专用的 Python 环境并安装依赖：

   ```powershell
   py -3.12 -m venv .venv
   .\.venv\Scripts\python.exe -m pip install -r requirements.txt
   ```

   如果没有 `py` 命令，改用 Python 3.12 的 `python.exe` 完整路径执行第一行。无需激活虚拟环境，也无需修改系统 Python。锁定的运行依赖为 NumPy 2.1.2、PyArrow 23.0.0；建议使用已验证的 Python 3.12。

3. 双击 **`启动转换工具.cmd`**。启动器优先使用此目录的 `.venv`。也可直接运行：

   ```powershell
   .\.venv\Scripts\python.exe run_converter.py
   ```

4. 在窗口里设置：

   | 项目 | 填写内容 |
   | --- | --- |
   | 源目录 | QMT 安装目录下包含 `SH`、`SZ` 的 **`datadir`**，例如 `D:/QMT/datadir` |
   | 输出目录 | 另一个独立目录，例如 `E:/MarketData/qmt-parquet`，首次使用须为空或尚不存在 |
   | 周期 | 日线、分钟线或两者 |

   输出目录必须在 QMT 安装目录之外，也不能是源目录的上级目录。建议输出放在代码仓库之外。每个输出目录绑定源目录，不要把另一台电脑的状态库直接套在不同源路径上。

5. 点击 **“保存本机配置”**。下次启动自动加载目录和周期；证券筛选、日期范围、运行模式不保存，避免误用上次的限制。
6. 首次先选择少量证券、日线和短日期范围试转并查看报告。确认后清空证券和日期限制，再进行大范围转换。

修改窗口中的目录/筛选条件后会自动扫描预览。扫描不转换行情，但会在有效输出目录建立工具标记、状态库和报告。转换或核验需点击对应按钮；保存配置本身不会发起转换。

## 也可以直接编辑配置

在工具目录运行：

```powershell
Copy-Item config.example.json config.local.json
notepad config.local.json
```

将空路径改成这台电脑的真实目录，例如：

```json
{
  "source": "D:/QMT/datadir",
  "output": "E:/MarketData/qmt-parquet",
  "period": "both"
}
```

JSON 路径建议使用 `/`；如果使用反斜杠，必须写成 `D:\\QMT\\datadir`。支持 UTF-8（含 BOM）。`period` 仅接受 `1d`、`1m`、`both`。没有本机配置时窗口保持空白；命令行操作必须提供路径。损坏的配置或拼错字段会报错，不会静默转到其他目录。

默认配置始终取自工具目录，不受启动时当前工作目录影响。配置里的相对路径基于该配置文件所在目录解析；命令行显式传入的相对路径按当前工作目录解析。参数优先级为：**显式命令行参数 > 配置文件 > 空目录/两种周期**。可用 `--config` 指定其他 JSON 配置；显式指定的文件不存在会停止执行。

## 日常操作

先等 QMT 完成本次数据下载，再运行转换。下列命令读取已保存的 `config.local.json`：

```powershell
# 清点可识别文件；不转换行情
.\.venv\Scripts\python.exe run_converter.py scan

# 首次全量检查并转换所选范围
.\.venv\Scripts\python.exe run_converter.py convert --mode full

# 以后每天/每周下载完成后使用；这是默认模式
.\.venv\Scripts\python.exe run_converter.py convert --mode incremental

# 只转换指定一天（或把两端改成一周）；包含起止日期
.\.venv\Scripts\python.exe run_converter.py convert --mode range --start 20260921 --end 20260921

# 强制重读源内容和已转换输出进行比对；不修复数据
.\.venv\Scripts\python.exe run_converter.py verify
```

增量模式会复用未变化文件的校验状态，源文件变化时重新解析并识别新增或修订日期。指定日期范围控制发布范围，仍可能需要读取完整 DAT，并不保证只读取一天的字节。不要在首次全量时保留试用的证券/日期限制。

**指定日期范围时的价格校验（0.2.1）：** 开、高、低、收异常发生在范围内，会报错并阻止相关分区更新；发生在范围外，仅警告并打印异常日期及价格，不阻塞本次范围。无论使用 `full`、`incremental` 或 `range`，只要填写了日期边界就按该边界处理。未填写边界时校验全部历史，历史异常仍会报错。头部、记录长度、时间顺序、未知状态等结构性检查仍针对整个文件，不能因日期筛选忽略。

例如源文件早年存在开盘高于最高价，而你只需要近期数据，可明确设置起止日期。异常价格不会修正，也不会在所选范围外发布；本次“完成／已验证”仅指报告中请求的转换范围。范围转换不会把源文件标为全历史已校验，后续无日期限制的增量仍会重检。报告将范围外警告与排除项分开，并在阻塞时列出未更新分区及相关股票。原有状态库和输出无需删除，修复后用同一输出目录重跑失败范围即可。

单次覆盖配置、限制到某个证券的示例：

```powershell
.\.venv\Scripts\python.exe run_converter.py convert --period 1d --code 600000.SH --mode full
.\.venv\Scripts\python.exe run_converter.py scan --source "D:/QMT/datadir" --output "E:/MarketData/another-output" --period 1d
```

`--code` 可重复，格式为 `600000.SH` 或 `000001.SZ`；窗口里可以逗号分隔。日期接受 `YYYYMMDD` 或 `YYYY-MM-DD`。`range` 模式两端日期必填。完整参数见 `run_converter.py convert --help`。

工具没有定时任务，不会自行每天运行。关闭运行中的窗口会请求取消，等待当前文件或分区处理结束。运行返回 `partial`、`failed` 或 `cancelled` 时不要视为全部完成；查看报告后修正问题并重跑。

## 支持范围和输出位置

只支持当前已识别的 QMT DAT 布局，并非任意扩展名为 DAT 的文件。识别沪深六位证券代码，以及日线 `86400`、一分钟 `60` 目录。未知目录、无法分类代码和格式异常文件列入报告，不推测其含义。

```text
源 datadir/
  SH/86400/600000.DAT
  SH/60/600000.DAT
  SZ/86400/000001.DAT
  SZ/60/000001.DAT

输出目录/
  data/qmt-daily-monthly/YYYY/MM/SH.parquet   # 股票日线，SZ 同理
  data/qmt-minute/YYYYMMDD/SH.parquet        # 股票分钟，SZ 同理
  data/indexes/daily/...                    # 已识别指数单独保存
  data/indexes/minute/...
  reports/<run_id>.json                     # 结构化报告
  reports/<run_id>.txt                      # 中文摘要
  .qmt-converter.json                       # 输出目录归属标记
  .state/cache.sqlite3                     # 增量及校验状态
  .staging/                                # 运行时暂存
  .journal/                                # 发布及恢复记录
```

命令行结束时输出报告路径；窗口中“打开报告”打开中文摘要。数据字段包括 `trade_date/code/exchange/datetime/minute/timestamp/open/high/low/close/volume/amount`。`timestamp` 为 Unix 毫秒，文本日期时间为北京时间。Parquet 使用 Snappy、Parquet 1.0、data page 1.0、无 dictionary。

数据文件可以供 pandas、PyArrow 等读取。已有研究项目若还依赖自己的清单、数据类型或复权口径，应先适配并验证；本工具不生成其他系统的生产清单，也不进行复权。

## 校验标记意味着什么

校验记录保存在本地状态库和运行报告里，与源文件指纹、源日期及输出分区关联。后续增量会复用适用记录；不是永久有效、脱离文件变化的“认证”。

| 项目 | 本工具的范围 |
| --- | --- |
| 转换一致性 | DAT 解析、缓存、Parquet 读回及核验模式内容比对 |
| 市场覆盖 | 不能仅凭现有文件判断是否齐全；需独立证券名单、日历和停牌资料 |
| 数据真实性 | 同源一致不能证明行情本身正确；需可信独立来源比较 |
| 外部证据 | 支持显式 `--evidence` 提供的离线 JSON 记录；仅证明所提供样本的匹配情况 |
| 字段映射 | 未经独立来源确认的价格、成交量单位及未知记录字，不宣称已完成独立验证 |

增量快速跳过依赖文件大小、时间和文件身份等元数据。如果外部程序改了内容却保留这些属性，需运行 `verify` 或 `convert --mode full` 强制重读。建议定期核验，或在怀疑历史数据修订时核验。核验不修复输出，确认源数据正确后再运行全量转换。

不要随意删除 `.state`、归属标记或恢复记录。中断后再次转换会检查可恢复事务；极早期中断可能留下未登记的临时/备份文件，先保留排查。状态库会额外占磁盘，DAT 转 Parquet 加缓存不一定更小。未提供全市场速度保证：时间取决于 DAT 数量、历史长度、磁盘、分区数量及修订情况。

## 私人仓库与更新

只上传本工具目录，**不要上传整个行情/策略工作目录**。`.gitignore` 已排除本机配置、DAT、Parquet、SQLite、日志、常用输出/报告目录、虚拟环境和密钥文件。它不能阻止强制添加或事后已跟踪文件继续上传；新增文件后仍需检查 `git status --short` 和暂存差异。

报告和缓存可能记录本机绝对路径、证券代码、日期及数据样本，排查时也应按需要脱敏。不要把真实路径填写到被跟踪的 `config.example.json`；不要把账号或令牌加入配置，本工具不需要它们。私人仓库仍会把已提交内容存储在托管平台。

台式机已配置完成后，后续更新通常只需：

```powershell
git pull --ff-only
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

未跟踪的 `config.local.json` 和仓库外的行情数据不随拉取更新。每台电脑重新配置自己的路径、独立建立虚拟环境。迁移已有输出及状态时需保留完整目录并核实绑定路径；最简单的跨机方式是在新电脑选择新输出目录，从本机 DAT 重新建立。

## 开发验证

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m pytest -q
```

测试使用临时目录和合成 DAT，不读取真实行情。窗口测试需要 Tcl/Tk；Windows 部分符号链接测试在无相应权限时会跳过。版本 0.2.0 完成脱敏、本机配置和虚拟环境启动；0.2.1 修复范围外历史价格异常阻塞近期转换，并新增范围、缓存、共享分区阻塞和恢复回归。业务字段、分区布局和既有状态库格式保持兼容。
