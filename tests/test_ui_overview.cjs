
const { test } = require('node:test');
const assert = require('node:assert/strict');
const { insights, trajectory, playedTime } = require('../ui/overview.js');

test('range analysis counts a real zero, preserves missing damage, and sorts a common roster', () => {
  const a={hero:'万蒂奇',damage:0}, b={hero:'万蒂奇',damage:1200}, c={hero:'万蒂奇'};
  const path={hero:'探路者'}, life={hero:'命脉'};
  const rows=[
    {self:a,score:-38,summary:{players:[a,path,life]}},
    {self:b,score:182,summary:{players:[life,b,path]}},
    {self:c,score:null,summary:{players:[c,path]}},
  ];
  const result=insights(rows,r=>r.self,r=>r.score);
  assert.deepEqual([result.scored,result.up,result.down,result.median,result.highDamage,result.damageGames],[2,1,1,600,1,2]);
  assert.equal(result.roster.count,2);
  assert.equal(result.roster.names[0],'万蒂奇');
  assert.deepEqual(trajectory(rows,r=>r.score),[0,null,182,144]);
});

test('played time clips the window, merges overlap, and ignores unknown or edited times', () => {
  const base = Date.parse('2026-11-11T05:00:00Z');
  const at = minute => new Date(base + minute * 60000).toISOString();
  const rows = [
    {round_started_at:at(-5), round_end_at:at(5)},
    {round_started_at:at(4), settled_at:at(10)},
    {round_started_at:at(18), snapshot_at:at(22)},
    {settled_at:at(30)},
    {round_started_at:at(30), settled_at:at(40), time_corrected:true},
  ];
  assert.deepEqual(playedTime(rows,base,base+20*60000), {milliseconds:12*60000,games:3});
});
