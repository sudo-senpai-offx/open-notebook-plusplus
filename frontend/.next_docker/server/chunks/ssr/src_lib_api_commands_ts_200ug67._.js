module.exports=[89952,a=>{"use strict";var b=a.i(92415);a.s(["commandsApi",0,{queueDepth:async()=>{try{let a=await b.default.get("/commands/jobs?status_filter=new");return Array.isArray(a.data)?a.data.length:0}catch{return 0}}}])}];

//# sourceMappingURL=src_lib_api_commands_ts_200ug67._.js.map