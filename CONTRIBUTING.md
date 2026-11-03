# 参与贡献

欢迎改进识别、调度、安装体验和文档。项目面向 Windows MuMu；问题是否解决，以可复现的行为和相称的验证为准。

## 开始之前

先看 [使用指南](docs/09-user-guide.md)、[已知限制](docs/ROADMAP.md) 和已有 Issues。提交较大的功能前先开 Issue，说明具体使用场景和预期行为。

开发使用标准 CPython 3.11–3.12 x64，在仓库根目录运行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\setup-source.ps1
.\.venv\Scripts\python.exe -m pytest -q
python scripts/audit_publication.py --repository . --history
```

`tests/` 不依赖维护者私有图片；`research-tests/` 需要未发布的实机样本，不属于默认测试集。不要为通过这些历史测试上传自己的完整游戏画面或复制他人的账号数据。

## 修改与验证

- 让一次 PR 解决一个清楚的问题，写明触发条件、实际表现和修改后的行为。
- 游戏输入只通过 `transport.Device`，同一个实例由一个 Worker 串行控制。
- 保留账号与实例归属、实例互斥、未知输入不盲目重放等边界；不添加购买、续费或频道消息操作。
- 配置示例使用虚构角色和地点，不硬编码维护者的账号、主城或模拟器编号。
- 测试与改动相称。修复识别失败时优先保留最小、脱敏的复现，不以测试数量代替实机覆盖。
- 记录验证范围：离线测试、OCR 环境、UI、单次实机操作和长期运行应分别说明。

开发时直接运行源码；完成相关修改后再构建程序，不必每次调试都打包。只有用户明确授权时才接管其游戏，不通过远程诊断自动执行游戏输入。

## 提交 PR

从最新 `main` 建立分支，提交后填写 PR 模板。说明如何验证；没有验证的真实账号或页面，应明确列出。自动检查会运行独立测试和发布内容审计。

原创代码与文档按 [MIT License](LICENSE) 提供。提交第三方代码、图像或模型时保留来源及适用许可；项目的 MIT 许可不替代第三方授权。识别用游戏图形的权利说明见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。
