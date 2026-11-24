# 三谋助手 0.12.1：给 AI Agent 的操作与维护说明

本说明适用于收到的分发包。以解压目录为项目根目录，不依赖作者电脑的磁盘、用户名、账号或模拟器编号。

## 文件与数据

| 路径 | 用途 |
|---|---|
| `app/SanmouAssistant.exe` | 自带 Python、Qt、OpenCV、ONNX 和 OCR 模型的 Windows x64 程序 |
| `source/sanmou_assistant/` | 与程序同版本的完整产品源码和识别资源 |
| `source/scripts/` | 源码环境安装、启动、构建和组装分发包 |
| `source/tests/` | 不依赖作者账号截图的可运行回归测试；私有实机研究测试不进入公开包 |
| `docs/使用说明.md` | 首次设置、使用和故障反馈 |
| `release-manifest.json` | 版本、文件大小与 SHA256 清单 |
| `data/` | 用户首次运行后创建；账号配置、日志和持久订单在这里 |

根目录启动器和直接打开 `app/SanmouAssistant.exe` 使用同一个根目录 `data`。源码启动器在分发包内也使用这个目录。
不要同时启动源码和程序控制同一个实例。实例互斥会拒绝第二个控制者；不同账号绑定不同实例时可独立运行。
`--data-dir` 优先于环境变量；`SANMOU_DATA_DIR` 可覆盖默认目录。源码直接运行模块且不传参数时，默认是 LocalAppData，
因此在 Agent 操作中始终显式传入绝对 `--data-dir`，避免误用第二份数据库。
0.6.0的当前账号由 `data/accounts.json` 标记；每个账号的配置、会话和SQLite分别在 `data/accounts/<账号ID>/`。旧 `active-profile.json` 只用于首次迁移和显式兼容，不代表迁移后的当前设置。

## 开始前

1. 先读本文件和使用说明，确认用户本次允许的范围。用户只要求研究时，只读文件、日志和数据库，不接管游戏。
2. 获准连接后，在 UI 自动发现 MuMu 实例，按名称、编号和预览选中正确游戏。不要假设编号是 `0`。
3. 核对真实安装的 `nx_main/mumu-cli.exe`，本版本适配 Windows MuMu 5 CLI。作者基准为 MuMu `5.30.1.3586`。
   不使用雷电、蓝叠、MuMu Mac 或旧版 MuMuManager 的接口来冒充已兼容。
4. 让用户本人登录；使用“发现游戏包名”识别渠道。主城账号与坐标必须属于当前区服和角色。
5. 操作前看 `data/accounts.json`、所选账号的 `profile.json`、最新执行会话的 `events.jsonl`、进程与只读 SQLite。
   文档、历史截图和进程存活都不能证明 Worker 当前在运行。

## 命令行

下面均在 PowerShell 中执行。把 `$root` 换成实际解压路径，且始终加引号；不要求管理员权限。

```powershell
$root = 'C:\Games\SanmouAssistant'
$exe = Join-Path $root 'app\SanmouAssistant.exe'
$data = Join-Path $root 'data'
& $exe --data-dir $data --command runtime
& $exe --data-dir $data --command discover
& $exe --data-dir $data --command packages
& $exe --data-dir $data --command preview
& $exe --data-dir $data --command calibrate
& $exe --data-dir $data --command scan
& $exe --data-dir $data --command diagnostics --output (Join-Path $root 'feedback.zip')
```

CLI 默认使用界面当前选中的账号；可用 `--account-id <账号ID>` 明确指定，或用 `--profile <文件>` 读取独立配置（两者不能同时用）。窗口版 exe 在控制台可能没有可见 stdout，读取所选账号 `data/accounts/<账号ID>/sessions/<时间>/result.json` 与 `events.jsonl`；显式 `--profile` 仍使用旧 `data/sessions/`。`result.json` 只保留最近2000条内存摘要，完整长会话必须读取未截断的 `events.jsonl`。
`runtime` 检查本地 OCR，不连接游戏。`discover` 只查询实例；`packages` 要求所选实例已启动，读取安装包列表，不启动游戏。
`preview` 读取当前画面，不导航。`scan` 会启动游戏并导航读取，不下单。`calibrate` 会导航核对主城。
CLI 校验结果通过 `calibrated` 事件输出，**不会自动保存到配置**；UI“校验账号与主城”会在 Worker 运行期只保存已核实的账号和主城两个字段，不夹带其他未保存表单，写盘前仍校验完整配置。
没有自己的有效配置前，优先使用 UI 完成首次引导。

