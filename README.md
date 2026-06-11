<div align="center">

# 🤖 Hermes Launcher

**一键启动 Hermes Agent + WebUI 的现代化桌面启动器**

![Windows](https://img.shields.io/badge/Windows-11-blue?logo=windows&logoColor=white)
![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white)
![PyInstaller](https://img.shields.io/badge/Build-PyInstaller-FF6B6B)
![License](https://img.shields.io/badge/License-MIT-green)

</div>

---

## ✨ 特性

- 🪟 **Win11 风格 GUI** — 基于 CustomTkinter 的现代化界面，自动跟随系统浅色/深色主题
- 🤖 **智能启动顺序** — 先检测 Hermes Agent 状态，再按顺序启动 Agent → WebUI
- 📥 **自动下载/更新** — 从 GitHub 自动下载最新版 Hermes WebUI，带进度显示
- 🔄 **实时状态监控** — 卡片式状态面板 + 控制台日志输出，实时掌握运行状态
- 📦 **免安装、便携** — 所有文件保存在程序目录，不写注册表，绿色运行
- 🌐 **一键打开 WebUI** — 启动后可直接点击打开浏览器访问 WebUI
- ⚙ **灵活配置** — 支持自定义端口、主机、Python 路径、Hermes 路径
- 🎨 **主题切换** — 支持 system / dark / light 三种外观模式

## 🚀 快速开始

### 前置要求

- **Hermes Agent** — 已安装且 `hermes` 命令可用（[安装指南](https://github.com/nesquena/hermes)）
- **Python 3.10+** — 如果使用源代码运行
- **网络连接** — 首次下载 WebUI 需要

### 方式一：下载预编译的 EXE（推荐）

1. 前往 [Releases](https://github.com/lcohvne-tomorin/Hermes-Launcher/releases) 下载最新版 `HermesLauncher.exe`
2. 双击运行即可

### 方式二：从源码运行

```bash
# 克隆仓库
git clone https://github.com/lcohvne-tomorin/Hermes-Launcher.git
cd HermesLauncher

# 安装依赖
pip install customtkinter

# 运行
python launcher.py
```

### 首次使用

1. 如果 Hermes WebUI 未安装，点击 **「📥 下载/更新 WebUI」**
2. 点击 **「▶ 启动全部」** — 程序会自动检测 Hermes Agent，然后启动 WebUI

## 🔧 构建指南

### Windows EXE

```bash
# 确保已安装 PyInstaller
pip install pyinstaller

# 使用构建脚本
build_windows.bat
```

> 构建产物位于 `dist/HermesLauncher.exe`，约 33 MB。



### 如何更换 WebUI 端口？

点击 **「⚙ 设置」**，在 "WebUI 设置" 中修改端口号（默认 8787）。

### 启动失败怎么办？

查看主界面下方的 **「📋 输出日志」** 面板，错误信息会实时显示在那里。常见问题包括：
- Python 未安装或缺少 `customtkinter`
- Hermes Agent 未安装或不在 PATH 中
- 端口被占用

## 🧪 技术栈

| 组件 | 技术 |
|------|------|
| Hermes WebUI | [hermes-webui](https://github.com/nesquena/hermes-webui) |
| GUI 框架 | [CustomTkinter](https://github.com/TomSchimansky/CustomTkinter) 5.2+ |
| 打包工具 | [PyInstaller](https://pyinstaller.org/) 6.20+ |
| 目标运行时 | Hermes Agent + Hermes WebUI |
| 支持平台 | Windows 10/11 |

## 📄 许可证

[MIT](LICENSE)

---

<div align="center">
  <sub>Made with ❤️ for the Hermes community</sub>
</div>
