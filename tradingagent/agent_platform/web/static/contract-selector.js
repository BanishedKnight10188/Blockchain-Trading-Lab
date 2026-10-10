"use strict";
window.ContractSelector=class ContractSelector {
  constructor({select,search,status,retry,fetchCatalog}){
    Object.assign(this,{select,search,status,retry,fetchCatalog});
    this.contracts=[];this.current="";this.loading=false;this.failed=false;
    select.addEventListener("change",()=>{this.current=select.value;});
    search.addEventListener("input",()=>this.render());
    retry.addEventListener("click",()=>this.load());
  }
  reset(){this.search.value="";this.current=this.contracts.some(c=>c.symbol==="BTCUSDT")?"BTCUSDT":this.contracts[0]?.symbol||"";this.render();}
  render(){
    const query=this.search.value.trim().toUpperCase();
    const matches=this.contracts.filter(c=>c.symbol.includes(query));
    const selected=this.contracts.find(c=>c.symbol===this.current);
    const visible=selected&&!matches.includes(selected)?[selected,...matches]:matches;
    this.select.replaceChildren();
    if(!visible.length){const option=document.createElement("option");option.value="";option.textContent=this.loading?"正在获取合约目录…":"合约目录暂不可用";this.select.append(option);this.select.value="";}
    for(const contract of visible){const option=document.createElement("option");option.value=contract.symbol;option.textContent=contract.symbol;option.defaultSelected=contract.symbol==="BTCUSDT";this.select.append(option);}
    if(selected)this.select.value=selected.symbol;
    this.select.disabled=!this.contracts.length;
    if(this.failed)this.status.textContent=this.contracts.length?`读取失败，保留已确认的 ${this.contracts.length} 个合约；可重新获取。`:"合约目录读取失败，请点击重新获取币种。";
    else this.status.textContent=query?`匹配 ${matches.length} / ${this.contracts.length} 个合约；请在下拉列表选择。`:`共 ${this.contracts.length} 个 USDT 永续合约，可展开列表或输入币种搜索。`;
  }
  async load(){
    if(this.loading)return;
    this.loading=true;this.retry.disabled=true;this.status.textContent="正在获取全部 USDT 永续合约…";
    try{
      const data=await this.fetchCatalog();
      if(!Array.isArray(data.contracts)||!data.contracts.length||data.contracts.some(c=>typeof c.symbol!=="string"||!c.symbol.endsWith("USDT"))||new Set(data.contracts.map(c=>c.symbol)).size!==data.contracts.length)throw new Error("invalid_catalog");
      this.contracts=data.contracts;
      if(!this.contracts.some(c=>c.symbol===this.current))this.current=this.contracts.some(c=>c.symbol==="BTCUSDT")?"BTCUSDT":this.contracts[0].symbol;
      this.failed=false;
    }catch{this.failed=true;}
    finally{this.loading=false;this.retry.disabled=false;this.render();}
  }
};
