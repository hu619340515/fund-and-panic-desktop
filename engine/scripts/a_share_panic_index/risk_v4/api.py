"""本地 v2 类型约束入口，持仓导入先预览，不自动保存。"""
from fastapi import APIRouter,Body,HTTPException,Query


def router(service):
    api=APIRouter(prefix="/api/v2")
    def checked(call,*args,**kwargs):
        try:return call(*args,**kwargs)
        except ValueError as error:raise HTTPException(status_code=400,detail={"code":"invalid_input","message":str(error)}) from error
    @api.get("/risk/snapshot")
    def snapshot(symbol:str="market"):
        return checked(service.snapshot,symbol)
    @api.get("/risk/history")
    def history(symbol:str="market",kind:str=Query("backtest",pattern="^(backtest|published)$"),limit:int=Query(252,ge=1,le=5000)):
        return checked(service.history,symbol,kind,limit)
    @api.get("/risk/validation")
    def validation(symbol:str="market"):
        return checked(service.validation,symbol)
    def symbols(body):
        values=body.get("symbols")
        if values is not None and (not isinstance(values,list) or not 1<=len(values)<=20 or any(not isinstance(v,str) for v in values)):
            raise HTTPException(status_code=400,detail="symbols须为1至20个标的代码")
        return values
    @api.post("/risk/refresh")
    def refresh(body:dict=Body(default={})):
        return checked(service.refresh,symbols(body),body.get("fund_codes"))
    @api.post("/risk/train")
    def train(body:dict=Body(default={})):
        return checked(service.train,symbols(body))
    @api.get("/jobs")
    def jobs():return {"jobs":service.job_status()}
    @api.get("/portfolio")
    def portfolio():return service.portfolio()
    @api.put("/portfolio")
    def update(body:dict=Body(...)):
        return checked(service.update_portfolio,body)
    @api.post("/portfolio/import-csv")
    def import_csv(body:dict=Body(...)):
        if not isinstance(body.get("text"),str):raise HTTPException(status_code=400,detail="CSV text 必须是文本")
        return checked(service.import_csv,body["text"])
    @api.get("/advice")
    def advice():return service.advice()
    return api