```powershell
# 经用户明确授权后：按已保存策略执行，最多2次新出征、最多运行10分钟。
& $exe --data-dir $data --command run --execute --max-orders 2 --duration 600
# 重置铺路从第一段开始，不连接模拟器；存在待核对订单时拒绝重置相应队伍。
& $exe --data-dir $data --command pave-reset
```

`--duration` 在监控循环轮首检查；启动、单轮读取和正在进行的有界操作可能使总耗时长于指定值。
停止通过 UI“停止任务”、`ctl stop` 或关闭窗口请求；等待“已停止”或进程退出后再接管。
`--command return --execute`、`dailies --execute`、`farming --execute` 都有真实游戏影响，必须在用户授权范围内使用。
不带 `--execute` 的 `run` 只监控。不要使用直接 ADB 输入绕过产品 Worker、账号验证或实例互斥。

## 运行时控制与热换坐标（面向 Agent）

三谋助手支持**运行中免重启热换坐标**（拉线目标、武勋目标、备用征兵点）与实时控制接口，统一服务两种挂机模式：

1. **人机同屏协同（推荐）**：用户正常打开桌面 GUI，桌面窗口会自动启动仅限本机环回（`127.0.0.1`）的控制服务并写入 `data/control-server.json`（含每次启动生成的随机 Bearer Token）。Agent 通过 `ctl` 命令即可实时查看状态、热切坐标、暂停/恢复，GUI 表格与总览会同步刷新，无需杀进程或重走连接/主城/部队/隐藏初始化检查。
2. **无头后台托管**：不开桌面窗口时，可启动常驻控制服务：
   ```powershell
   & $exe --data-dir $data --command serve
   ```
   随后通过同一套 `ctl` 命令启停多账号、监控状态和热切换坐标。

### `ctl` 常用命令（全部输出单段 JSON，退出码 0 成功 / 1 失败）

```powershell
# 1. 列出账号与运行态（服务离线时自动只读回退）
& $exe ctl --data-dir $data accounts

# 2. 查看实时状态（连接、粮食、武勋、各队角标/兵力/决策、当前目标）
& $exe ctl --data-dir $data status --account-id <账号ID>

# 3. 查看当前武勋/拉线目标与备用征兵点
& $exe ctl --data-dir $data targets --account-id <账号ID>

# 4. 运行中热切换拉线目标（默认停用旧目标、新目标置顶启用并立即唤醒下一轮）
& $exe ctl --data-dir $data switch-target --account-id <账号ID> `
    --mode line --address "前线焦点(300,300)" --actions guard,attack,march

# 5. 运行中热切换武勋目标（--keep-others 可保留其他目标的启用状态）
& $exe ctl --data-dir $data switch-target --account-id <账号ID> `
    --mode merit --address "涪县(320,310)" --actions attack,occupy,guard --keep-others

# 6. 热更新备用征兵点（拉线支持 --team main:1 或 --team all；--clear 可清空）
& $exe ctl --data-dir $data set-backup --account-id <账号ID> --mode line --team all --address "前线营帐(310,305)"
& $exe ctl --data-dir $data set-backup --account-id <账号ID> --mode merit --address "备用营帐(330,320)" --enable

# 7. 运行控制：启动 / 暂停 / 恢复 / 立即唤醒 / 刷新画面 / 停止 / 查看近期日志
& $exe ctl --data-dir $data start --account-id <账号ID> --command run --execute
& $exe ctl --data-dir $data pause --account-id <账号ID>
& $exe ctl --data-dir $data resume --account-id <账号ID>
& $exe ctl --data-dir $data wake --account-id <账号ID>
& $exe ctl --data-dir $data refresh --account-id <账号ID>
& $exe ctl --data-dir $data logs --account-id <账号ID> --limit 50
& $exe ctl --data-dir $data stop --account-id <账号ID>
```

### 热更新与安全约束

