# Windows 初始化材料

这些文件随仓库直接提供，供 `scripts/install_windows.ps1` 和 `scripts/bootstrap.py --offline` 使用。

- `python-3.14.5-amd64.exe`：Python 官方的 Windows x64 完整安装程序。
- `wheels/`：标准 CPython 3.14、Windows x64 的完整运行依赖，以及项目安装需要的 setuptools、wheel。
- `requirements.txt`：固定版本和校验值的安装清单。
- `manifest.json`：每个安装包的官方来源、SHA-256、大小、版本和依赖元数据。
- `PYTHON-LICENSE.txt`：Python 许可证。各 Python 包的许可证保留在原始 wheel 内。

初始化时先复用已有 Python，缺少时从本地安装。完整材料安装无需访问包索引。文件缺失时，Windows 安装脚本会按 manifest 中的 URL 补下载并核对校验值。

MCP 固定为 1.30.0，MPh 固定为 1.4.0。其余运行依赖按目标平台解析后固定；开发和测试工具通过单独的 `--online --dev` 安装。

Windows 初始化任务见 [initialize-windows.md](../../docs/initialize-windows.md)。本目录的哈希与依赖元数据已核对；实际 Windows 运行及 COMSOL 连接由公司电脑验证。

## 更新材料

在 Windows x64 的 Python 3.14 环境中确定新的依赖组合，再下载全部二进制依赖和安装构建依赖。同步更新安装清单、来源和校验值。运行 `python -m unittest tests.test_install_bundle` 检查目标平台、完整依赖关系和文件校验值。

Python 官方来源：[发布页面](https://www.python.org/downloads/release/python-3145/)。第三方包来源：manifest 中记录的 PyPI 文件地址。
