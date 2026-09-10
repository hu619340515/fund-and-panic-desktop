# 基金估值 Windows 桌面版

一个可双击运行的 Windows 基金估值小浮窗。应用支持系统托盘、基金增删与排序、自动刷新、开机启动，以及多数据源自动降级。

## 下载

- [安装版 1.0.4](https://github.com/hu619340515/valuation-of-funds-skill/releases/download/v1.0.4/FundValuation-Setup-1.0.4-x64.exe)
- [便携版 1.0.4](https://github.com/hu619340515/valuation-of-funds-skill/releases/download/v1.0.4/FundValuation-Portable-1.0.4-x64.exe)

![主窗口](./docs/ui-preview.png)

![托盘悬停迷你窗](./docs/ui-mini-preview.png)

## 功能

- Windows 10/11 x64 桌面应用，提供安装版和便携版 EXE。
- 默认使用 420 × 780 的深色窄窗；行情以单行显示，点击基金才展开净值、日期、来源和管理按钮。
- 默认置顶并使用 94% 不透明度，可在设置中关闭置顶或把透明度调整为 55%–100%。
- 最小化后进入 Windows 任务栏；关闭主窗口后驻留系统托盘。托盘可打开、刷新、切换开机启动或彻底退出。
- 鼠标悬停在系统托盘图标上时，会显示不抢焦点的迷你行情窗，移开后自动隐藏。
- 首次运行预置原项目的 11 只基金，可添加、删除和调整顺序。
- 显示估算净值、估算涨跌幅、官方净值、数据日期、实际数据来源和数据新鲜度。
- 交易时段主行显示“实时估”；跨日或官方净值发布后显示“收盘”日涨跌，旧估值只在展开详情中保留，避免把昨日估值误认为实时行情。
- 默认每 60 秒自动刷新，可在设置中选择 30 秒、60 秒、2 分钟或 5 分钟。
- 配置和最后成功行情保存在 Electron 用户数据目录（Windows 通常位于 `%APPDATA%`）。

## 数据源顺序

1. 天天基金旧版 `fundgz` JSONP 接口。
2. 天天基金 `FundValuationLast` 批量接口。
3. 新浪基金估值接口，主口径缺失时使用第二口径。
4. 东方财富 `pingzhongdata` 官方净值。
5. 本地最后成功缓存。

旧接口连续三次完全失败后会熔断 30 分钟，避免重复等待已失效的接口。只有估值时间属于当前上海日期的数据会显示为“今日估值”；官方净值、历史估值和缓存不会计入实时平均涨跌幅。

本项目根据公开接口的响应格式独立实现适配器。备用数据源设计参考了 [hzm0321/real-time-fund](https://github.com/hzm0321/real-time-fund)，未复制其 AGPL-3.0 源码。

恒生 LIGHT云“小梵基金估值”接口未在 v1 中接入：该接口需要开发者 App Key/App Secret、OAuth2 Access Token 和相应服务权限，本应用不会要求或保存这些凭证。

## 本地开发

```powershell
npm.cmd install
npm.cmd run dev
```

验证与生产构建：

```powershell
npm.cmd run typecheck
npm.cmd test
npm.cmd run build
```

生成 Windows 安装版和便携版：

```powershell
npm.cmd run dist:win
```

产物位于 `dist` 目录：

- `FundValuation-Setup-1.0.4-x64.exe`：安装程序。
- `FundValuation-Portable-1.0.4-x64.exe`：便携版。

## 安全设计

- 所有网络请求和文件操作仅在 Electron 主进程执行。
- 渲染层启用 `contextIsolation` 和沙箱，关闭 Node.js 访问。
- preload 只暴露白名单、类型化的基金管理接口。
- 主进程仅允许访问代码中列出的 HTTPS 数据源域名。
- 基金代码、设置值和基金排序均在主进程再次校验。

## 说明

基金估值来自公开接口，可能延迟、缺失或与基金公司最终公布净值存在偏差，仅供个人学习和参考，不构成投资建议。

当前构建没有商业代码签名，首次运行时 Windows SmartScreen 可能显示“未知发布者”。