- **离线降级**：当 GUI 与 `serve` 均未运行时，`accounts`、`targets`、`switch-target`、`set-backup` 会自动以校验模式读写 `profile.json` 并返回 `"live_applied": false, "source": "offline"`；不需要手改 JSON 文件。
- **白名单边界**：运行中仅允许热更新武勋目标、拉线目标与备用征兵点；实例编号、包名、账号主城、队伍任务角色与铺路路线仍须停机修改。
- **不撤军、不丢订单**：换坐标后立即唤醒等待中的巡检循环，空闲/可派队伍马上按新坐标出击，已在旧坐标行军或驻守的队伍自然结束后再转；SQLite 待核对订单保持原状按现场结清。
- **本机令牌隐私**：`data/control-server.json` 含本机随机访问令牌，仅限本机进程使用，不进入诊断导出包，也不要写入日志或分享给第三方。

## 产品约束

- 一次控制一个所选实例，所有输入只经 `transport.Device`，由同一个 `Worker` 串行执行。
- 画面先归一到1920×1080，Android输入再按实际尺寸换算；其他屏幕比例拒绝。推荐实际1920×1080横屏、简体中文。
- 停止/退出停止新输入并释放实例互斥，不关闭模拟器、不撤军、不取消游戏内征兵；停止时已发出的输入可能已被游戏接收。
- 以当前账号、实例和队伍状态安排操作，不把旧观测当作当前现场。误入页面仅在金色游戏返回箭头与独立标题均确认后有限返回（单次2步、10分钟3步），恢复后重读整批并保留pending。暂时无法识别时暂停本轮输入、按巡检重读；异地登录、维护和持续设备故障留证停止。
- 普通出征未确认可同页最多提交3次，后续轮次读现场重试；铺路、讨贼、回城不套用这个宽松规则。
- 坐标定位以实际到达的目标地块坐标为唯一放行条件，不回读输入框；没离开坐标地图或地块坐标不符时自动重输一次，两次仍不对就停在该段，绝不盲点攻占。前往只移动镜头、不派兵不消耗资源。
- 铺路订单与进度持久保存，以到达当前目标并读到“停留”角标推进；行军、忙碌、战斗、撤退期间等待。攻占空地的守军战斗是正常过程，只等待结束自动衔接，不按异常处理；战斗后重伤仍属未覆盖。不能用“还在城外”冒充到达。
- 铺路目标地块没有“攻占”指令（例如别人已铺到）时，跳过该点继续下一个点；先重截一帧再判定，避免识别抖动误跳。
- 出征后出现“补给少于N，战斗伤害降低…”提醒时，勾选“今日登录不再提示”并点“确定”后继续；只处理这一种补给提醒，不改点其他弹窗。补给降到 0 不禁止出征。
- 运行总览常驻显示铺路 `done/total` 与下一点；这只是进度展示，位置与到点仍以现场读取为准。巡检遇到暂未识别页面时先尝试关闭已识别的月卡/活动/聊天/渠道等弹层再重读；只关闭，不自动领取月卡、不进入购买入口、不改日常开关。
- 有未完成或 blocked 铺路路线的队伍保留给铺路，不被武勋、备用补兵或讨贼抢用。
- 改路线后指纹会失效，完成进度重置并重新保留队伍。已有未知订单时先恢复原路线并读现场，不删库强行放行。
- 主城或城外空闲队伍兵力达用户下限、重伤/体力/补给/粮食达标即可出击；城外备用补兵达到本队出击下限后即可普通回城，不要求征满。不把预计预备兵与真实兵力相加。首次账号核对先于批次读取。
- 总览显示本次实时日志，清空显示与重新启动任务不删除磁盘记录。ADB明确短断连可在10分钟内最多3次恢复所选连接并重读，绝不重放失败输入；权限拒绝、恢复失败及其他持续设备错误仍停止。
- 武勋沿用原补兵策略，不统一修改勾选。铺路的营帐征兵、辎重补兵和回城后继续选项已移除；实战补给下降、遇敌路线重选与击退恢复仍未实现。
- 不购买/续费、不发频道消息、不新增集结/委托、不自动转换资源地。亲卫自动出征及铺路未开放。
- 每个账号和区服单独配置；scope由游戏包名、账号与主城坐标组成。切角色后新建配置并重新核验，不能沿用别人的主城。

## 排障与修改

