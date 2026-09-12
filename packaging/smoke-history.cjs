// 2.0.4 起近一年仅显示正式收盘；使用真实便携版验证隔离估计记录、布局和图表导出。
process.argv[2] ??= require('node:path').join(__dirname, '../desktop/dist/FundAndPanic-Portable-2.0.4-x64.exe')
require('./smoke-charts.cjs')
