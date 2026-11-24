// Static checks for classic browser scripts in llm_usage/web/:
// verifies every script tag in index.html exists, parses cleanly, and that no
// two scripts loaded in the shared global scope redeclare the same top-level binding.
import {test} from 'node:test';
import assert from 'node:assert/strict';
import {readdirSync, readFileSync} from 'node:fs';
import {fileURLToPath} from 'node:url';
import path from 'node:path';
import vm from 'node:vm';

const webDir=path.resolve(path.dirname(fileURLToPath(import.meta.url)),'../../llm_usage/web');
const indexHtml=readFileSync(path.join(webDir,'index.html'),'utf8');

function scriptOrder(){
 const scripts=[];
 for(const match of indexHtml.matchAll(/<script\s+src="\/assets\/([^"?]+)(?:\?[^"]*)?"/g)){
  scripts.push(match[1]);
 }
 return scripts;
}

test('all web/*.js files parse cleanly as classic scripts',()=>{
 const files=readdirSync(webDir).filter(f=>f.endsWith('.js')).sort();
 assert.ok(files.length>0);
 for(const file of files){
  const code=readFileSync(path.join(webDir,file),'utf8');
  assert.doesNotThrow(()=>new vm.Script(code,{filename:file}),`syntax error in ${file}`);
 }
});

test('index.html loads every page script in order without top-level lexical collisions',()=>{
 const scripts=scriptOrder();
 assert.ok(scripts.length>=10,'expected page scripts in index.html');
 assert.equal(scripts[0],'theme.js');
 assert.equal(scripts[1],'format.js');
 assert.equal(scripts.at(-1),'app.js');
 // Every non-worker JS file in llm_usage/web/ should be referenced by index.html.
 const disk=new Set(readdirSync(webDir).filter(f=>f.endsWith('.js')&&f!=='sw.js'));
 assert.deepEqual(new Set(scripts),disk,'index.html script list must match llm_usage/web/*.js (excluding sw.js)');
 // Combining all classic scripts into one script compiles only if there are no
 // duplicate top-level const/let/class declarations across files.
 const combined=scripts.map(file=>`// === ${file} ===\n`+readFileSync(path.join(webDir,file),'utf8')).join('\n;\n');
 assert.doesNotThrow(()=>new vm.Script(combined,{filename:'combined-web-scripts.js'}));
});
