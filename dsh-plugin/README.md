# DSH 百炼浮窗插件

内置 Windows x64 引擎，随 DSH 启停。

完整解压插件 ZIP，打开 DSH 后运行 `Install.cmd`，在插件页刷新并启用 `dsh-bailian-hud`。从「更多 → 百炼浮窗」显示、隐藏、打开面板或退出服务。

获取 AK、查询权限、保存配置和同步验证见项目的真实截图教程。[打开教程](https://github.com/imYuu-dev/BailianHUD#获取自己的-ak)

数据目录为 `%USERPROFILE%\.dsh\storages\bailian-hud`。先启用插件，再将 `bss-credentials.example.json` 复制到数据目录，改名为 `bss-credentials.json`，填写自己的 `aliyunAkId` 和 `aliyunAkSecret`。退出服务后再点击显示，等待同步。

开发入口为 `index.js`、`controller.mjs`、`client.js`。可选 `local-config.json` 支持 `enginePath`、`dataDirectory`、`autoStart`，配置文件只留本机。

测试命令为 `node --test tests/controller.test.mjs`。MIT 许可，第三方许可见发行包 `licenses/`。
