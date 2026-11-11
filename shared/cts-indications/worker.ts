import {readFileSync} from "node:fs";
import {alignedSourceStates} from "./aligned.ts";
try{const request=JSON.parse(readFileSync(0,"utf8"));const result=alignedSourceStates(request.symbol??"ASSET-USDT",request.bars,request.sources,request.peers??[]);process.stdout.write(JSON.stringify(result));}
catch(error){process.stderr.write(error instanceof Error?error.message:String(error));process.exitCode=1;}