先拿到用户从日志页导出的诊断 ZIP，解压后打开 HTML。默认“安全诊断”只含版本、依赖、会话名、事件计数和不含原始消息值的告警元数据，不含配置、数据库、截图或原始日志。
仅在用户明确选择“完整诊断”后，包内才包含配置、最近业务记录、原始近期日志和最多12张事件引用截图（总计16MiB）；完整模式可能含昵称、坐标和本机路径。CLI 对应显式参数为 `--include-private`。两种模式都不含完整数据库、ADB logcat或登录凭据。
完整日志按会话每个文件最多取2MiB尾部，可能已不含早期根因，必要时向用户索取对应会话和复现时间。

检查顺序：版本与包清单 → MuMu CLI及实例 → 游戏渠道/分辨率 → 账号主城 → 页面识别 → 订单与路线状态 → 堆栈。
遇到 `prepared/uncertain` 先看现场和对应截图，不直接删除订单；不要复制运行中的SQLite主文件当完整备份。
需要备份时用SQLite backup API，或有序停机并确认WAL收束后备份全套数据。
截图是可滚动的诊断缓存，普通/动作/故障预算128/128/32MiB，最长7天；订单持久记录独立保留。
完整诊断包可能包含昵称、坐标、屏幕及本机路径；安全与完整诊断都不会自动上传或发送给第三方。

源码环境：安装标准CPython3.11–3.12 x64，在 `source` 目录运行 `scripts/setup-source.ps1`，首次联网下载依赖。
运行 `.venv/Scripts/python.exe -m pytest -q`；此套件可在分发目录独立执行。`--ui-proof` 在 offscreen 环境只证明控件树和布局可渲染；若系统缺少 CJK 字体，截图中的中文会显示为方框，不能据此宣称文案或字体验收通过。
维护者本机另有未分发账号截图支持的完整实机图片回归，其通过范围须与接收者本机验收分开报告。
开发期间运行源码完整UI，完成修复与验证后使用 `scripts/build.ps1 -Python <venv-python>` 构建；
本机发布再运行 `scripts/package_release.py` 生成程序+源码完整包，缺少匿名映射会默认拒绝；已脱敏公开源码或 CI 需显式传 `--allow-no-anonymization`。不要逐次打包调试，不把接收者的data或截图混入发行包。

完成报告区分：离线测试、程序OCR环境、桌面UI、真实连接、单次提交、路线闭环、长期运行、其他电脑与其他账号。
单机验证不能证明所有渠道、赛季、画面布局和Windows机器都适用。

## 0.11.2 当前行为

队伍状态优先读取头像角标，重伤在无角标时读取头像附近文字。武勋按配置顺序尝试现场存在的指令，不因队伍站在目标上自行裁剪候选。未确认后只在现场仍空闲时换下一条，已忙起来则本轮停手。默认巡检30秒，可派但连续两轮未动作时解除该队冷却再读现场。

开启任务前隐藏选项时，任务开始前打开游戏隐藏，并在运行中保持。铺路使用多阶段状态推进，不按颜色或计时推断完成。

0.11.2 起：铺路进度 `idx` 永远指向下一个未处理点——到点（队伍停在目标点）或确认目标没有攻占指令即连续前移，启动恢复与正常运行共用同一套推进流程，每轮每条路线最多成功派出一段；队伍已停下但坐标连续读不出时暂停恢复该段，避免向旧目标重复派兵；派出前地块动作菜单未确认时不得按“无攻占”跳过。角标识别不再逐套试布局取最强，改为按本队血条二维位置实测（支持连续水平与纵向偏移），模板只负责已知状态、认不出的亮字形保守保持“未识别”；空闲是正向结论——有效队伍行、整帧 HUD 有效、本格无角标且与主城位置证据一致才成立，空闲/练兵与城外位置矛盾一律转“未识别”，不作为可派证据。刷武勋、讨贼、拉线与日常行为规则不变。

## 仓库与隐私

GitHub仓库只保存产品源码、识别用小图、脱敏测试、使用说明和构建脚本。作者的data、完整截图、数据库、运行证据、历史交接、私有实机研究测试和本机配置留在本机，不以 `research-tests` 名义进入公开包。不要为运行这些测试复制别人的账号数据。

原创代码与文档按根目录 LICENSE 的 MIT 许可发布；第三方依赖、模型、游戏图形和商标保留各自权利，见 THIRD_PARTY_NOTICES.md。
