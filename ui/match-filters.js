'use strict';
// Date controls describe Beijing time even when Windows uses another time zone.
const MatchLogFilters = (() => {
  const hour = 3600000;
  const format = time => new Date(time + 8 * hour).toISOString().slice(0, 16);
  function parseBoundary(value, end) {
    const parts = /^(\d{4}-\d{2}-\d{2})(?:[T ](\d{2}:\d{2}))?$/.exec(value);
    if (!parts || value.startsWith('0000')) return NaN;
    const minute = `${parts[1]}T${parts[2] || (end ? '23:59' : '00:00')}`;
    const time = Date.parse(`${minute}:00+08:00`);
    // Date.parse can normalize impossible dates, such as February 30.
    if (!Number.isFinite(time) || format(time) !== minute) return NaN;
    return time + (end ? 59999 : 0);
  }

  function timeRange(start = '', end = '') {
    start = start.trim();
    end = end.trim();
    const from = start ? parseBoundary(start, false) : -Infinity;
    const to = end ? parseBoundary(end, true) : Infinity;
    const error = Number.isNaN(from) || Number.isNaN(to) ? 'invalid_date' : from > to ? 'reversed' : null;
    return {from, to, active: !!(start || end), valid: error === null, error};
  }

  function recentTimeInputs(hours, now = Date.now()) {
    return [now - hours * hour, now].map(time => format(time).replace('T', ' '));
  }

  function boundaryInput(date, hour, minute, end = false) {
    if (!date || !hour) return date;
    return `${date}T${hour}:${minute || (end ? '59' : '00')}`;
  }

  function matchesTimeRange(match, range) {
    if (!range.active) return true;
    if (!range.valid || (match.record_origin === 'upload' && !match.time_corrected)) return false;
    if (!match.time) return false;
    const timestamp = new Date(match.time).getTime();
    return Number.isFinite(timestamp) && timestamp >= range.from && timestamp <= range.to;
  }

  return Object.freeze({timeRange, matchesTimeRange, recentTimeInputs, boundaryInput});
})();
if (typeof module !== 'undefined' && module.exports) module.exports = MatchLogFilters;
