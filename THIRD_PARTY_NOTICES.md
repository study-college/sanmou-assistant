# 第三方组件与资源

## 项目原创内容

仓库的 MIT License 适用于贡献者有权许可的原创代码与文档。它不授予对第三方软件、模型、游戏图形、商标或游戏本身的额外权利。

## 运行依赖

Python、PySide6/Qt、OpenCV、NumPy、ONNX Runtime、RapidOCR 及其模型等组件保留各自许可。Windows 完整包的 `licenses/` 收集已安装发行包中附带的许可和版权文件；依赖版本见 `requirements-lock.txt`。源代码仓库不会把这些依赖改为 MIT。

PySide6/Qt 以动态库形式放在 `app/_internal`，保持可替换；相关源代码与许可信息见 [Qt for Python](https://www.qt.io/qt-for-python) 和 [Qt 官方下载](https://download.qt.io/official_releases/QtForPython/)。OCR 来源见 [RapidOCR](https://github.com/RapidAI/RapidOCR)。依赖的最终适用条款以该组件随附文本为准。

## 游戏图形、名称与识别资源

`sanmou_assistant/assets/` 的图标、角标及掩码来自游戏界面的小范围裁切或相应处理，`docs/guide-assets/` 包含局部界面示例。这些文件用于图像识别和使用说明，不是完整游戏截图或用户账号资料。相关游戏图形、名称和商标属于各自权利人，不在本项目 MIT 授权范围内；本项目不声称拥有或能够重新授权这些权利。

本项目没有随包分发游戏或模拟器，也不是游戏或模拟器厂商的官方产品。发现资源来源或权利标注问题，可通过仓库反馈入口联系维护者说明具体文件。

`docs/assets/` 中为项目绘制的矢量图遵循项目 MIT License。
