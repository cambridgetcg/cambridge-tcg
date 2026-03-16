/**
 * Chart.js configuration factories.
 *
 * createPriceChartConfig  — single-line card price chart
 * createIndexChartConfig  — multi-line market index chart (S&P 500 style)
 */
function createPriceChartConfig(labels, prices) {
  return {
    type: 'line',
    data: {
      labels: labels,
      datasets: [{
        label: Currency.chartLabel(),
        data: prices,
        borderColor: '#2563eb',
        backgroundColor: 'rgba(37, 99, 235, 0.08)',
        fill: true,
        tension: 0.3,
        pointRadius: 0,
        pointHitRadius: 10,
      }],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      interaction: { mode: 'index', intersect: false },
      plugins: {
        legend: { display: false },
        tooltip: {
          callbacks: {
            title: function(items) {
              if (!items.length) return '';
              var d = items[0].label;
              return new Date(d + 'T00:00:00').toLocaleDateString('en-GB', {
                day: 'numeric', month: 'short', year: 'numeric',
              });
            },
            label: function(ctx) {
              return ' ' + Currency.format(Number(ctx.raw));
            },
          },
        },
      },
      scales: {
        x: {
          ticks: { maxTicksLimit: 8, maxRotation: 0, font: { size: 11 } },
          grid: { display: false },
        },
        y: {
          ticks: {
            callback: function(v) { return Currency.format(v); },
            font: { size: 11 },
          },
          grid: { color: 'rgba(0,0,0,0.06)' },
        },
      },
    },
  };
}

var INDEX_COLORS = {
  ALL:  { border: '#2563eb', bg: 'rgba(37, 99, 235, 0.08)' },
  OP:   { border: '#dc2626', bg: 'rgba(220, 38, 38, 0.08)' },
  PKMN: { border: '#d97706', bg: 'rgba(217, 119, 6, 0.08)' },
};

var SET_CHART_COLORS = [
  { border: '#2563eb', bg: 'rgba(37, 99, 235, 0.08)' },
  { border: '#dc2626', bg: 'rgba(220, 38, 38, 0.08)' },
  { border: '#16a34a', bg: 'rgba(22, 163, 74, 0.08)' },
  { border: '#d97706', bg: 'rgba(217, 119, 6, 0.08)' },
  { border: '#7c3aed', bg: 'rgba(124, 58, 237, 0.08)' },
  { border: '#db2777', bg: 'rgba(219, 39, 119, 0.08)' },
  { border: '#0891b2', bg: 'rgba(8, 145, 178, 0.08)' },
  { border: '#ea580c', bg: 'rgba(234, 88, 12, 0.08)' },
  { border: '#4f46e5', bg: 'rgba(79, 70, 229, 0.08)' },
  { border: '#059669', bg: 'rgba(5, 150, 105, 0.08)' },
];

function createIndexChartConfig(labels, datasets) {
  return {
    type: 'line',
    data: {
      labels: labels,
      datasets: datasets.map(function(ds, idx) {
        var c = INDEX_COLORS[ds.key] || SET_CHART_COLORS[idx % SET_CHART_COLORS.length];
        return {
          label: ds.name,
          data: ds.data,
          borderColor: c.border,
          backgroundColor: c.bg,
          fill: false,
          tension: 0.3,
          pointRadius: 0,
          pointHitRadius: 10,
          borderWidth: 2,
          spanGaps: true,
        };
      }),
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      interaction: { mode: 'index', intersect: false },
      plugins: {
        legend: {
          display: true,
          position: 'top',
          labels: { usePointStyle: true, pointStyle: 'circle', padding: 16, font: { size: 12 } },
        },
        tooltip: {
          callbacks: {
            title: function(items) {
              if (!items.length) return '';
              var d = items[0].label;
              return new Date(d + 'T00:00:00').toLocaleDateString('en-GB', {
                day: 'numeric', month: 'short', year: 'numeric',
              });
            },
            label: function(ctx) {
              return ' ' + ctx.dataset.label + ': ' + Number(ctx.raw).toFixed(2);
            },
          },
        },
      },
      scales: {
        x: {
          ticks: { maxTicksLimit: 8, maxRotation: 0, font: { size: 11 } },
          grid: { display: false },
        },
        y: {
          title: { display: true, text: 'Index (base = 100)', font: { size: 12 } },
          ticks: { font: { size: 11 } },
          grid: { color: 'rgba(0,0,0,0.06)' },
        },
      },
    },
  };
}
