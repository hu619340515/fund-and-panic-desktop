// 便携版外层解压启动器不会转发调试输出，直接连接内层 Electron 的本地调试端口。
const { spawn } = require('node:child_process')
const { createServer } = require('node:net')
const { chromium } = require('../desktop/node_modules/playwright')

async function port() {
  return new Promise((resolve,reject) => {
    const server = createServer()
    server.once('error',reject)
    server.listen(0,'127.0.0.1',()=> {const value=server.address().port;server.close(()=>resolve(value))})
  })
}
async function retry(operation, timeout=120000) {
  const deadline = Date.now()+timeout
  let error
  while(Date.now()<deadline) {
    try {return await operation()} catch(value) {error=value;await new Promise(resolve=>setTimeout(resolve,200))}
  }
  throw error ?? new Error('便携版启动超时')
}
exports.launchPortable = async (executablePath,args,env) => {
  const chromePort = await port()
  const nodePort = await port()
  const launcher = spawn(executablePath,[`--inspect=127.0.0.1:${nodePort}`,`--remote-debugging-port=${chromePort}`,...args],{env,windowsHide:true,stdio:['ignore','pipe','pipe']})
  let diagnostics=''
  launcher.stdout.on('data',data=>{diagnostics=(diagnostics+data).slice(-16000)})
  launcher.stderr.on('data',data=>{diagnostics=(diagnostics+data).slice(-16000)})
  let ws
  let browser
  let pid
  try {
    const targets = await retry(async()=> {
      const response=await fetch(`http://127.0.0.1:${nodePort}/json/list`,{signal:AbortSignal.timeout(1500)})
      const values=await response.json()
      if(!values[0]?.webSocketDebuggerUrl) throw new Error('等待便携版内部引擎调试端口')
      return values
    })
    ws=new WebSocket(targets[0].webSocketDebuggerUrl)
    await new Promise((resolve,reject)=>{ws.addEventListener('open',resolve,{once:true});ws.addEventListener('error',reject,{once:true})})
    let id=0
    const pending=new Map()
    ws.addEventListener('message',event=> {
      const message=JSON.parse(event.data)
      const handler=pending.get(message.id)
      if(handler){pending.delete(message.id);handler(message)}
    })
    const evaluate = expression => new Promise((resolve,reject)=> {
      const current=++id
      const timer=setTimeout(()=>{pending.delete(current);reject(new Error('便携版主进程响应超时'))},10000)
      pending.set(current,message=>{clearTimeout(timer);if(message.error||message.result?.exceptionDetails) reject(new Error(JSON.stringify(message.error??message.result.exceptionDetails)));else resolve(message.result?.result?.value)})
      ws.send(JSON.stringify({id:current,method:'Runtime.evaluate',params:{expression,returnByValue:true,awaitPromise:true}}))
    })
    pid=await evaluate('process.pid')
    browser=await retry(()=>chromium.connectOverCDP(`http://127.0.0.1:${chromePort}`,{timeout:2000}))
    return {
      firstWindow:()=>retry(async()=>{const page=browser.contexts()[0]?.pages()[0];if(!page)throw new Error('等待便携版窗口');return page}),
      evaluate:fn=>evaluate(`(${fn.toString()})(process.mainModule.require('electron'))`),
      close:async()=> {
        // 调试器仍连接时 Node 会等待连接断开；先派发退出，再主动断开调试连接。
        ws.send(JSON.stringify({id:++id,method:'Runtime.evaluate',params:{expression:"process.mainModule.require('electron').app.quit()"}}))
        await new Promise(resolve=>setTimeout(resolve,300))
        ws.close()
        await browser.close().catch(()=>{})
        await retry(async()=>{try {process.kill(pid,0)}catch{return true}throw new Error('等待便携版退出')},20000)
      }
    }
  } catch(error) {
    ws?.close()
    await browser?.close().catch(()=>{})
    if(pid) {try{process.kill(pid)}catch{}}
    launcher.kill()
    throw new Error(`${error.message}\n${diagnostics}`)
  }
}
