const fs=require('fs'), vm=require('vm'), assert=require('assert');
const src=fs.readFileSync('frontend/app.js','utf8');
function section(start,end){return src.slice(src.indexOf(start),src.indexOf(end,src.indexOf(start)));}
const requests=[];let renders=0;
const state={projectId:1,imageDashboardSeq:0,imageQueueSeq:0,imageSceneId:10,sceneId:10,workspaceMode:'image',imageDashboard:null,imageQueue:{jobs:[]}};
const context={state,Number,console,api:url=>new Promise(resolve=>requests.push({url,resolve})),$:()=>({textContent:''}),renderImageWorkbench:()=>renders++,renderImageInspector:()=>{},loadImageWorkbenchHistory:async()=>{},showToast:()=>{throw Error('Unexpected error toast');}};
vm.createContext(context);
vm.runInContext(section('async function loadImageDashboard()', '\nfunction renderImageWorkbench()')+'\n'+section('async function refreshImageQueueStatus()', '\nasync function uploadSceneImageFile'),context);
(async()=>{
 const oldDashboard=context.loadImageDashboard();state.projectId=2;const newDashboard=context.loadImageDashboard();
 requests[1].resolve({scenes:[{id:20,name:'project2'}]});await newDashboard;
 requests[0].resolve({scenes:[{id:10,name:'project1'}]});await oldDashboard;
 assert.equal(state.imageDashboard.scenes[0].id,20);assert.equal(state.imageSceneId,20);
 requests.length=0;state.projectId=1;const oldQueue=context.refreshImageQueueStatus();state.projectId=2;const newQueue=context.refreshImageQueueStatus();
 requests[1].resolve({jobs:[{project_id:2}],queue_size:0});await newQueue;
 requests[0].resolve({jobs:[{project_id:1}],queue_size:9});await oldQueue;
 assert.equal(state.imageQueue.jobs[0].project_id,2);assert.equal(state.imageQueue.queue_size,0);
 console.log('PASS actual dashboard and queue loaders reject late responses from previous project');
})().catch(e=>{console.error(e);process.exitCode=1});
