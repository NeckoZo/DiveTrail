const assert = require('node:assert/strict');
const {timeRange} = require('../ui/match-filters.js');

assert.equal(timeRange('2026-02-30T12:00').error, 'invalid_date');
assert.equal(timeRange('', '2026-02-29T12:00').error, 'invalid_date');
assert.equal(timeRange('2024-02-29T12:00').valid, true);
assert.equal(timeRange('2026-11-05T24:00').error, 'invalid_date');
assert.equal(timeRange('0000-11-05T12:00').error, 'invalid_date');
assert.equal(timeRange('2026-11-06T00:00', '2026-11-05T00:00').error, 'reversed');
console.log('Calendar validation: passed');
