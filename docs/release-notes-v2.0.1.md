# 2.0.1：修复恐慌指数实网采集

修复 Windows 打包版采集子进程误入服务参数解析的问题；此前引擎健康检查可通过，但真实行情采集会失败。

- 增加 PyInstaller `freeze_support()`，完善子进程异常退出、超时及回收；发布前必须通过实际 EXE 的三种多进程诊断。
- 拒绝东方财富截断的全市场数据及不完整代码表，避免把前 100 只上涨股票当作全市场。默认使用新浪全市场分页、腾讯指数，保留备用来源。
- 按固定字段解析新浪 IF 最新价和买卖价，避免将开盘价、最高价误作当前价格。
- 不把新浪与其 AKShare 封装作为独立验证，不把不同 QVIX 标的进行同指标交叉比较。
- 行情采集失败显示具体原因和重试时间；健康引擎不会因行情失败被重启。正式收盘数据与历史曲线仍可独立读取。
- 保留已有用户配置、数据库和指数算法。冷启动历史不足时保持缺项、暂定质量标记，不补造历史或指标。

接口实现参考：[AKShare 新浪期货源码](https://github.com/akfamily/akshare/blob/main/akshare/futures/futures_zh_sina.py)、[AKShare QVIX 源码](https://github.com/akfamily/akshare/blob/main/akshare/index/index_option_qvix.py)、[easyquotation](https://github.com/shidenggui/easyquotation)。

安装版和便携版均内置 Python。退出旧版本后运行新版即可，用户数据路径不变。Windows 安装包尚未签名。
