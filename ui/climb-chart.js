'use strict';
const ClimbChart=(()=>{
 function segments(points){
   // Shape-preserving cubic Hermite slopes: passes through each observed RP, without overshoot.
   if(points.length<2)return [];
   const slopes=points.slice(1).map((p,i)=>(p.y-points[i].y)/(p.x-points[i].x||1));
   const tangent=points.map((p,i)=>i===0?slopes[0]:i===points.length-1?slopes.at(-1):slopes[i-1]*slopes[i]<=0?0:2*slopes[i-1]*slopes[i]/(slopes[i-1]+slopes[i]));
   return points.slice(1).map((p,i)=>{const a=points[i],dx=(p.x-a.x)/3;return {from:a,to:p,path:`M${a.x},${a.y} C${a.x+dx},${a.y+tangent[i]*dx} ${p.x-dx},${p.y-tangent[i+1]*dx} ${p.x},${p.y}`};});
 }
 function select(points,range){
   if(range==='all')return points;
   const count=Number(range),indices=points.map((p,i)=>p.match_id?i:-1).filter(i=>i>=0);
   if(indices.length<=count)return points;
   const start=indices[indices.length-count];return points.slice(start>0&&!points[start-1].match_id?start-1:start);
 }
 return {segments,select};
})();
if(typeof module!=='undefined')module.exports=ClimbChart;
