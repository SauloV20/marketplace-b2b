// Gráficos do início do painel (os dados vêm nos atributos data-* de cada <canvas>)
(function () {
  'use strict';
  function dados(id) {
    var c = document.getElementById(id);
    return c && { c: c, rotulos: JSON.parse(c.dataset.rotulos || '[]'), valores: JSON.parse(c.dataset.valores || '[]') };
  }
  var d = dados('daily'), t = dados('top');
  if (!window.Chart) return;
  if (d) new Chart(d.c, {type: 'line', data: {labels: d.rotulos, datasets: [{label: 'R$', data: d.valores,
    borderColor: '#1B4DDB', backgroundColor: '#E8EFFD', fill: true, tension: .3}]},
    options: {plugins: {legend: {display: false}}}});
  if (t) new Chart(t.c, {type: 'bar', data: {labels: t.rotulos, datasets: [{label: 'Unidades', data: t.valores,
    backgroundColor: '#1B4DDB'}]}, options: {indexAxis: 'y', plugins: {legend: {display: false}}}});
})();
