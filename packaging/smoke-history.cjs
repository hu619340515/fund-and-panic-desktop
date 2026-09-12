// v4 历史状态图及正式/回测隔离已纳入统一 Electron smoke。
process.argv[2] ??= require('node:path').join(__dirname, '../desktop/dist/FundAndPanic-Portable-3.0.0-x64.exe')
require('./smoke-desktop.cjs')
