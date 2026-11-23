# 后端二次开发目录

在本目录新增普通 `.py` 模块即可由应用自动发现，无需修改 `app/main.py`。以下划线开头的模块不会加载。

以 [`_template.py.example`](_template.py.example) 为起点，并遵循 [`docs/secondary-development.md`](../../../docs/secondary-development.md)。模板文件不会参与运行。

源码扩展通过宿主登记带命名空间的路由、启动或关闭钩子及通知格式化器。部署已编译主程序时，可以按 [外部后端扩展包契约](../../../docs/extension-packages.md#后端--前后端完整包)交付 ZIP，在面板预检并重启后加载。
