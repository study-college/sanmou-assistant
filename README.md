# 三谋助手 0.8.1

《三国：谋定天下》的 Windows 桌面助手，通过截图识别和模拟 Android 输入进行队伍监控与任务调度。

## 下载与使用

从 [Releases](https://github.com/study-college/sanmou-assistant/releases/latest) 下载 Windows x64 完整包和同名 `.sha256` 校验文件。完整解压后双击 `启动助手.cmd`；程序版自带 Python、Qt、OCR 和运行依赖。

首次使用请先发现自己的 MuMu 实例、匹配游戏渠道、校验账号和主城，再保存队伍和目标。先运行“只监控”，确认读数正确后再“开始执行”。详见 [使用指南](docs/09-user-guide.md)。

仓库当前为私有，查看源码和下载附件均需要有访问权限的 GitHub 账号。

## 功能

- 单窗口多账号，每个账号独立配置、Worker、日志和数据库；每个实例只允许一个控制者。
- 武勋按配置的指令顺序持续调度，支持主城与城外空闲队伍、失败隔离与状态监控。
- 按实际奖励次数参与讨贼；额外无奖励协助由独立开关控制，默认关闭。
- 多阶段自动铺路，按真实队伍状态推进并保存进度。
- 城内自动寻找农田、低粮恢复，以及分别配置的签到、月卡和免费奖励等日常。
- 本地实时日志、预览、诊断导出和截图滚动保存。

## 环境与边界

Windows 10/11 x64；已验证模拟器基线为 Windows MuMu 5.30.1.3586。推荐简体中文、1920×1080 横屏。其他电脑、渠道、赛季布局及长期无人值守仍需分别验证。

0.8.1 的现有实机记录包含指令受理、重伤恢复后继续出发与短时连续巡检，不能证明长期稳定。铺路的实战补给下降、遇敌路线重选和击退恢复仍未实现。详见 [版本说明](docs/RELEASE-0.8.1.md)。

停止任务或退出助手会停止新输入，不撤军、不取消游戏内任务、不关闭模拟器。识别和模拟输入可能出错，使用前应自行了解游戏规则；本项目不保证账号安全。

## 从源码运行

使用标准 CPython 3.10–3.12 x64，不使用 Anaconda/Conda。在 PowerShell 中运行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\setup-source.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\start-source.ps1
```

安装器创建隔离的 `.venv` 并安装锁定依赖；首次安装需要网络。也可在安装后运行：

```powershell
.\.venv\Scripts\python.exe -m sanmou_assistant --data-dir .\data
.\.venv\Scripts\python.exe -m pytest -q
```

`tests/` 为可独立运行的脱敏测试；`research-tests/` 保留历史实机图片回归代码，所需私有图片未上传，不属于默认测试集。离线测试不操作游戏，也不代替实机验收。

## 构建完整包

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\build.ps1 -Python .\.venv\Scripts\python.exe
.\.venv\Scripts\python.exe .\scripts\package_release.py
```

完整包包含程序、同版本源码、识别资源、使用说明、第三方许可和逐文件 SHA256 清单；在解压根目录运行 `校验完整性.ps1` 核验。构建不会携带作者或接收者的账号数据。

也可以在 GitHub 的 Actions 中手动运行 **Build Windows release**，填写已有版本标签。流程在 Windows 环境安装依赖、运行独立测试与 OCR 检查、构建和校验完整包，并上传到该标签已存在的 Release；不会按时间自动运行。正式发布前仍需检查发行说明和附件摘要。

## 隐私与维护

仓库只包含产品文件和脱敏文档、测试。账号配置、完整游戏截图、SQLite、运行日志、备份和本机历史交接资料不上传。运行后生成的 `data/`、构建产物与环境文件已列入 `.gitignore`。

诊断包可能包含昵称、坐标和截图，请检查后再分享。给 AI 或维护者操作前，先读 [AGENTS.md](AGENTS.md) 和 [Agent 操作手册](docs/AGENT-GUIDE.md)，明确本次是否允许接管游戏。

本项目尚未选择开源许可证；第三方依赖和资源保留其各自权利及许可。程序包不包含游戏或模拟器。
