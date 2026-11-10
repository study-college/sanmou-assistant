# 三谋助手 0.9.5

2026-11-10 · Windows x64 · 长铺路路线可用性与测试卫生小版本。

## 本版改动

- **取消铺路路线 30 段上限**：`Profile.validate()` 不再限制每条路线的坐标个数
  （原为 1～30 个）。几十上百段的长路线可以直接保存并逐段推进；仍要求已分配给
  队伍的路线至少有一个坐标，逐点坐标范围/格式校验不变。
- **长路线粘贴**：路线编辑器的粘贴框上限从 1 万字符放大到 20 万字符，避免长路线
  在“识别并添加”阶段先被截断。
- **旧数据不迁移**：已有 `pave_routes` 原样读取、原样保存；不会因为本版改动重写
  或截断任何现有路线。
- **测试不再改写系统剪贴板**：写剪贴板收敛到 `ui.set_clipboard`，测试改为拦截该
  入口，不再把示例地址写进使用者的真实剪贴板。界面“复制地址”按钮行为不变。

## 测试

- 全量 `python -m pytest -q`：**518 passed, 57 skipped**（573.56 秒）。
- 新增回归：
  - `tests/test_pave.py::test_pave_route_has_no_coordinate_count_cap`：
    118 段路线通过校验，存取后仍为 118 段。
  - `tests/test_ui.py::test_pave_page_saves_long_route_without_truncating`：
    经真实窗口保存/重开，118 段与“段数 118”原样保留。
- 剪贴板隔离验证：先写入标记文本，再运行
  `tests/test_ui.py -k game_address_paste`，跑完剪贴板仍为标记文本。

## 打包自检

- 打包 exe 版本 0.9.5、`frozen=true`。
- `--command diagnostics` 退出码 0，诊断 ZIP 正常生成。
- `--ui-proof` 生成 6 页界面截图，标题栏/侧栏显示 v0.9.5。
- 用 118 段演示配置启动打包 exe，界面正常显示该路线（段数 118），无校验错误。
- 完整包文件数、字节数与 SHA256 以包内 `release-manifest.json` 和同目录
  `.zip.sha256` 为准；`校验完整性.ps1` 逐文件校验通过。

## 隐私边界

- 完整包不包含本机账号名、账号 ID、主城坐标、路线坐标、数据库、截图或日志。
- 公开源码按白名单导出；生产 `data/`、`HANDOFF.md`、`STATUS.md` 与私有历史文档
  不上传。

## 证据

- 构建与自检产物：`artifacts/release-0.9.5/`。
- 118 段长路线保全记录：`artifacts/pave-long-route-2026-11-10/receipt.json`
  （本机数据，不随发行包分发）。
- 更多实现细节见 `CHANGELOG.md` 与使用指南中“0.9.5 起”的说明。
