# DSH 百炼浮窗插件

内置 Windows x64 引擎，随 DSH 启停。

完整解压插件 ZIP，打开 DSH 后运行 `Install.cmd`，在插件页刷新并启用 `dsh-bailian-hud`。从「更多 → 百炼浮窗」显示、隐藏、打开面板或退出服务。

获取 AK、查询权限和同步验证见项目的真实截图教程。[打开教程](https://github.com/imYuu-dev/BailianHUD#获取自己的-ak)

打开 DSH「插件 → dsh-bailian-hud」，粘贴 AccessKey ID 和 AccessKey Secret，点击「导入账号」。页面显示已导入数量，可添加多个账号，并在「当前账号」中选择浮窗显示的账号。已有 AK 会自动识别。

配置自动保存在 `%USERPROFILE%\.dsh\storages\bailian-hud`，切换账号后立即同步。

开发入口为 `index.js`、`controller.mjs`、`client.js`。可选 `local-config.json` 支持 `enginePath`、`dataDirectory`、`autoStart`，配置文件只留本机。

测试命令为 `node --test tests/controller.test.mjs`。MIT 许可，第三方许可见发行包 `licenses/`。
